# RUN: python3 %s

import errno
import itertools
import json
import multiprocessing
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from distributed_state import (  # noqa: E402
    BitmapDeltaJournal,
    bounded_advisory_lock,
    ContentAddressedInputStore,
    COVERAGE_SHARED_FILESYSTEM_REQUIREMENTS,
    CoverageOwnerShardGossip,
    FencedTargetLeaseTable,
    FencedWorkLeaseTable,
    LiveContinuationDescriptor,
    LiveStateStore,
    LEASE_SHARED_FILESYSTEM_REQUIREMENTS,
    PersistentShardLedger,
    ShardedBitmapDeltaJournal,
    SharedFilesystemCapabilityError,
    SharedFilesystemRequirementProfile,
    StateShardCoordinator,
    WorkLeaseJournal,
    durable_link,
    durable_makedirs,
    durable_rename_noreplace,
    durable_replace,
    durable_rmtree,
    durable_rmtree_step,
    durable_unlink,
    merge_shared_filesystem_requirements,
    probe_shared_state_filesystem,
    qualify_shared_filesystem_cluster_lock,
)
from live_continuation import LiveContinuationExecutor  # noqa: E402


def _race_noreplace_rename(
    source: str,
    destination: str,
    start,
    results,
) -> None:
    start.wait()
    try:
        durable_rename_noreplace(source, destination)
    except FileExistsError:
        results.put(("rejected", os.path.basename(source)))
    except BaseException as error:
        results.put(("error", type(error).__name__))
    else:
        results.put(("published", os.path.basename(source)))


class DurableFilesystemTests(unittest.TestCase):
    def test_shared_filesystem_probe_verifies_host_process_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            capability = probe_shared_state_filesystem(tmp, timeout=2.0)

            snapshot = capability.snapshot()
            self.assertEqual(
                snapshot["schema"],
                "symcc-shared-filesystem-capabilities-v1",
            )
            self.assertEqual(snapshot["probe_scope"], "same-host-subprocess-v1")
            self.assertEqual(snapshot["root"], os.path.realpath(tmp))
            for name in {
                "file_fsync",
                "directory_fsync",
                "same_directory_replace",
                "cross_directory_replace",
                "publication_replace",
                "hard_link",
                "durable_unlink",
                "advisory_lock_exclusion",
                "advisory_lock_release",
            }:
                self.assertIs(snapshot[name], True, name)
            self.assertIs(snapshot["cluster_lock_verified"], False)
            self.assertTrue(snapshot["filesystem_type"])
            self.assertFalse(
                any(
                    name.startswith(
                        (
                            ".symcc-fs-probe-",
                            ".symcc-fs-publication-probe-",
                        )
                    )
                    for name in os.listdir(tmp)
                )
            )

    def test_shared_filesystem_probe_checks_configured_publication_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "state")
            publication = os.path.join(tmp, "corpus")
            capability = probe_shared_state_filesystem(
                state,
                timeout=2.0,
                publication_root=publication,
            )

            self.assertEqual(capability.root, os.path.realpath(state))
            self.assertEqual(
                capability.publication_root,
                os.path.realpath(publication),
            )
            self.assertTrue(capability.publication_replace)
            self.assertEqual(os.listdir(state), [])
            self.assertEqual(os.listdir(publication), [])

    def test_cluster_lock_upgrade_requires_complete_multi_host_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = probe_shared_state_filesystem(tmp, timeout=2.0)

        upgraded = qualify_shared_filesystem_cluster_lock(
            local,
            members=((0, "node-a"), (1, "node-b"), (2, "node-b")),
            representatives=(0, 1),
            rounds=2,
            contention_checks=4,
            release_checks=2,
            identity_checks=3,
        )
        snapshot = upgraded.snapshot()
        self.assertFalse(local.cluster_lock_verified)
        self.assertTrue(snapshot["cluster_lock_verified"])
        self.assertEqual(
            snapshot["schema"],
            "symcc-shared-filesystem-capabilities-v3",
        )
        self.assertEqual(snapshot["probe_scope"], "cross-host-mpi-lock-v2")
        self.assertEqual(
            snapshot["cluster_lock_members"],
            [
                {"rank": 0, "processor": "node-a"},
                {"rank": 1, "processor": "node-b"},
                {"rank": 2, "processor": "node-b"},
            ],
        )
        self.assertEqual(snapshot["cluster_lock_representatives"], [0, 1])
        self.assertEqual(snapshot["cluster_lock_rounds"], 2)
        self.assertEqual(snapshot["cluster_lock_contention_checks"], 4)
        self.assertEqual(snapshot["cluster_lock_release_checks"], 2)
        self.assertEqual(snapshot["cluster_lock_identity_checks"], 3)

        with self.assertRaisesRegex(ValueError, "multiple processors"):
            qualify_shared_filesystem_cluster_lock(
                local,
                members=((0, "node-a"), (1, "node-a")),
                representatives=(0,),
                rounds=1,
                contention_checks=1,
                release_checks=1,
                identity_checks=2,
            )
        with self.assertRaisesRegex(ValueError, "contention evidence"):
            qualify_shared_filesystem_cluster_lock(
                local,
                members=((0, "node-a"), (1, "node-b")),
                representatives=(0, 1),
                rounds=2,
                contention_checks=1,
                release_checks=2,
                identity_checks=2,
            )
        with self.assertRaisesRegex(ValueError, "namespace identity evidence"):
            qualify_shared_filesystem_cluster_lock(
                local,
                members=((0, "node-a"), (1, "node-b")),
                representatives=(0, 1),
                rounds=2,
                contention_checks=2,
                release_checks=2,
                identity_checks=1,
            )

    def test_lease_profile_skips_unneeded_operations_and_records_unknown(self):
        module = sys.modules["distributed_state"]
        real_replace = module.durable_replace

        def same_directory_only(source, destination):
            self.assertEqual(os.path.dirname(source), os.path.dirname(destination))
            return real_replace(source, destination)

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch(
                "distributed_state.durable_replace", side_effect=same_directory_only
            ),
            mock.patch(
                "distributed_state.durable_link",
                side_effect=AssertionError("hard link must not be probed"),
            ),
        ):
            capability = probe_shared_state_filesystem(
                tmp,
                timeout=2.0,
                requirements=LEASE_SHARED_FILESYSTEM_REQUIREMENTS,
            )

        snapshot = capability.snapshot()
        self.assertEqual(
            snapshot["schema"],
            "symcc-shared-filesystem-capabilities-v2",
        )
        self.assertEqual(snapshot["requirement_profile"], "lease-table-v1")
        self.assertEqual(
            snapshot["unverified_operations"],
            [
                "cross_directory_replace",
                "publication_replace",
                "hard_link",
            ],
        )
        self.assertIs(snapshot["cross_directory_replace"], None)
        self.assertIs(snapshot["publication_replace"], None)
        self.assertIs(snapshot["hard_link"], None)
        for name in snapshot["required_operations"]:
            self.assertIs(snapshot[name], True, name)

    def test_lease_profile_fails_closed_on_required_replace(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch(
                "distributed_state.durable_replace",
                side_effect=OSError(errno.EOPNOTSUPP, "unsupported replace"),
            ),
        ):
            with self.assertRaisesRegex(
                SharedFilesystemCapabilityError, "same-directory replace"
            ):
                probe_shared_state_filesystem(
                    tmp,
                    timeout=1.0,
                    requirements=LEASE_SHARED_FILESYSTEM_REQUIREMENTS,
                )

    def test_requirement_profile_rejects_incoherent_contracts(self):
        with self.assertRaisesRegex(ValueError, "invalid.*profile"):
            SharedFilesystemRequirementProfile("Not Valid")
        with self.assertRaisesRegex(ValueError, "invalid.*profile"):
            SharedFilesystemRequirementProfile(None)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "file and directory fsync"):
            SharedFilesystemRequirementProfile("missing-fsync", file_fsync=False)
        with self.assertRaisesRegex(ValueError, "must be qualified together"):
            SharedFilesystemRequirementProfile("half-lock", advisory_lock_release=False)
        with self.assertRaisesRegex(TypeError, "hard_link must be bool"):
            SharedFilesystemRequirementProfile("truthy", hard_link=1)

    def test_requirement_profiles_merge_exact_operation_union(self):
        replacement = SharedFilesystemRequirementProfile(
            "replace-only",
            cross_directory_replace=False,
            publication_replace=False,
            hard_link=False,
            durable_unlink=False,
            advisory_lock_exclusion=False,
            advisory_lock_release=False,
        )
        locking = SharedFilesystemRequirementProfile(
            "lock-only",
            same_directory_replace=False,
            cross_directory_replace=False,
            publication_replace=False,
            hard_link=False,
            durable_unlink=False,
        )

        merged = merge_shared_filesystem_requirements(
            "combined-test-v1", replacement, locking
        )

        self.assertEqual(
            merged.required_operations,
            (
                "file_fsync",
                "directory_fsync",
                "same_directory_replace",
                "advisory_lock_exclusion",
                "advisory_lock_release",
            ),
        )
        with self.assertRaisesRegex(ValueError, "at least one"):
            merge_shared_filesystem_requirements("empty-v1")

    def test_mountinfo_parser_selects_and_unescapes_deepest_mount(self):
        module = sys.modules["distributed_state"]
        mountinfo = (
            "36 25 0:42 / / rw,relatime - overlay overlay rw\n"
            "44 36 0:50 / /mnt/shared\\040dir rw,relatime "
            "- nfs4 server:/export rw,local_lock=none\n"
        )
        with mock.patch("builtins.open", mock.mock_open(read_data=mountinfo)):
            self.assertEqual(
                module._mountinfo_for_path("/mnt/shared dir/state"),
                ("/mnt/shared dir", "nfs4", "server:/export"),
            )
        self.assertTrue(module._is_distributed_filesystem("nfs4"))
        self.assertTrue(module._is_distributed_filesystem("fuse.sshfs"))
        self.assertFalse(module._is_distributed_filesystem("fuse.local"))
        self.assertFalse(module._is_distributed_filesystem("overlay"))

    def test_shared_filesystem_probe_rejects_nonexclusive_flock(self):
        acquired = subprocess.CompletedProcess((), 0, "", "")
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch("distributed_state.subprocess.run", return_value=acquired),
        ):
            with self.assertRaisesRegex(
                SharedFilesystemCapabilityError, "did not exclude a child process"
            ):
                probe_shared_state_filesystem(tmp, timeout=1.0)

            self.assertFalse(
                any(name.startswith(".symcc-fs-probe-") for name in os.listdir(tmp))
            )

    def test_shared_filesystem_probe_rejects_unreleased_flock(self):
        blocked = subprocess.CompletedProcess((), 73, "", "")
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch(
                "distributed_state.subprocess.run", side_effect=(blocked, blocked)
            ),
        ):
            with self.assertRaisesRegex(
                SharedFilesystemCapabilityError, "closed advisory lock was not released"
            ):
                probe_shared_state_filesystem(tmp, timeout=1.0)

    def test_shared_filesystem_probe_bounds_child_process_wait(self):
        timeout = subprocess.TimeoutExpired(("python",), 1.0)
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch("distributed_state.subprocess.run", side_effect=timeout),
        ):
            with self.assertRaisesRegex(
                SharedFilesystemCapabilityError, "advisory-lock probe timed out"
            ):
                probe_shared_state_filesystem(tmp, timeout=1.0)

            self.assertFalse(
                any(
                    name.startswith(
                        (
                            ".symcc-fs-probe-",
                            ".symcc-fs-publication-probe-",
                        )
                    )
                    for name in os.listdir(tmp)
                )
            )

    def test_shared_filesystem_probe_cleans_failed_replace(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch(
                "distributed_state.durable_replace",
                side_effect=OSError(errno.EIO, "injected replace failure"),
            ),
        ):
            with self.assertRaisesRegex(
                SharedFilesystemCapabilityError, "same-directory replace"
            ):
                probe_shared_state_filesystem(tmp, timeout=1.0)

            self.assertFalse(
                any(name.startswith(".symcc-fs-probe-") for name in os.listdir(tmp))
            )

    def test_shared_filesystem_probe_rejects_actual_publication_boundary(self):
        module = sys.modules["distributed_state"]
        real_replace = module.durable_replace
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "state")
            corpus = os.path.join(tmp, "corpus")

            def replace(source, destination):
                if os.path.basename(destination) == "published":
                    raise OSError(errno.EXDEV, "injected cross-device link")
                return real_replace(source, destination)

            with mock.patch("distributed_state.durable_replace", side_effect=replace):
                with self.assertRaisesRegex(
                    SharedFilesystemCapabilityError,
                    "configured publication-boundary replace",
                ):
                    probe_shared_state_filesystem(
                        state, timeout=1.0, publication_root=corpus
                    )

            self.assertEqual(os.listdir(state), [])
            self.assertEqual(os.listdir(corpus), [])

    def test_shared_filesystem_probe_rejects_nonfinite_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(
                ValueError, "probe timeout must be a finite number"
            ):
                probe_shared_state_filesystem(tmp, timeout=float("inf"))
            self.assertEqual(os.listdir(tmp), [])

    def test_shared_filesystem_probe_caps_direct_library_timeout(self):
        observed: list[float] = []

        def child_result(_path, timeout):
            observed.append(timeout)
            return subprocess.CompletedProcess(
                (), 73 if len(observed) == 1 else 0, "", ""
            )

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch(
                "distributed_state._run_lock_probe_child", side_effect=child_result
            ),
        ):
            probe_shared_state_filesystem(tmp, timeout=1e9)

        self.assertEqual(observed, [60.0, 60.0])

    def test_fenced_table_can_require_filesystem_capabilities(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = FencedWorkLeaseTable(
                tmp,
                shard_count=2,
                verify_filesystem=True,
                filesystem_probe_timeout=2.0,
            )

            self.assertIsNotNone(table.filesystem_capabilities)
            assert table.filesystem_capabilities is not None
            self.assertTrue(table.filesystem_capabilities.advisory_lock_exclusion)
            self.assertIs(table.filesystem_capabilities.hard_link, None)
            self.assertEqual(
                table.filesystem_capabilities.snapshot()["schema"],
                "symcc-shared-filesystem-capabilities-v2",
            )

    def test_coverage_gossip_uses_its_minimal_filesystem_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = CoverageOwnerShardGossip(
                tmp,
                verify_filesystem=True,
                filesystem_probe_timeout=2.0,
            )

            capability = table.filesystem_capabilities
            self.assertIsNotNone(capability)
            assert capability is not None
            self.assertEqual(
                capability.requirement_profile,
                COVERAGE_SHARED_FILESYSTEM_REQUIREMENTS.name,
            )
            self.assertIs(capability.durable_unlink, None)
            self.assertTrue(capability.same_directory_replace)
            self.assertTrue(capability.advisory_lock_exclusion)

    def test_directory_sync_batch_rejects_cross_directory_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "source"
            destination_dir = Path(tmp) / "destination"
            source_dir.mkdir()
            destination_dir.mkdir()
            source = source_dir / "record"
            destination = destination_dir / "record"
            source.write_bytes(b"record")
            batch = sys.modules["distributed_state"]._DirectorySyncBatch()

            with self.assertRaisesRegex(ValueError, "same-directory replacement"):
                batch.replace(str(source), str(destination))

            self.assertTrue(source.exists())
            self.assertFalse(destination.exists())
            self.assertEqual(batch.pending_count, 0)

    def test_nested_directory_creation_persists_each_new_component(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = os.path.join(tmp, "first")
            nested = os.path.join(first, "nested")
            with mock.patch(
                "distributed_state.fsync_directory",
                wraps=sys.modules["distributed_state"].fsync_directory,
            ) as sync:
                durable_makedirs(nested, exist_ok=False)

            self.assertTrue(os.path.isdir(nested))
            self.assertEqual(
                [call.args[0] for call in sync.call_args_list],
                [first, tmp, nested, first],
            )
            with self.assertRaises(FileExistsError):
                durable_makedirs(nested, exist_ok=False)

    def test_cross_directory_replace_syncs_public_then_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging = os.path.join(tmp, "staging")
            public = os.path.join(tmp, "public")
            os.mkdir(staging)
            os.mkdir(public)
            source = os.path.join(staging, "object")
            destination = os.path.join(public, "object")
            Path(source).write_bytes(b"durable-object")
            with mock.patch(
                "distributed_state.fsync_directory",
                wraps=sys.modules["distributed_state"].fsync_directory,
            ) as sync:
                durable_replace(source, destination)

            self.assertEqual(Path(destination).read_bytes(), b"durable-object")
            self.assertFalse(os.path.exists(source))
            self.assertEqual(
                [call.args[0] for call in sync.call_args_list],
                [public, staging],
            )

    def test_directory_sync_failure_is_reported_after_visible_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "temporary")
            destination = os.path.join(tmp, "published")
            Path(source).write_bytes(b"recoverable")
            with mock.patch(
                "distributed_state.fsync_directory",
                side_effect=OSError("directory-offline"),
            ):
                with self.assertRaisesRegex(OSError, "directory-offline"):
                    durable_replace(source, destination)

            self.assertFalse(os.path.exists(source))
            self.assertEqual(Path(destination).read_bytes(), b"recoverable")

    def test_link_and_unlink_require_directory_durability(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            destination = os.path.join(tmp, "destination")
            Path(source).write_bytes(b"metadata")
            durable_link(source, destination)
            self.assertEqual(Path(destination).read_bytes(), b"metadata")
            durable_unlink(destination)
            self.assertFalse(os.path.exists(destination))

    def test_rmtree_requires_parent_directory_durability(self):
        state = sys.modules["distributed_state"]
        with tempfile.TemporaryDirectory() as tmp:
            tree = os.path.join(tmp, "tree")
            os.makedirs(os.path.join(tree, "nested"))
            Path(tree, "nested", "state").write_bytes(b"durable-state")
            with mock.patch(
                "distributed_state.fsync_directory", wraps=state.fsync_directory
            ) as sync:
                durable_rmtree(tree)
            self.assertFalse(os.path.exists(tree))
            sync.assert_called_once_with(tmp)

        with tempfile.TemporaryDirectory() as tmp:
            tree = os.path.join(tmp, "tree")
            os.mkdir(tree)
            with mock.patch(
                "distributed_state.fsync_directory",
                side_effect=OSError("cleanup durability unknown"),
            ):
                with self.assertRaisesRegex(OSError, "cleanup durability unknown"):
                    durable_rmtree(tree)
            self.assertFalse(os.path.exists(tree))

    def test_rmtree_step_is_bounded_resumable_and_durable(self):
        state = sys.modules["distributed_state"]
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / "tree"
            leaf = tree / "one" / "two"
            leaf.mkdir(parents=True)
            for index in range(5):
                (leaf / f"state-{index}").write_bytes(b"retired")

            results = []
            with mock.patch.object(
                state.shutil,
                "rmtree",
                side_effect=AssertionError("unbounded deletion used"),
            ):
                while tree.exists():
                    result = durable_rmtree_step(
                        str(tree), entry_limit=2, time_limit=1.0
                    )
                    results.append(result)

            self.assertEqual(
                [result.removed_entries for result in results],
                [2, 2, 2, 2],
            )
            self.assertEqual(
                [result.complete for result in results],
                [False, False, False, True],
            )
            self.assertEqual(
                [result.stop_reason for result in results],
                ["entry-limit", "entry-limit", "entry-limit", "complete"],
            )

    def test_rmtree_step_time_budget_progress_and_symlink_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / "tree"
            tree.mkdir()
            for index in range(3):
                (tree / f"state-{index}").write_bytes(b"retired")
            tick = 0

            def advancing_clock():
                nonlocal tick
                tick += 1
                return float(tick)

            result = durable_rmtree_step(
                str(tree),
                entry_limit=10,
                time_limit=0.1,
                _clock=advancing_clock,
            )
            self.assertEqual(result.removed_entries, 1)
            self.assertFalse(result.complete)
            self.assertEqual(result.stop_reason, "time-limit")
            self.assertEqual(len(tuple(tree.iterdir())), 2)
            self.assertTrue(
                durable_rmtree_step(str(tree), entry_limit=10, time_limit=1.0).complete
            )

        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / "tree"
            outside = Path(tmp) / "outside"
            tree.mkdir()
            outside.mkdir()
            marker = outside / "must-remain"
            marker.write_bytes(b"outside")
            (tree / "link").symlink_to(outside, target_is_directory=True)

            result = durable_rmtree_step(str(tree), entry_limit=2, time_limit=1.0)
            self.assertTrue(result.complete)
            self.assertEqual(result.removed_entries, 2)
            self.assertEqual(marker.read_bytes(), b"outside")

    def test_rmtree_step_fails_closed_and_preserves_resumable_progress(self):
        state = sys.modules["distributed_state"]
        for entry_limit, time_limit, expected in (
            (0, 1.0, "entry limit"),
            (True, 1.0, "entry limit"),
            (1, 0.0, "time limit"),
            (1, float("nan"), "time limit"),
        ):
            with self.subTest(entry_limit=entry_limit, time_limit=time_limit):
                with self.assertRaisesRegex(ValueError, expected):
                    durable_rmtree_step(
                        "/unused",
                        entry_limit=entry_limit,
                        time_limit=time_limit,
                    )

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(state.os, "O_NOFOLLOW", None),
        ):
            tree = Path(tmp) / "tree"
            tree.mkdir()
            with self.assertRaisesRegex(
                OSError, "no-follow directory traversal is unavailable"
            ):
                durable_rmtree_step(str(tree), entry_limit=1, time_limit=1.0)
            self.assertTrue(tree.is_dir())

        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / "tree"
            tree.mkdir()
            (tree / "state").write_bytes(b"retired")
            with mock.patch.object(
                state.os, "fsync", side_effect=OSError("incremental durability unknown")
            ):
                with self.assertRaisesRegex(OSError, "incremental durability unknown"):
                    durable_rmtree_step(str(tree), entry_limit=1, time_limit=1.0)
            self.assertTrue(tree.is_dir())
            self.assertEqual(tuple(tree.iterdir()), ())
            self.assertTrue(
                durable_rmtree_step(str(tree), entry_limit=1, time_limit=1.0).complete
            )

    def test_rmtree_step_reopens_after_an_early_directory_eof(self):
        state = sys.modules["distributed_state"]
        real_rmdir = state.os.rmdir
        attempts = 0

        def early_eof_once(path, *, dir_fd=None):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError(errno.ENOTEMPTY, "iterator skipped an entry")
            return real_rmdir(path, dir_fd=dir_fd)

        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / "tree"
            tree.mkdir()
            with mock.patch.object(state.os, "rmdir", side_effect=early_eof_once):
                result = durable_rmtree_step(str(tree), entry_limit=1, time_limit=1.0)
            self.assertTrue(result.complete)
            self.assertEqual(result.removed_entries, 1)
            self.assertEqual(attempts, 2)

    def test_noreplace_rename_is_atomic_and_durable(self):
        state = sys.modules["distributed_state"]
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "active")
            destination = os.path.join(tmp, "retired")
            os.mkdir(source)
            Path(source, "state").write_bytes(b"committed")
            with mock.patch(
                "distributed_state.fsync_directory", wraps=state.fsync_directory
            ) as sync:
                durable_rename_noreplace(source, destination)
            self.assertFalse(os.path.exists(source))
            self.assertEqual(Path(destination, "state").read_bytes(), b"committed")
            sync.assert_called_once_with(tmp)

        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "active")
            destination = os.path.join(tmp, "retired")
            os.mkdir(source)
            os.mkdir(destination)
            Path(source, "source-state").write_bytes(b"source")
            Path(destination, "destination-state").write_bytes(b"destination")
            with self.assertRaises(FileExistsError):
                durable_rename_noreplace(source, destination)
            self.assertEqual(Path(source, "source-state").read_bytes(), b"source")
            self.assertEqual(
                Path(destination, "destination-state").read_bytes(),
                b"destination",
            )

    def test_noreplace_rename_requires_kernel_primitive(self):
        state = sys.modules["distributed_state"]
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(state, "_RENAMEAT2", None),
        ):
            source = os.path.join(tmp, "active")
            destination = os.path.join(tmp, "retired")
            os.mkdir(source)
            with self.assertRaisesRegex(OSError, "RENAME_NOREPLACE.*unavailable"):
                durable_rename_noreplace(source, destination)
            self.assertTrue(os.path.isdir(source))
            self.assertFalse(os.path.lexists(destination))

    def test_noreplace_rename_has_one_multiprocess_winner(self):
        # mpi4py may keep native progress threads after another test imports it.
        # Spawn keeps this filesystem race independent of inherited thread state.
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as tmp:
            destination = os.path.join(tmp, "retired")
            sources = [os.path.join(tmp, f"active-{index}") for index in range(2)]
            for index, source in enumerate(sources):
                os.mkdir(source)
                Path(source, "owner").write_text(str(index), encoding="ascii")

            start = context.Event()
            results = context.Queue()
            processes = [
                context.Process(
                    target=_race_noreplace_rename,
                    args=(source, destination, start, results),
                )
                for source in sources
            ]
            for process in processes:
                process.start()
            start.set()
            for process in processes:
                process.join(5.0)

            self.assertTrue(all(not process.is_alive() for process in processes))
            self.assertEqual([process.exitcode for process in processes], [0, 0])
            outcomes = [results.get(timeout=1.0) for _ in processes]
            self.assertEqual(
                sorted(outcome for outcome, _ in outcomes),
                ["published", "rejected"],
            )
            winner = next(
                source for outcome, source in outcomes if outcome == "published"
            )
            loser = next(
                source for outcome, source in outcomes if outcome == "rejected"
            )
            self.assertFalse(os.path.exists(os.path.join(tmp, winner)))
            self.assertTrue(os.path.isdir(os.path.join(tmp, loser)))
            self.assertEqual(
                Path(destination, "owner").read_text(encoding="ascii"),
                winner.rsplit("-", 1)[1],
            )

    def test_advisory_lock_identity_requires_no_follow(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = os.path.join(tmp, "gc.lock")
            with mock.patch.object(os, "O_NOFOLLOW", None):
                with self.assertRaisesRegex(OSError, "O_NOFOLLOW is required"):
                    with bounded_advisory_lock(
                        lock_path, timeout=0.1, description="test lock"
                    ):
                        self.fail("unsupported no-follow lock was acquired")
            self.assertFalse(os.path.lexists(lock_path))

        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "target")
            lock_path = os.path.join(tmp, "gc.lock")
            Path(target).write_bytes(b"not-a-lock")
            os.symlink(target, lock_path)
            with self.assertRaises(OSError):
                with bounded_advisory_lock(
                    lock_path, timeout=0.1, description="test lock"
                ):
                    self.fail("symlinked lock was acquired")
            self.assertEqual(Path(target).read_bytes(), b"not-a-lock")


class ContentAddressedInputStoreTests(unittest.TestCase):
    def test_nested_root_is_created_and_reopened_component_by_component(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "namespace" / "nested" / "objects"
            store = ContentAddressedInputStore(str(root))
            content = b"component-wise-root-roundtrip"

            object_id, path = store.put(content)
            snapshot = store.snapshot(object_id, retain_content=True)

            self.assertTrue(root.is_dir())
            self.assertEqual(Path(path).read_bytes(), content)
            self.assertEqual(snapshot.sha256, object_id)
            self.assertEqual(snapshot.content, content)

    def test_relative_root_returns_stable_absolute_object_paths(self):
        original_working_directory = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.chdir(tmp)
                store = ContentAddressedInputStore("relative-objects")
                content = b"working-directory-independent-object"
                object_id, path = store.put(content)
                os.chdir(os.path.sep)

                snapshot = store.snapshot(object_id, retain_content=True)
                self.assertTrue(os.path.isabs(path))
                self.assertEqual(path, store.object_path(object_id))
                self.assertEqual(Path(path).read_bytes(), content)
                self.assertEqual(snapshot.content, content)
            finally:
                os.chdir(original_working_directory)

    def test_root_creation_rejects_symlink_ancestor_without_side_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside"
            outside.mkdir()
            alias = root / "alias"
            alias.symlink_to(outside, target_is_directory=True)

            with self.assertRaises(OSError):
                ContentAddressedInputStore(str(alias / "missing" / "objects"))

            self.assertEqual(tuple(outside.iterdir()), ())

    def test_existing_root_rejects_symlink_ancestor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside"
            objects = outside / "objects"
            objects.mkdir(parents=True)
            sentinel = objects / "sentinel"
            sentinel.write_bytes(b"unchanged")
            alias = root / "alias"
            alias.symlink_to(outside, target_is_directory=True)

            with self.assertRaises(OSError):
                ContentAddressedInputStore(str(alias / "objects"))

            self.assertEqual(sentinel.read_bytes(), b"unchanged")
            self.assertEqual(tuple(objects.iterdir()), (sentinel,))

    def test_object_suffix_is_bounded_to_the_digest_leaf(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ContentAddressedInputStore(tmp, object_suffix=".json")
            object_id, path = store.put(b"suffix-aware-object")
            self.assertEqual(
                path,
                os.path.join(tmp, object_id[:2], object_id[2:] + ".json"),
            )
            self.assertEqual(Path(path).read_bytes(), b"suffix-aware-object")

            full_leaf_store = ContentAddressedInputStore(
                os.path.join(tmp, "full-leaf"),
                object_suffix=".smt2",
                full_digest_leaf=True,
            )
            full_id, full_path = full_leaf_store.put(b"full-digest-leaf")
            self.assertEqual(
                full_path,
                os.path.join(
                    tmp,
                    "full-leaf",
                    full_id[:2],
                    full_id + ".smt2",
                ),
            )
            self.assertEqual(Path(full_path).read_bytes(), b"full-digest-leaf")

            with self.assertRaisesRegex(ValueError, "must be Boolean"):
                ContentAddressedInputStore(
                    os.path.join(tmp, "invalid-leaf-mode"),
                    full_digest_leaf=1,
                )

            for invalid in ("/escape", "nested/object", "nul\0suffix"):
                with self.subTest(invalid=invalid):
                    with self.assertRaisesRegex(ValueError, "object suffix"):
                        ContentAddressedInputStore(
                            os.path.join(tmp, "invalid"),
                            object_suffix=invalid,
                        )

    def test_object_is_verified_and_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ContentAddressedInputStore(tmp)
            object_id, path = store.put(b"symbolic-input")
            self.assertEqual(Path(path).read_bytes(), b"symbolic-input")
            self.assertEqual(store.materialize(object_id, None), path)
            with self.assertRaises(ValueError):
                store.put(b"different", object_id)

    def test_import_enforces_transport_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            Path(source).write_bytes(b"12345")
            store = ContentAddressedInputStore(os.path.join(tmp, "objects"), 4)
            with self.assertRaises(ValueError):
                store.import_path(source)

    def test_import_rejects_symlink_and_path_replacement(self):
        state = sys.modules["distributed_state"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.write_bytes(b"a" * (2 * 1024 * 1024))
            alias = root / "alias"
            alias.symlink_to(source)
            store = ContentAddressedInputStore(str(root / "objects"))
            with self.assertRaises(OSError):
                store.import_path(str(alias))

            replacement = root / "replacement"
            replacement.write_bytes(b"b" * (2 * 1024 * 1024))
            original_read = state.os.read
            replaced = False

            def replace_after_first_read(descriptor, size):
                nonlocal replaced
                chunk = original_read(descriptor, size)
                if chunk and not replaced:
                    replaced = True
                    os.replace(replacement, source)
                return chunk

            with mock.patch.object(
                state.os, "read", side_effect=replace_after_first_read
            ):
                with self.assertRaises(OSError):
                    store.import_path(str(source))

    def test_cached_object_is_verified_and_repaired(self):
        state = sys.modules["distributed_state"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ContentAddressedInputStore(str(root / "objects"))
            content = b"verified-content-addressed-input"
            object_id, path = store.put(content)
            with mock.patch.object(
                state,
                "stable_regular_file_snapshot",
                side_effect=AssertionError("identity fast path missed"),
            ):
                self.assertEqual(store.materialize(object_id, None), path)

            Path(path).write_bytes(b"corrupt")
            with self.assertRaises(ValueError):
                store.materialize(object_id, None)
            repaired_id, repaired_path = store.put(content, object_id)
            self.assertEqual(repaired_id, object_id)
            self.assertEqual(repaired_path, path)
            self.assertEqual(Path(path).read_bytes(), content)

            outside = root / "outside"
            outside.write_bytes(b"outside-must-not-change")
            Path(path).unlink()
            Path(path).symlink_to(outside)
            with self.assertRaises(ValueError):
                store.materialize(object_id, None)
            store.put(content, object_id)
            self.assertFalse(Path(path).is_symlink())
            self.assertEqual(Path(path).read_bytes(), content)
            self.assertEqual(outside.read_bytes(), b"outside-must-not-change")

    def test_put_closes_publication_race_and_accepts_exact_competitor(self):
        state = sys.modules["distributed_state"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ContentAddressedInputStore(str(root / "objects"))
            content = b"publication-race-content"
            object_id = store.digest(content)
            original_replace = state.durable_replace

            fast_store = ContentAddressedInputStore(str(root / "fast-objects"))
            with mock.patch.object(
                state,
                "stable_regular_file_snapshot",
                wraps=state.stable_regular_file_snapshot,
            ) as fallback_hash:
                fast_id, fast_path = fast_store.put(content, object_id)
            self.assertEqual(fast_id, object_id)
            self.assertEqual(Path(fast_path).read_bytes(), content)
            fallback_hash.assert_not_called()

            def write_at(directory_fd, name, value):
                descriptor = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                    0o600,
                    dir_fd=directory_fd,
                )
                try:
                    os.write(descriptor, value)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)

            def corrupt_after_publish(
                source,
                destination,
                *,
                directory_fd=None,
            ):
                self.assertIsNotNone(directory_fd)
                original_replace(
                    source,
                    destination,
                    directory_fd=directory_fd,
                )
                write_at(
                    directory_fd,
                    destination,
                    b"wrong-publication-content",
                )

            with mock.patch.object(
                state,
                "stable_regular_file_snapshot",
                wraps=state.stable_regular_file_snapshot,
            ) as fallback_hash:
                with mock.patch.object(
                    state, "durable_replace", side_effect=corrupt_after_publish
                ):
                    with self.assertRaisesRegex(OSError, "changed during publication"):
                        store.put(content, object_id)
            self.assertEqual(fallback_hash.call_count, 1)
            self.assertNotIn(object_id, store._verified_identities)

            def exact_competitor_after_publish(
                source,
                destination,
                *,
                directory_fd=None,
            ):
                self.assertIsNotNone(directory_fd)
                original_replace(
                    source,
                    destination,
                    directory_fd=directory_fd,
                )
                competitor = destination + ".competitor"
                write_at(directory_fd, competitor, content)
                os.replace(
                    competitor,
                    destination,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                )

            exact_store = ContentAddressedInputStore(str(root / "exact-objects"))
            with mock.patch.object(
                state,
                "stable_regular_file_snapshot",
                wraps=state.stable_regular_file_snapshot,
            ) as fallback_hash:
                with mock.patch.object(
                    state, "durable_replace", side_effect=exact_competitor_after_publish
                ):
                    observed_id, path = exact_store.put(content, object_id)
            self.assertEqual(observed_id, object_id)
            self.assertEqual(Path(path).read_bytes(), content)
            self.assertEqual(fallback_hash.call_count, 1)
            self.assertIn(object_id, exact_store._verified_identities)

            retry_store = ContentAddressedInputStore(str(root / "retry-objects"))
            snapshot_attempts = 0
            original_snapshot = state.stable_regular_file_snapshot

            def transient_snapshot(*args, **kwargs):
                nonlocal snapshot_attempts
                snapshot_attempts += 1
                if snapshot_attempts <= 3:
                    raise OSError(errno.ESTALE, "injected concurrent replacement")
                return original_snapshot(*args, **kwargs)

            with (
                mock.patch.object(
                    state,
                    "durable_replace",
                    side_effect=exact_competitor_after_publish,
                ),
                mock.patch.object(
                    state,
                    "stable_regular_file_snapshot",
                    side_effect=transient_snapshot,
                ),
            ):
                retried_id, retried_path = retry_store.put(content, object_id)
            self.assertEqual(retried_id, object_id)
            self.assertEqual(Path(retried_path).read_bytes(), content)
            self.assertEqual(snapshot_attempts, 4)
            self.assertIn(object_id, retry_store._verified_identities)

            bounded_store = ContentAddressedInputStore(
                str(root / "bounded-retry-objects")
            )
            bounded_attempts = 0

            def unstable_snapshot(*_args, **_kwargs):
                nonlocal bounded_attempts
                bounded_attempts += 1
                raise OSError(errno.ESTALE, "injected persistent replacement")

            with (
                mock.patch.object(
                    state,
                    "durable_replace",
                    side_effect=exact_competitor_after_publish,
                ),
                mock.patch.object(
                    state,
                    "stable_regular_file_snapshot",
                    side_effect=unstable_snapshot,
                ),
                mock.patch.object(
                    state,
                    "_INPUT_STORE_PUBLICATION_VERIFY_ATTEMPTS",
                    3,
                ),
            ):
                with self.assertRaisesRegex(OSError, "changed during publication"):
                    bounded_store.put(content, object_id)
            self.assertEqual(bounded_attempts, 3)
            self.assertNotIn(object_id, bounded_store._verified_identities)

    def test_object_namespace_rejects_root_and_shard_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside_root = root / "outside-root"
            outside_root.mkdir()
            alias = root / "root-alias"
            alias.symlink_to(outside_root, target_is_directory=True)
            with self.assertRaises(OSError):
                ContentAddressedInputStore(str(alias))
            self.assertEqual(tuple(outside_root.iterdir()), ())

            objects = root / "objects"
            store = ContentAddressedInputStore(str(objects))
            content = b"shard-symlink-must-not-redirect"
            object_id = store.digest(content)
            outside_shard = root / "outside-shard"
            outside_shard.mkdir()
            (objects / object_id[:2]).symlink_to(
                outside_shard,
                target_is_directory=True,
            )
            with self.assertRaises(OSError):
                store.put(content, object_id)
            self.assertEqual(tuple(outside_shard.iterdir()), ())
            self.assertNotIn(object_id, store._verified_identities)

    def test_publication_rejects_replaced_shard_without_redirecting_io(self):
        state = sys.modules["distributed_state"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            objects = root / "objects"
            outside = root / "outside"
            outside.mkdir()
            store = ContentAddressedInputStore(str(objects))
            content = b"descriptor-anchored-publication"
            object_id = store.digest(content)
            shard = objects / object_id[:2]
            detached = root / "detached-shard"
            original_replace = state.durable_replace
            replaced = False

            def replace_shard(
                source,
                destination,
                *,
                directory_fd=None,
            ):
                nonlocal replaced
                original_replace(
                    source,
                    destination,
                    directory_fd=directory_fd,
                )
                shard.rename(detached)
                shard.symlink_to(outside, target_is_directory=True)
                replaced = True

            with mock.patch.object(state, "durable_replace", side_effect=replace_shard):
                with self.assertRaisesRegex(OSError, "CAS directory"):
                    store.put(content, object_id)

            self.assertTrue(replaced)
            self.assertEqual(tuple(outside.iterdir()), ())
            self.assertEqual(
                (detached / (object_id[2:])).read_bytes(),
                content,
            )
            self.assertEqual(tuple(detached.glob("*.tmp")), ())
            self.assertNotIn(object_id, store._verified_identities)

    def test_publication_rejects_replaced_root_without_redirecting_io(self):
        state = sys.modules["distributed_state"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            objects = root / "objects"
            outside = root / "outside-root"
            outside.mkdir()
            store = ContentAddressedInputStore(str(objects))
            content = b"descriptor-anchored-root-publication"
            object_id = store.digest(content)
            detached = root / "detached-root"
            original_replace = state.durable_replace
            replaced = False

            def replace_root(
                source,
                destination,
                *,
                directory_fd=None,
            ):
                nonlocal replaced
                original_replace(
                    source,
                    destination,
                    directory_fd=directory_fd,
                )
                objects.rename(detached)
                objects.symlink_to(outside, target_is_directory=True)
                replaced = True

            with mock.patch.object(state, "durable_replace", side_effect=replace_root):
                with self.assertRaisesRegex(OSError, "CAS root ancestry"):
                    store.put(content, object_id)

            self.assertTrue(replaced)
            self.assertEqual(tuple(outside.iterdir()), ())
            self.assertEqual(
                (detached / object_id[:2] / object_id[2:]).read_bytes(),
                content,
            )
            self.assertEqual(tuple(detached.rglob("*.tmp")), ())
            self.assertNotIn(object_id, store._verified_identities)

    def test_publication_rejects_replaced_root_ancestor(self):
        state = sys.modules["distributed_state"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            namespace = root / "namespace"
            stable = namespace / "stable"
            objects = stable / "objects"
            outside = root / "outside-ancestor"
            outside_objects = outside / "objects"
            outside_objects.mkdir(parents=True)
            sentinel = outside_objects / "sentinel"
            sentinel.write_bytes(b"outside-unchanged")
            store = ContentAddressedInputStore(str(objects))
            content = b"descriptor-anchored-ancestor-publication"
            object_id = store.digest(content)
            detached = root / "detached-ancestor"
            original_replace = state.durable_replace
            replaced = False

            def replace_ancestor(
                source,
                destination,
                *,
                directory_fd=None,
            ):
                nonlocal replaced
                original_replace(
                    source,
                    destination,
                    directory_fd=directory_fd,
                )
                stable.rename(detached)
                stable.symlink_to(outside, target_is_directory=True)
                replaced = True

            with mock.patch.object(
                state, "durable_replace", side_effect=replace_ancestor
            ):
                with self.assertRaisesRegex(OSError, "CAS root ancestry"):
                    store.put(content, object_id)

            self.assertTrue(replaced)
            self.assertEqual(sentinel.read_bytes(), b"outside-unchanged")
            self.assertEqual(tuple(outside_objects.iterdir()), (sentinel,))
            self.assertEqual(
                (detached / "objects" / object_id[:2] / object_id[2:]).read_bytes(),
                content,
            )
            self.assertEqual(tuple(detached.rglob("*.tmp")), ())
            self.assertNotIn(object_id, store._verified_identities)


class BitmapDeltaJournalTests(unittest.TestCase):
    def test_delta_catches_worker_up_to_current_version(self):
        journal = BitmapDeltaJournal(4)
        journal.record(1, [(2, 1)])
        journal.record(2, [(2, 2), (9, 4)])
        payload = journal.payload(0, 2, bytes(16))
        bitmap = journal.apply(None, payload, minimum_size=16)
        self.assertEqual(bitmap[2], 3)
        self.assertEqual(bitmap[9], 4)

    def test_full_snapshot_is_used_after_history_eviction(self):
        journal = BitmapDeltaJournal(1)
        journal.record(1, [(1, 1)])
        journal.record(2, [(2, 2)])
        full = bytes([0, 1, 2, 0])
        payload = journal.payload(0, 2, full)
        self.assertEqual(payload["bitmap_full"], full)


class ShardedDistributedStateTests(unittest.TestCase):
    def test_sharded_bitmap_payload_is_equivalent_to_global_delta(self):
        journal = ShardedBitmapDeltaJournal(history_limit=4, shard_count=4)
        journal.record(1, [(1, 1), (2, 2)])
        journal.record(2, [(5, 4), (8, 8)])
        payload = journal.payload(0, 2, bytes(16))
        bitmap = BitmapDeltaJournal.apply(None, payload, minimum_size=16)
        self.assertEqual(bitmap[1], 1)
        self.assertEqual(bitmap[2], 2)
        self.assertEqual(bitmap[5], 4)
        self.assertEqual(bitmap[8], 8)
        self.assertEqual(payload["bitmap_shards"], 4)

    def test_persistent_shard_ledger_recovers_digests(self):
        with tempfile.TemporaryDirectory() as tmp:
            digest_a = "0" * 64
            digest_b = "f" * 64
            ledger = PersistentShardLedger(tmp, shard_count=8)
            ledger.add(digest_a)
            ledger.add(digest_b)
            reloaded = PersistentShardLedger(tmp, shard_count=8)
            self.assertEqual(reloaded.load_recent(), {digest_a, digest_b})

    def test_coverage_owner_shards_deduplicate_and_gossip_bucket_bits(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = CoverageOwnerShardGossip(
                tmp,
                shard_count=4,
                coordinator_id="master-a",
                coordinator_index=0,
                coordinator_count=2,
            )
            second = CoverageOwnerShardGossip(
                tmp,
                shard_count=4,
                coordinator_id="master-b",
                coordinator_index=1,
                coordinator_count=2,
            )

            self.assertEqual(first.claim([(1, 1), (6, 2)], now=10.0), 2)
            self.assertEqual(second.claim([(1, 1)], now=11.0), 0)
            self.assertEqual(second.claim([(1, 5), (7, 8)], now=12.0), 2)

            first_delta = dict(first.pull())
            second_delta = dict(second.pull())
            self.assertEqual(first_delta[1], 5)
            self.assertEqual(first_delta[6], 2)
            self.assertEqual(first_delta[7], 8)
            self.assertEqual(second_delta, first_delta)
            self.assertGreaterEqual(second.snapshot()["duplicate_features"], 1)

            restored = CoverageOwnerShardGossip(
                tmp,
                shard_count=4,
                coordinator_id="master-c",
                coordinator_index=0,
                coordinator_count=1,
            )
            self.assertEqual(dict(restored.pull()), first_delta)

    def test_coverage_owner_batch_preserves_order_with_one_write_per_shard(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = CoverageOwnerShardGossip(
                tmp,
                shard_count=2,
                coordinator_id="master",
            )

            novelty = table.claim_many(
                [
                    [(1, 1)],
                    [(1, 3)],
                    [(3, 1)],
                    [(1, 1), (3, 1)],
                ],
                now=10.0,
            )

            self.assertEqual(novelty, [1, 1, 1, 0])
            snapshot = table.snapshot()
            self.assertEqual(snapshot["claims"], 4)
            self.assertEqual(snapshot["claim_batches"], 1)
            self.assertEqual(snapshot["claim_shard_writes"], 1)
            self.assertEqual(dict(table.pull()), {1: 3, 3: 1})

            left = CoverageOwnerShardGossip(
                tmp,
                shard_count=2,
                coordinator_id="left",
                coordinator_index=0,
                coordinator_count=2,
            )
            right = CoverageOwnerShardGossip(
                tmp,
                shard_count=2,
                coordinator_id="right",
                coordinator_index=1,
                coordinator_count=2,
            )
            rendezvous = threading.Barrier(2)
            errors = []
            results = []
            left_write = left._write_shard
            right_write = right._write_shard

            def synchronized(write):
                def run(shard, record):
                    rendezvous.wait(timeout=2.0)
                    write(shard, record)
                return run

            def claim(owner, delta):
                try:
                    results.append(owner.claim(delta, now=20.0))
                except BaseException as error:
                    errors.append(error)

            with mock.patch.object(
                left, "_write_shard", side_effect=synchronized(left_write)
            ), mock.patch.object(
                right, "_write_shard", side_effect=synchronized(right_write)
            ):
                threads = [
                    threading.Thread(target=claim, args=(left, [(2, 1)])),
                    threading.Thread(target=claim, args=(right, [(5, 2)])),
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=3.0)
                self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(sorted(results), [1, 1])

    def test_coverage_owner_rejects_malformed_delta_without_partial_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = CoverageOwnerShardGossip(
                tmp, shard_count=4, coordinator_id="master"
            )
            invalid = (
                [(5, -1)],
                [(5, 0)],
                [(5, 256)],
                [(5, 1), (5, 2)],
                [(True, 1)],
                [(5, True)],
                [("5", 1)],
                [((1 << 23), 1)],
                [(5, 1), ("bad", 2)],
            )
            for delta in invalid:
                with self.subTest(delta=delta), self.assertRaisesRegex(
                    ValueError, "coverage delta"
                ):
                    table.claim(delta, now=10.0)
            self.assertEqual(table.snapshot()["claims"], 0)
            self.assertEqual(table.pull(), [])

    def test_coverage_owner_rejects_unbounded_topology_batch_and_pull(self):
        with tempfile.TemporaryDirectory() as tmp:
            for arguments in (
                {"shard_count": 0},
                {"shard_count": 4097},
                {"coordinator_index": 1, "coordinator_count": 1},
                {"coordinator_count": 4097},
            ):
                with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                    CoverageOwnerShardGossip(tmp, **arguments)

            table = CoverageOwnerShardGossip(tmp, shard_count=4)
            with self.assertRaisesRegex(ValueError, "candidate budget"):
                table.claim_many([[] for _ in range(4097)])
            for shards in ([True], [4], ["1"], "1", {1: "invalid"}):
                with self.subTest(shards=shards), self.assertRaisesRegex(
                    ValueError, "coverage pull"
                ):
                    table.pull(shards)
            with self.assertRaisesRegex(ValueError, "coverage pull"):
                table.pull(itertools.repeat(0))
            self.assertEqual(table.pull([0, 0, 1]), [])

    def test_coverage_owner_dense_bitmap_grouping_has_bounded_amplification(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = CoverageOwnerShardGossip(tmp, shard_count=64)
            dense = bytearray(1 << 18)
            dense[::4] = b"\x01" * (len(dense) // 4)

            tracemalloc.start()
            grouped, feature_count = table._group_deltas([dense])
            _current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()

            self.assertEqual(feature_count, len(dense) // 4)
            self.assertEqual(sum(len(entries) for entries in grouped.values()), feature_count)
            self.assertLess(peak, len(dense) * 8)

    def test_coverage_owner_enforces_total_batch_feature_budget_before_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = CoverageOwnerShardGossip(tmp, shard_count=4)
            module = sys.modules["distributed_state"]
            with mock.patch.object(
                module, "_MAX_COVERAGE_CLAIM_FEATURES", 2,
            ), self.assertRaisesRegex(ValueError, "feature budget"):
                table.claim_many(
                    [bytes((1, 1)), [(2, 1)]],
                    now=10.0,
                )
            self.assertEqual(table.snapshot()["claims"], 0)
            self.assertEqual(table.pull(), [])

    def test_coverage_owner_computes_liveness_once_per_claim_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = CoverageOwnerShardGossip(
                tmp,
                shard_count=16,
                coordinator_id="master",
                coordinator_count=16,
            )
            with mock.patch.object(
                table,
                "_live_coordinators",
                wraps=table._live_coordinators,
            ) as live:
                self.assertEqual(
                    table.claim([(index, 1) for index in range(16)], now=10.0),
                    16,
                )
            self.assertEqual(live.call_count, 1)

    def test_coverage_owner_full_pull_uses_bounded_lock_batches(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = CoverageOwnerShardGossip(
                tmp, shard_count=20, coordinator_id="master"
            )
            with mock.patch.object(
                table,
                "_locked_shards_after_recovery",
                wraps=table._locked_shards_after_recovery,
            ) as locked:
                self.assertEqual(table.pull(), [])
            batches = [set(call.args[0]) for call in locked.call_args_list]
            self.assertEqual(len(batches), 3)
            self.assertEqual(set().union(*batches), set(range(20)))
            self.assertLessEqual(max(map(len, batches)), 8)

    def test_coverage_owner_batch_retries_transient_multishard_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = CoverageOwnerShardGossip(
                tmp,
                shard_count=2,
                coordinator_id="master",
            )
            original_write = table._write_shard
            failed_once = False

            def transient_write(shard, record):
                nonlocal failed_once
                if shard == 1 and not failed_once:
                    failed_once = True
                    raise OSError("injected transient shard failure")
                original_write(shard, record)

            with mock.patch.object(table, "_write_shard", transient_write):
                novelty = table.claim_many(
                    [[(0, 1)], [(1, 2)]],
                    now=10.0,
                )

            self.assertEqual(novelty, [1, 1])
            self.assertFalse(os.path.exists(table.transaction_path()))
            self.assertEqual(dict(table.pull()), {0: 1, 1: 2})

    def test_coverage_owner_recovers_interrupted_multishard_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = CoverageOwnerShardGossip(
                tmp,
                shard_count=2,
                coordinator_id="master-a",
            )
            original_write = table._write_shard

            def persistent_failure(shard, record):
                if shard == 1:
                    raise OSError("injected persistent shard failure")
                original_write(shard, record)

            with mock.patch.object(table, "_write_shard", persistent_failure):
                with self.assertRaisesRegex(OSError, "persistent shard failure"):
                    table.claim_many([[(0, 1), (1, 2)]], now=10.0)

            self.assertTrue(os.path.exists(table.transaction_path()))
            restored = CoverageOwnerShardGossip(
                tmp,
                shard_count=2,
                coordinator_id="master-b",
            )
            self.assertFalse(os.path.exists(restored.transaction_path()))
            self.assertEqual(dict(restored.pull()), {0: 1, 1: 2})
            self.assertEqual(restored.snapshot()["recovered_transactions"], 1)
            self.assertEqual(restored.claim([(0, 1), (1, 2)], now=11.0), 0)

    def test_coverage_transaction_reader_rejects_symlinks_and_empty_wals(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = CoverageOwnerShardGossip(
                tmp, shard_count=2, coordinator_id="master")
            outside = Path(tmp) / "outside.json"
            outside.write_text("{}\n", encoding="ascii")
            symlink = Path(table.transaction_path("a" * 64))
            symlink.symlink_to(outside)
            with self.assertRaises(OSError):
                table._read_transaction(str(symlink))

            symlink.unlink()
            empty = {
                "schema": 1,
                "shard_count": 2,
                "coordinator": "master",
                "created": 10.0,
                "records": [],
                "novel_by_candidate": [0],
            }
            empty["transaction_sha256"] = table._transaction_digest(empty)
            table._write_transaction(empty, str(symlink))
            with self.assertRaisesRegex(
                ValueError, "invalid coverage-owner batch transaction"
            ):
                table._read_transaction(str(symlink))

    def test_coverage_transaction_writer_enforces_reader_byte_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = CoverageOwnerShardGossip(
                tmp, shard_count=2, coordinator_id="master")
            record = {"payload": "x" * 128}
            destination = table.transaction_path("b" * 64)
            with mock.patch.object(
                sys.modules["distributed_state"],
                "_MAX_COVERAGE_TRANSACTION_BYTES",
                32,
            ), self.assertRaisesRegex(ValueError, "exceeds byte budget"):
                table._write_transaction(record, destination)
            self.assertFalse(os.path.exists(destination))
            self.assertEqual(list(Path(tmp).rglob("*.tmp")), [])

    def test_coverage_shard_lock_is_bounded_and_never_age_stolen(self):
        with tempfile.TemporaryDirectory() as tmp:
            holder = CoverageOwnerShardGossip(
                tmp,
                shard_count=4,
                coordinator_id="holder",
                lock_ttl=1.0,
                lock_acquire_timeout=1.0,
            )
            contender = CoverageOwnerShardGossip(
                tmp,
                shard_count=4,
                coordinator_id="contender",
                lock_ttl=1.0,
                lock_acquire_timeout=0.01,
            )
            shard = holder.shard_for_index(5)
            with holder._locked(shard):
                os.utime(holder.lock_path(shard), (1.0, 1.0))
                started = time.monotonic()
                with self.assertRaisesRegex(
                    TimeoutError,
                    "configured age hint=1.000s; kernel advisory locks "
                    "are never time-stolen",
                ):
                    contender.claim([(5, 1)], now=10.0)
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertTrue(os.path.isfile(holder.lock_path(shard)))

            self.assertEqual(contender.claim([(5, 1)], now=11.0), 1)

    def test_coverage_gossip_rejects_nonfinite_lock_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "coverage heartbeat TTL must be"):
                CoverageOwnerShardGossip(tmp, heartbeat_ttl=float("inf"))
            with self.assertRaisesRegex(ValueError, "coverage lock age hint must be"):
                CoverageOwnerShardGossip(tmp, lock_ttl=float("nan"))
            with self.assertRaisesRegex(
                ValueError, "coverage lock acquisition timeout must be"
            ):
                CoverageOwnerShardGossip(tmp, lock_acquire_timeout=float("inf"))

    def test_coverage_shard_corruption_fails_closed_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = CoverageOwnerShardGossip(
                tmp, shard_count=4, coordinator_id="master"
            )
            index = 5
            shard = table.shard_for_index(index)
            self.assertEqual(table.claim([(index, 1)], now=10.0), 1)
            path = Path(table.state_path(shard))
            valid_record = {
                "schema": 1,
                "shard": shard,
                "epoch": 1,
                "entries": [[index, 1]],
                "contributors": {"master": 1},
            }
            corrupt_records = (
                {**valid_record, "shard": shard + 1},
                {**valid_record, "entries": [[index + 1, 1]]},
                {**valid_record, "updated": float("inf")},
            )

            for corrupt_record in corrupt_records:
                with self.subTest(record=corrupt_record):
                    path.write_text(json.dumps(corrupt_record), encoding="utf-8")
                    corrupt = path.read_bytes()

                    with self.assertRaisesRegex(
                        ValueError, "invalid coverage-owner shard record"
                    ):
                        table.claim([(index + 4, 2)], now=11.0)

                    self.assertEqual(path.read_bytes(), corrupt)
            restored = CoverageOwnerShardGossip(
                tmp, shard_count=4, coordinator_id="reader"
            )
            with self.assertRaisesRegex(
                ValueError, "invalid coverage-owner shard record"
            ):
                restored.pull([shard])

    def test_coverage_shard_and_peer_heartbeat_reject_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = CoverageOwnerShardGossip(
                tmp,
                shard_count=4,
                coordinator_id="master-0",
                coordinator_index=0,
                coordinator_count=2,
            )
            outside = Path(tmp) / "outside.json"
            outside.write_text("{}\n", encoding="ascii")
            shard = table.shard_for_index(5)
            Path(table.state_path(shard)).symlink_to(outside)
            with self.assertRaises(OSError):
                table.pull([shard])

            Path(table.heartbeat_path(1)).symlink_to(outside)
            self.assertEqual(table.owner_for_shard(1, now=10.0), (0, "master-0"))

    def test_coverage_shard_write_failure_cleans_temporary_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = CoverageOwnerShardGossip(
                tmp, shard_count=4, coordinator_id="master"
            )
            index = 5
            shard = table.shard_for_index(index)
            destination = table.state_path(shard)
            real_replace = sys.modules["distributed_state"].durable_replace

            def fail_shard(source, target):
                if target == destination:
                    raise OSError("injected shard publication failure")
                real_replace(source, target)

            with mock.patch(
                "distributed_state.durable_replace", side_effect=fail_shard
            ):
                with self.assertRaisesRegex(
                    OSError, "injected shard publication failure"
                ):
                    table.claim([(index, 1)], now=10.0)

            self.assertFalse(os.path.exists(destination))
            self.assertEqual(
                list(Path(destination).parent.glob(Path(destination).name + ".*.tmp")),
                [],
            )

    def test_coverage_timestamps_reject_nonfinite_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = CoverageOwnerShardGossip(
                tmp,
                shard_count=4,
                coordinator_id="master",
                coordinator_index=0,
                coordinator_count=2,
            )
            heartbeat_before = Path(table.heartbeat_path()).read_bytes()

            with self.assertRaisesRegex(ValueError, "coverage timestamp must be"):
                table.claim([(5, 1)], now=float("nan"))
            with self.assertRaisesRegex(ValueError, "coverage timestamp must be"):
                table.owner_for_shard(1, now=float("inf"))

            self.assertEqual(
                Path(table.heartbeat_path()).read_bytes(), heartbeat_before
            )
            self.assertFalse(os.path.exists(table.state_path(1)))

    def test_nonfinite_peer_heartbeat_cannot_become_immortal_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = CoverageOwnerShardGossip(
                tmp,
                shard_count=4,
                coordinator_id="master-0",
                coordinator_index=0,
                coordinator_count=2,
            )
            Path(table.heartbeat_path(1)).write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "id": "ghost",
                        "index": 1,
                        "count": 2,
                        "updated": float("inf"),
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(table.owner_for_shard(1, now=100.0), (0, "master-0"))


class WorkLeaseJournalTests(unittest.TestCase):
    def test_rejects_nonfinite_spliced_and_unowned_journal_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "leases.jsonl")
            for invalid_ttl in (float("nan"), float("inf")):
                with self.subTest(invalid_ttl=invalid_ttl):
                    with self.assertRaisesRegex(ValueError, "finite"):
                        WorkLeaseJournal(path, lease_ttl=invalid_ttl)

            journal = WorkLeaseJournal(path, lease_ttl=30.0)
            payload = {"path": "seed", "target_branch": 17}
            other_payload = {"path": "other", "target_branch": 18}
            work_id = journal.work_id(payload)
            other_id = journal.work_id(other_payload)
            self.assertFalse(journal.lease(other_id, payload, worker=1, now=1.0))
            with self.assertRaisesRegex(ValueError, "timestamp"):
                journal.lease(work_id, payload, worker=1, now=float("inf"))
            self.assertFalse(journal.complete(work_id, now=2.0))
            self.assertNotIn(work_id, journal.completed)
            with self.assertRaisesRegex(ValueError, "recovery TTL"):
                journal.recover_expired(now=2.0, lease_ttl=float("nan"))
            self.assertFalse(Path(path).exists())

            valid_payload = {"path": "valid", "target_branch": 19}
            valid_id = journal.work_id(valid_payload)
            Path(path).write_text(
                "\n".join(
                    (
                        json.dumps(
                            {
                                "op": "lease",
                                "id": work_id,
                                "payload": other_payload,
                                "worker": 1,
                                "time": 10.0,
                                "attempts": 1,
                                "clock": "unix",
                            }
                        ),
                        (
                            '{"op":"lease","id":"%s","id":"%s",'
                            '"payload":{"path":"seed","target_branch":17},'
                            '"worker":1,"time":10.0,"attempts":1,'
                            '"clock":"unix"}'
                        )
                        % (work_id, valid_id),
                        (
                            '{"op":"lease","id":"%s",'
                            '"payload":{"path":"seed","target_branch":17},'
                            '"worker":1,"time":Infinity,"attempts":1,'
                            '"clock":"unix"}'
                        )
                        % work_id,
                        json.dumps(
                            {
                                "op": "lease",
                                "id": valid_id,
                                "payload": valid_payload,
                                "worker": 2,
                                "time": 11.0,
                                "attempts": 1,
                                "clock": "unix",
                            }
                        ),
                        json.dumps(
                            {"op": "done", "id": other_id, "time": 12.0}
                        ),
                    )
                )
                + "\n",
                encoding="utf-8",
            )

            restored = WorkLeaseJournal(path, lease_ttl=30.0)
            self.assertEqual(set(restored.leases), {valid_id})
            self.assertEqual(restored.completed, set())
            self.assertEqual(
                restored.recover_expired(now=50.0),
                [valid_payload],
            )

    def test_recovers_only_expired_unfinished_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "leases.jsonl")
            journal = WorkLeaseJournal(path, lease_ttl=30.0)
            first = {"path": "a", "focus_bytes": "", "target_branch": 11}
            second = {"path": "b", "focus_bytes": "0-3", "target_branch": 22}
            first_id = journal.work_id(first)
            second_id = journal.work_id(second)
            journal.lease(first_id, first, worker=1, now=10.0)
            journal.lease(second_id, second, worker=2, now=20.0)
            journal.complete(second_id, now=21.0)

            restored = WorkLeaseJournal(path, lease_ttl=30.0)
            self.assertEqual(restored.recover_expired(now=100.0), [first])
            self.assertEqual(restored.recover_expired(now=100.0), [])

    def test_ignores_corrupt_jsonl_tail_and_compacts_active_leases(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "leases.jsonl")
            active = {"path": "seed", "focus_bytes": "", "target_branch": 0}
            done = {"path": "old", "focus_bytes": "", "target_branch": 1}
            active_id = WorkLeaseJournal.work_id(active)
            done_id = WorkLeaseJournal.work_id(done)

            journal = WorkLeaseJournal(path, lease_ttl=1.0, compact_after=128)
            journal.lease(active_id, active, worker=1, now=10.0)
            journal.lease(done_id, done, worker=2, now=10.0)
            journal.complete(done_id, now=11.0)
            with open(path, "a", encoding="utf-8") as stream:
                stream.write("{not-json\n")

            restored = WorkLeaseJournal(path, lease_ttl=1.0, compact_after=128)
            self.assertIn(active_id, restored.leases)
            self.assertNotIn(done_id, restored.leases)
            restored.compact()

            compacted = WorkLeaseJournal(path, lease_ttl=1.0, compact_after=128)
            self.assertEqual(set(compacted.leases), {active_id})

    def test_abandon_is_persistent_and_append_failure_does_not_advance(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "leases.jsonl")
            journal = WorkLeaseJournal(path)
            payload = {"path": "seed", "target_branch": 17}
            work_id = journal.work_id(payload)
            self.assertTrue(journal.lease(work_id, payload, worker=1, now=10.0))
            self.assertTrue(journal.abandon(work_id, now=11.0))
            self.assertNotIn(work_id, journal.leases)
            restored = WorkLeaseJournal(path)
            self.assertNotIn(work_id, restored.leases)
            self.assertNotIn(work_id, restored.completed)

            with mock.patch.object(
                restored, "_append", side_effect=OSError("disk full")
            ):
                with self.assertRaisesRegex(OSError, "disk full"):
                    restored.lease(work_id, payload, worker=2, now=12.0)
            self.assertNotIn(work_id, restored.leases)

    def test_active_work_lease_rejects_duplicate_and_fences_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "leases.jsonl")
            journal = WorkLeaseJournal(path, lease_ttl=30.0)
            payload = {"path": "seed", "target_branch": 23}
            work_id = journal.work_id(payload)

            self.assertTrue(journal.lease(work_id, payload, worker=1, now=100.0))
            self.assertFalse(journal.lease(work_id, payload, worker=2, now=101.0))
            self.assertFalse(journal.abandon(work_id, worker=2, now=102.0))
            self.assertFalse(journal.complete(work_id, worker=2, now=103.0))
            self.assertEqual(journal.leases[work_id]["worker"], 1)

            self.assertTrue(journal.abandon(work_id, worker=1, now=104.0))
            self.assertTrue(journal.lease(work_id, payload, worker=2, now=105.0))
            self.assertFalse(journal.complete(work_id, worker=1, now=106.0))
            self.assertTrue(journal.complete(work_id, worker=2, now=107.0))
            self.assertIn(work_id, journal.completed)

    def test_recovery_migrates_legacy_clock_and_zero_ttl_is_unconditional(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "leases.jsonl")
            payload = {"path": "legacy", "target_branch": 5}
            work_id = WorkLeaseJournal.work_id(payload)
            Path(path).write_text(
                json.dumps(
                    {
                        "op": "lease",
                        "id": work_id,
                        "payload": payload,
                        "worker": 1,
                        "time": 9_000_000_000.0,
                        "attempts": 1,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            legacy = WorkLeaseJournal(path, lease_ttl=30.0)
            legacy.compact()
            legacy = WorkLeaseJournal(path, lease_ttl=30.0)
            self.assertEqual(legacy.leases[work_id]["clock"], "legacy")
            self.assertEqual(legacy.recover_expired(now=1.0), [payload])

            current = WorkLeaseJournal(path, lease_ttl=30.0)
            self.assertTrue(
                current.lease(work_id, payload, worker=2, now=9_000_000_000.0)
            )
            self.assertEqual(current.recover_expired(now=1.0, lease_ttl=0.0), [payload])
            rows = [
                json.loads(line)
                for line in Path(path).read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(rows[-1]["clock"], "unix")

    def test_work_lease_default_timestamp_uses_unix_clock(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = WorkLeaseJournal(os.path.join(tmp, "leases.jsonl"))
            payload = {"path": "seed"}
            work_id = journal.work_id(payload)
            with mock.patch("distributed_state.time.time", return_value=1234.5):
                self.assertTrue(journal.lease(work_id, payload, worker=1))
            self.assertEqual(journal.leases[work_id]["updated"], 1234.5)
            self.assertEqual(journal.leases[work_id]["clock"], "unix")


class FencedWorkLeaseTableTests(unittest.TestCase):
    def test_fencing_prevents_duplicate_active_claims_and_stale_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = FencedWorkLeaseTable(tmp, shard_count=4, lease_ttl=10.0)
            second = FencedWorkLeaseTable(tmp, shard_count=4, lease_ttl=10.0)
            payload = {"path": "seed", "target_branch": 0}
            work_id = WorkLeaseJournal.work_id(payload)

            token_a = first.claim(
                work_id, payload, owner="master-a", worker=1, now=100.0
            )
            self.assertIsNotNone(token_a)
            self.assertIsNone(
                second.claim(work_id, payload, owner="master-b", worker=2, now=105.0)
            )

            expired = second.recover_expired(now=120.0)
            self.assertEqual(expired, [payload])
            token_b = second.claim(
                work_id, payload, owner="master-b", worker=2, now=120.0
            )
            self.assertIsNotNone(token_b)
            self.assertNotEqual(token_a, token_b)
            self.assertFalse(first.complete(work_id, token_a or "", now=121.0))
            self.assertFalse(first.begin_commit(work_id, token_a or "", now=121.0))
            self.assertTrue(second.begin_commit(work_id, token_b or "", now=121.5))
            self.assertTrue(second.complete(work_id, token_b or "", now=122.0))
            self.assertIsNone(
                first.claim(work_id, payload, owner="master-a", worker=1, now=200.0)
            )

    def test_heartbeat_extends_active_fence(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = FencedWorkLeaseTable(tmp, shard_count=2, lease_ttl=10.0)
            payload = {"path": "seed", "focus_bytes": "0-3"}
            work_id = WorkLeaseJournal.work_id(payload)
            token = table.claim(work_id, payload, owner="master", worker=1, now=10.0)
            self.assertTrue(table.heartbeat(work_id, token or "", now=19.0))
            self.assertEqual(table.recover_expired(now=25.0), [])
            self.assertEqual(table.recover_expired(now=30.0), [payload])

    def test_heartbeat_many_syncs_each_affected_shard_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = FencedWorkLeaseTable(tmp, shard_count=4, lease_ttl=10.0)
            work_ids = (
                "00000000" + "a" * 56,
                "00000004" + "b" * 56,
                "00000001" + "c" * 56,
            )
            tokens = {
                work_id: table.claim(
                    work_id, {"path": work_id}, owner="master", now=10.0
                )
                for work_id in work_ids
            }
            self.assertTrue(all(tokens.values()))

            state = sys.modules["distributed_state"]
            with mock.patch(
                "distributed_state.fsync_directory", wraps=state.fsync_directory
            ) as sync:
                result = table.heartbeat_many(tokens, now=19.0)

            self.assertEqual(result.renewed, tuple(sorted(work_ids)))
            self.assertEqual(result.lost, ())
            self.assertEqual(result.directory_syncs, 2)
            self.assertEqual(
                [call.args[0] for call in sync.call_args_list],
                [table.shard_dir(0), table.shard_dir(1)],
            )
            self.assertTrue(
                all(
                    table._read_record(work_id)["updated"] == 19.0
                    for work_id in work_ids
                )
            )

    def test_heartbeat_many_flushes_published_prefix_after_later_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = FencedWorkLeaseTable(tmp, shard_count=4, lease_ttl=10.0)
            first = "00000000" + "a" * 56
            second = "00000004" + "b" * 56
            tokens = {
                work_id: table.claim(
                    work_id, {"path": work_id}, owner="master", now=10.0
                )
                for work_id in (first, second)
            }
            original_write = table._write_record

            def fail_second(work_id, record, **kwargs):
                if work_id == second:
                    raise OSError("injected second-record failure")
                return original_write(work_id, record, **kwargs)

            state = sys.modules["distributed_state"]
            with (
                mock.patch.object(table, "_write_record", side_effect=fail_second),
                mock.patch(
                    "distributed_state.fsync_directory", wraps=state.fsync_directory
                ) as sync,
            ):
                with self.assertRaisesRegex(OSError, "injected second-record failure"):
                    table.heartbeat_many(tokens, now=19.0)

            self.assertEqual(table._read_record(first)["updated"], 19.0)
            self.assertEqual(table._read_record(second)["updated"], 10.0)
            self.assertEqual(
                [call.args[0] for call in sync.call_args_list],
                [table.shard_dir(0)],
            )

    def test_heartbeat_many_directory_failure_is_not_acknowledged(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = FencedWorkLeaseTable(tmp, shard_count=4, lease_ttl=10.0)
            work_ids = (
                "00000000" + "a" * 56,
                "00000004" + "b" * 56,
            )
            tokens = {
                work_id: table.claim(
                    work_id, {"path": work_id}, owner="master", now=10.0
                )
                for work_id in work_ids
            }

            with mock.patch(
                "distributed_state.fsync_directory",
                side_effect=OSError("directory barrier failed"),
            ) as sync:
                with self.assertRaisesRegex(OSError, "directory barrier failed"):
                    table.heartbeat_many(tokens, now=19.0)

            self.assertEqual(sync.call_count, 1)
            restarted = FencedWorkLeaseTable(tmp, shard_count=4, lease_ttl=10.0)
            self.assertTrue(
                all(
                    restarted._read_record(work_id)["updated"] == 19.0
                    for work_id in work_ids
                )
            )

    def test_heartbeat_many_classifies_stale_tokens_without_extra_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = FencedWorkLeaseTable(tmp, shard_count=4, lease_ttl=10.0)
            current = "00000000" + "a" * 56
            stale = "00000001" + "b" * 56
            current_token = table.claim(
                current, {"path": current}, owner="master", now=10.0
            )
            table.claim(stale, {"path": stale}, owner="master", now=10.0)

            state = sys.modules["distributed_state"]
            with mock.patch(
                "distributed_state.fsync_directory", wraps=state.fsync_directory
            ) as sync:
                result = table.heartbeat_many(
                    {
                        current: current_token or "",
                        stale: "stale-token",
                    },
                    now=19.0,
                )

            self.assertEqual(result.renewed, (current,))
            self.assertEqual(result.lost, (stale,))
            self.assertEqual(result.directory_syncs, 1)
            self.assertEqual(
                [call.args[0] for call in sync.call_args_list],
                [table.shard_dir(0)],
            )

    def test_committing_fence_is_irreversible_and_cannot_be_stolen(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = FencedWorkLeaseTable(tmp, shard_count=2, lease_ttl=10.0)
            second = FencedWorkLeaseTable(tmp, shard_count=2, lease_ttl=10.0)
            payload = {"path": "seed", "target_branch": 7}
            work_id = WorkLeaseJournal.work_id(payload)
            token_a = first.claim(work_id, payload, owner="a", worker=1, now=10.0)
            self.assertTrue(first.begin_commit(work_id, token_a or "", now=11.0))
            self.assertIsNone(
                second.claim(work_id, payload, owner="b", worker=2, now=15.0)
            )
            self.assertEqual(second.recover_expired(now=22.0), [])
            self.assertIsNone(
                second.claim(work_id, payload, owner="b", worker=2, now=22.0)
            )
            self.assertTrue(first.begin_commit(work_id, token_a or "", now=23.0))
            self.assertTrue(first.complete(work_id, token_a or "", now=24.0))
            self.assertEqual(
                first.snapshot_counts(now=100.0),
                {
                    "leased": 0,
                    "committing": 0,
                    "expired": 0,
                    "done": 1,
                },
            )

    def test_commit_manifest_is_durable_and_completion_is_classified_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = FencedWorkLeaseTable(tmp, shard_count=2, lease_ttl=10.0)
            helper = FencedWorkLeaseTable(tmp, shard_count=2, lease_ttl=10.0)
            payload = {"path": "seed", "origin": "generated"}
            work_id = WorkLeaseJournal.work_id(payload)
            manifest = {
                "schema": "test-result-commit-v1",
                "children": ["a" * 64, "b" * 64],
            }
            token = first.claim(work_id, payload, owner="master-a", now=10.0)
            self.assertTrue(
                first.begin_commit(work_id, token or "", commit=manifest, now=11.0)
            )
            self.assertFalse(
                first.begin_commit(
                    work_id, token or "", commit={"different": True}, now=12.0
                )
            )
            self.assertEqual(
                helper.recover_committing_records(),
                [
                    (work_id, payload, token, manifest),
                ],
            )
            self.assertEqual(
                helper.complete_once(work_id, token or "", now=13.0), "completed"
            )
            self.assertEqual(
                first.complete_once(work_id, token or "", now=14.0), "already"
            )
            self.assertEqual(
                first.complete_once(work_id, "wrong-token", now=15.0), "stale"
            )
            self.assertEqual(helper.recover_committing_records(), [])

    def test_commit_replay_serializes_publication_and_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = FencedWorkLeaseTable(tmp, shard_count=2, lease_ttl=10.0)
            helper = FencedWorkLeaseTable(tmp, shard_count=2, lease_ttl=10.0)
            payload = {"path": "seed", "origin": "generated"}
            work_id = WorkLeaseJournal.work_id(payload)
            manifest = {"schema": "test-result-commit-v1", "children": ["a"]}
            token = first.claim(work_id, payload, owner="master-a", now=10.0)
            self.assertTrue(first.begin_commit(
                work_id, token or "", commit=manifest, now=11.0
            ))

            published = []
            self.assertEqual(
                helper.replay_commit_once(
                    work_id,
                    token or "",
                    lambda observed: published.append(observed),
                    now=12.0,
                ),
                "completed",
            )
            self.assertEqual(published, [manifest])
            self.assertEqual(
                first.replay_commit_once(
                    work_id,
                    token or "",
                    lambda _observed: self.fail(
                        "an already completed WAL must not replay"
                    ),
                    now=13.0,
                ),
                "already",
            )

            retry_payload = {"path": "retry", "origin": "generated"}
            retry_id = WorkLeaseJournal.work_id(retry_payload)
            retry_token = first.claim(
                retry_id, retry_payload, owner="master-a", now=14.0
            )
            self.assertTrue(first.begin_commit(
                retry_id, retry_token or "", commit=manifest, now=15.0
            ))
            with self.assertRaisesRegex(OSError, "publication interrupted"):
                helper.replay_commit_once(
                    retry_id,
                    retry_token or "",
                    lambda _observed: (_ for _ in ()).throw(
                        OSError("publication interrupted")
                    ),
                    now=16.0,
                )
            self.assertEqual(
                helper.recover_committing_records(),
                [(retry_id, retry_payload, retry_token, manifest)],
            )

    def test_failed_atomic_replace_removes_temporary_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = FencedWorkLeaseTable(tmp, shard_count=2)
            work_id = "a" * 64
            with mock.patch.object(
                os, "replace", side_effect=OSError("storage offline")
            ):
                with self.assertRaisesRegex(OSError, "storage offline"):
                    table._write_record(work_id, {"status": "leased"})

            self.assertFalse(os.path.exists(table.record_path(work_id)))
            self.assertEqual(list(Path(tmp).rglob("*.tmp")), [])

    def test_directory_sync_failure_leaves_recoverable_fenced_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = FencedWorkLeaseTable(tmp, shard_count=2, lease_ttl=10.0)
            payload = {"path": "seed", "origin": "power-loss-test"}
            work_id = WorkLeaseJournal.work_id(payload)
            durable_makedirs(table.shard_dir(table.shard_for_work(work_id)))

            with mock.patch(
                "distributed_state.fsync_directory",
                side_effect=OSError("directory-offline"),
            ):
                with self.assertRaisesRegex(OSError, "directory-offline"):
                    table.claim(work_id, payload, owner="master-a", now=10.0)

            restarted = FencedWorkLeaseTable(tmp, shard_count=2, lease_ttl=10.0)
            records = restarted.snapshot_records()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0][0], work_id)
            self.assertEqual(records[0][1]["payload"], payload)
            self.assertEqual(records[0][1]["status"], "leased")
            self.assertEqual(restarted.recover_expired(now=19.0), [])
            self.assertEqual(restarted.recover_expired(now=20.0), [payload])

    def test_abandon_requires_current_precommit_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = FencedWorkLeaseTable(tmp, shard_count=2, lease_ttl=10.0)
            payload = {"path": "seed", "target_branch": 9}
            work_id = WorkLeaseJournal.work_id(payload)
            first = table.claim(work_id, payload, owner="first", worker=1, now=10.0)
            self.assertFalse(table.abandon(work_id, "wrong-token"))
            self.assertTrue(table.abandon(work_id, first or ""))
            self.assertFalse(os.path.exists(table.record_path(work_id)))

            second = table.claim(work_id, payload, owner="second", worker=2, now=11.0)
            self.assertTrue(table.begin_commit(work_id, second or "", now=12.0))
            self.assertFalse(table.abandon(work_id, second or ""))
            self.assertTrue(os.path.exists(table.record_path(work_id)))

    def test_record_identity_and_timestamps_are_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = FencedWorkLeaseTable(tmp, shard_count=2, lease_ttl=10.0)
            work_id = "c" * 64
            redirected = "d" * 64
            table._write_record(
                work_id,
                {
                    "schema": 1,
                    "id": redirected,
                    "status": "leased",
                    "payload": {"path": "wrong"},
                    "owner": "master",
                    "token": "token",
                    "updated": 1.0,
                },
            )
            self.assertIsNone(
                table.claim(work_id, {"path": "replacement"}, owner="other", now=100.0)
            )
            self.assertEqual(table.recover_expired(now=100.0), [])

            clean_id = "e" * 64
            with self.assertRaisesRegex(ValueError, "timestamp must be finite"):
                table.claim(
                    clean_id, {"path": "seed"}, owner="master", now=float("nan")
                )
            token = table.claim(clean_id, {"path": "seed"}, owner="master", now=100.0)
            self.assertTrue(token)
            with self.assertRaisesRegex(ValueError, "TTL must be finite"):
                table.recover_expired(now=2.0, lease_ttl=float("inf"))

            self.assertIsNone(table.claim(None, {"path": "seed"}, owner="master"))

            self.assertEqual(
                table.recover_expired_records(now=2.0, lease_ttl=0.0),
                [
                    (clean_id, {"path": "seed"}),
                ],
            )
            replacement = table.claim(
                clean_id,
                {"path": "seed"},
                owner="recovery",
                now=2.0,
                lease_ttl=0.0,
            )
            self.assertTrue(replacement)
            self.assertNotEqual(replacement, token)
            self.assertFalse(table.complete(clean_id, token or ""))

    def test_committing_recovery_fails_closed_without_durable_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = FencedWorkLeaseTable(tmp, shard_count=2)
            work_id = "f" * 64
            token = table.claim(work_id, {"path": "seed"}, owner="master", now=1.0)
            self.assertTrue(table.begin_commit(work_id, token or "", now=2.0))

            with self.assertRaisesRegex(ValueError, "lacks durable manifest"):
                table.recover_committing_records()

            table._write_record(work_id, {"schema": 0})
            with self.assertRaisesRegex(ValueError, "invalid fenced lease record"):
                table.recover_committing_records()

    def test_lock_acquisition_is_bounded_and_never_steals_by_wall_clock(self):
        with tempfile.TemporaryDirectory() as tmp:
            holder = FencedWorkLeaseTable(
                tmp,
                shard_count=2,
                lock_ttl=1.0,
                lock_acquire_timeout=1.0,
            )
            contender = FencedWorkLeaseTable(
                tmp,
                shard_count=2,
                lock_ttl=1.0,
                lock_acquire_timeout=0.01,
            )
            work_id = "b" * 64
            with holder._locked(work_id):
                os.utime(holder.lock_path(work_id), (1.0, 1.0))
                started = time.monotonic()
                with self.assertRaisesRegex(
                    TimeoutError,
                    "configured age hint=1.000s; kernel advisory locks "
                    "are never time-stolen",
                ):
                    contender.claim(work_id, {"path": "seed"}, owner="master")
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertTrue(os.path.isfile(holder.lock_path(work_id)))

            self.assertIsNotNone(
                contender.claim(work_id, {"path": "seed"}, owner="master", now=1.0)
            )

    def test_fenced_lock_is_released_after_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = FencedWorkLeaseTable(tmp, shard_count=2, lock_acquire_timeout=0.1)
            work_id = "a" * 64

            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                with table._locked(work_id):
                    raise RuntimeError("injected failure")

            with table._locked(work_id):
                self.assertTrue(os.path.isfile(table.lock_path(work_id)))

    def test_fenced_lock_is_reclaimed_after_sigkill_without_finally(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = FencedWorkLeaseTable(tmp, shard_count=2, lock_acquire_timeout=0.5)
            work_id = "c" * 64
            child_code = """
import sys
import time
sys.path.insert(0, sys.argv[1])
from distributed_state import FencedWorkLeaseTable
table = FencedWorkLeaseTable(
    sys.argv[2], shard_count=2, lock_acquire_timeout=1.0)
with table._locked(sys.argv[3]):
    print("L", flush=True)
    time.sleep(60.0)
"""
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    child_code,
                    str(ROOT / "util"),
                    tmp,
                    work_id,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                assert child.stdout is not None
                readable, _writable, _errors = select.select(
                    [child.stdout], [], [], 5.0
                )
                self.assertEqual(readable, [child.stdout])
                self.assertEqual(child.stdout.readline().strip(), "L")
                child.kill()
                self.assertEqual(child.wait(timeout=5.0), -9)

                started = time.monotonic()
                with table._locked(work_id):
                    pass
                self.assertLess(time.monotonic() - started, 0.5)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5.0)
                if child.stdout is not None:
                    child.stdout.close()
                if child.stderr is not None:
                    child.stderr.close()

    def test_fenced_lock_rejects_nonregular_lock_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = FencedWorkLeaseTable(tmp, shard_count=2)
            work_id = "d" * 64
            os.makedirs(table.lock_path(work_id))

            with self.assertRaises(IsADirectoryError):
                table.claim(work_id, {"path": "seed"}, owner="master")

            self.assertTrue(os.path.isdir(table.lock_path(work_id)))

    def test_fenced_lock_never_falls_back_when_flock_is_unsupported(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = FencedWorkLeaseTable(tmp, shard_count=2)
            work_id = "e" * 64
            unsupported = OSError(errno.EOPNOTSUPP, "filesystem locking unsupported")

            with mock.patch("distributed_state.fcntl.flock", side_effect=unsupported):
                with self.assertRaisesRegex(OSError, "filesystem locking unsupported"):
                    table.claim(work_id, {"path": "seed"}, owner="master")

            self.assertIsNotNone(
                table.claim(work_id, {"path": "seed"}, owner="master", now=1.0)
            )

    def test_fenced_table_rejects_unbounded_duration_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "lease TTL must be"):
                FencedWorkLeaseTable(tmp, lease_ttl=float("inf"))
            with self.assertRaisesRegex(ValueError, "lock age hint must be"):
                FencedWorkLeaseTable(tmp, lock_ttl=float("nan"))
            with self.assertRaisesRegex(ValueError, "lock acquisition timeout must be"):
                FencedWorkLeaseTable(tmp, lock_acquire_timeout=float("inf"))


class FencedTargetLeaseTableTests(unittest.TestCase):
    def test_overlapping_groups_conflict_without_partial_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = FencedTargetLeaseTable(tmp, shard_count=4, lease_ttl=10.0)
            second = FencedTargetLeaseTable(tmp, shard_count=4, lease_ttl=10.0)

            token_a = first.claim_group(
                (11, 22), {"path": "seed-a"}, owner="master-a", now=100.0
            )
            self.assertIsNotNone(token_a)
            self.assertIsNone(
                second.claim_group(
                    (22, 33), {"path": "seed-b"}, owner="master-b", now=105.0
                )
            )
            self.assertFalse(os.path.exists(second.record_path(second.target_id(33))))
            self.assertEqual(first.snapshot_counts(now=105.0)["leased"], 2)

            self.assertTrue(first.release_group((11, 22), token_a or ""))
            token_b = second.claim_group(
                (22, 33), {"path": "seed-b"}, owner="master-b", now=106.0
            )
            self.assertIsNotNone(token_b)

    def test_heartbeat_expiry_and_stale_release_are_fenced(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = FencedTargetLeaseTable(tmp, shard_count=4, lease_ttl=10.0)
            second = FencedTargetLeaseTable(tmp, shard_count=4, lease_ttl=10.0)
            group = (77, 88, 77, 0)
            token_a = first.claim_group(
                group, {"path": "seed-a"}, owner="master-a", now=10.0
            )
            self.assertTrue(first.heartbeat_group(group, token_a or "", now=19.0))
            self.assertIsNone(
                second.claim_group(
                    (88, 77), {"path": "seed-b"}, owner="master-b", now=25.0
                )
            )

            token_b = second.claim_group(
                (88, 77), {"path": "seed-b"}, owner="master-b", now=30.0
            )
            self.assertIsNotNone(token_b)
            self.assertFalse(first.release_group(group, token_a or ""))
            for target in (77, 88):
                record = second._read_record(second.target_id(target))
                self.assertEqual(record["token"], token_b)
            self.assertTrue(second.release_group((77, 88), token_b or ""))
            self.assertIsNotNone(
                first.claim_group(
                    (77, 88), {"path": "seed-c"}, owner="master-a", now=31.0
                )
            )

    def test_target_group_heartbeat_syncs_each_affected_shard_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = FencedTargetLeaseTable(tmp, shard_count=4, lease_ttl=10.0)
            group = tuple(range(101, 109))
            token = table.claim_group(group, {"path": "seed"}, owner="master", now=10.0)
            target_ids = table._target_ids(group)
            expected_directories = {
                table.shard_dir(table.shard_for_work(target_id))
                for _target, target_id in target_ids
            }

            state = sys.modules["distributed_state"]
            with mock.patch(
                "distributed_state.fsync_directory", wraps=state.fsync_directory
            ) as sync:
                self.assertTrue(table.heartbeat_group(group, token or "", now=19.0))

            observed = [call.args[0] for call in sync.call_args_list]
            self.assertEqual(len(observed), len(expected_directories))
            self.assertEqual(set(observed), expected_directories)

    def test_target_group_rejects_nonfinite_time_and_corrupt_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = FencedTargetLeaseTable(tmp, shard_count=4, lease_ttl=10.0)
            with self.assertRaisesRegex(ValueError, "timestamp must be finite"):
                table.claim_group(
                    (101,), {"path": "seed"}, owner="master", now=float("nan")
                )

            target_id = table.target_id(101)
            table._write_record(
                target_id,
                {
                    "schema": 0,
                    "id": target_id,
                    "status": "leased",
                    "payload": {},
                    "token": "corrupt",
                    "updated": 10.0,
                },
            )
            self.assertIsNone(
                table.claim_group(
                    (101,), {"path": "replacement"}, owner="other", now=20.0
                )
            )
            self.assertFalse(table.heartbeat_group((101,), "corrupt", now=20.0))

    def test_failed_group_publication_rolls_back_written_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = FencedTargetLeaseTable(tmp, shard_count=4, lease_ttl=10.0)
            original_write = table._write_record
            writes = 0

            def fail_second(work_id, record, **kwargs):
                nonlocal writes
                writes += 1
                if writes == 2:
                    raise OSError("injected publication failure")
                original_write(work_id, record, **kwargs)

            with mock.patch.object(table, "_write_record", side_effect=fail_second):
                token = table.claim_group(
                    (101, 202), {"path": "seed"}, owner="master", now=10.0
                )

            self.assertIsNone(token)
            for target in (101, 202):
                self.assertFalse(
                    os.path.exists(table.record_path(table.target_id(target)))
                )

    def test_group_claim_rejects_unserializable_payload_before_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = FencedTargetLeaseTable(tmp, shard_count=4, lease_ttl=10.0)
            group = (111, 222, 333)

            with self.assertRaises(TypeError):
                table.claim_group(
                    group,
                    {"unsupported": object()},
                    owner="master",
                    now=10.0,
                )

            for target in group:
                self.assertFalse(
                    os.path.exists(table.record_path(table.target_id(target)))
                )

    def test_group_claim_syncs_each_affected_shard_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = FencedTargetLeaseTable(tmp, shard_count=4, lease_ttl=10.0)
            group = tuple(range(101, 109))
            target_ids = table._target_ids(group)
            expected_directories = {
                table.shard_dir(table.shard_for_work(target_id))
                for _target, target_id in target_ids
            }
            for directory in expected_directories:
                os.makedirs(directory, exist_ok=True)

            state = sys.modules["distributed_state"]
            with mock.patch(
                "distributed_state.fsync_directory", wraps=state.fsync_directory
            ) as sync:
                token = table.claim_group(
                    group, {"path": "seed"}, owner="master", now=10.0
                )

            self.assertIsNotNone(token)
            observed = [call.args[0] for call in sync.call_args_list]
            self.assertEqual(len(observed), len(expected_directories))
            self.assertEqual(set(observed), expected_directories)

    def test_group_claim_directory_failure_rolls_back_every_member(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = FencedTargetLeaseTable(tmp, shard_count=4, lease_ttl=10.0)
            group = tuple(range(101, 109))
            target_ids = table._target_ids(group)
            expected_directories = {
                table.shard_dir(table.shard_for_work(target_id))
                for _target, target_id in target_ids
            }
            for directory in expected_directories:
                os.makedirs(directory, exist_ok=True)

            real_sync = sys.modules["distributed_state"].fsync_directory
            calls = 0

            def fail_first(directory):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise OSError("injected directory barrier failure")
                real_sync(directory)

            with mock.patch(
                "distributed_state.fsync_directory", side_effect=fail_first
            ):
                token = table.claim_group(
                    group, {"path": "seed"}, owner="master", now=10.0
                )

            self.assertIsNone(token)
            self.assertEqual(calls, len(expected_directories) + 1)
            for _target, target_id in target_ids:
                self.assertFalse(os.path.exists(table.record_path(target_id)))

    def test_stale_group_release_is_a_zero_mutation_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = FencedTargetLeaseTable(tmp, shard_count=4, lease_ttl=10.0)
            group = (401, 402, 403)
            token = table.claim_group(group, {"path": "seed"}, owner="master", now=10.0)
            target_ids = table._target_ids(group)
            last_target_id = target_ids[-1][1]
            changed = table._read_record(last_target_id)
            self.assertIsNotNone(changed)
            assert changed is not None
            changed["token"] = "replacement-token"
            table._write_record(last_target_id, changed)
            snapshots = {
                target_id: Path(table.record_path(target_id)).read_bytes()
                for _target, target_id in target_ids
            }

            self.assertFalse(table.release_group(group, token or ""))

            self.assertEqual(
                {
                    target_id: Path(table.record_path(target_id)).read_bytes()
                    for _target, target_id in target_ids
                },
                snapshots,
            )

    def test_group_release_syncs_each_affected_shard_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = FencedTargetLeaseTable(tmp, shard_count=4, lease_ttl=10.0)
            group = tuple(range(101, 109))
            token = table.claim_group(group, {"path": "seed"}, owner="master", now=10.0)
            target_ids = table._target_ids(group)
            expected_directories = {
                table.shard_dir(table.shard_for_work(target_id))
                for _target, target_id in target_ids
            }

            state = sys.modules["distributed_state"]
            with mock.patch(
                "distributed_state.fsync_directory", wraps=state.fsync_directory
            ) as sync:
                released = table.release_group(group, token or "")

            self.assertTrue(released)
            observed = [call.args[0] for call in sync.call_args_list]
            self.assertEqual(len(observed), len(expected_directories))
            self.assertEqual(set(observed), expected_directories)

    def test_group_release_unlink_failure_restores_removed_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = FencedTargetLeaseTable(tmp, shard_count=4, lease_ttl=10.0)
            group = (501, 502, 503)
            token = table.claim_group(group, {"path": "seed"}, owner="master", now=10.0)
            target_ids = table._target_ids(group)
            snapshots = {
                target_id: Path(table.record_path(target_id)).read_bytes()
                for _target, target_id in target_ids
            }
            real_unlink = os.unlink
            calls = 0

            def fail_second(path):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected unlink failure")
                real_unlink(path)

            with mock.patch("distributed_state.os.unlink", side_effect=fail_second):
                released = table.release_group(group, token or "")

            self.assertFalse(released)
            self.assertEqual(
                {
                    target_id: Path(table.record_path(target_id)).read_bytes()
                    for _target, target_id in target_ids
                },
                snapshots,
            )

    def test_group_release_directory_failure_restores_every_member(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = FencedTargetLeaseTable(tmp, shard_count=4, lease_ttl=10.0)
            group = tuple(range(601, 609))
            token = table.claim_group(group, {"path": "seed"}, owner="master", now=10.0)
            target_ids = table._target_ids(group)
            expected_directories = {
                table.shard_dir(table.shard_for_work(target_id))
                for _target, target_id in target_ids
            }
            snapshots = {
                target_id: Path(table.record_path(target_id)).read_bytes()
                for _target, target_id in target_ids
            }
            real_sync = sys.modules["distributed_state"].fsync_directory
            calls = 0

            def fail_first(directory):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise OSError("injected directory barrier failure")
                real_sync(directory)

            with mock.patch(
                "distributed_state.fsync_directory", side_effect=fail_first
            ):
                released = table.release_group(group, token or "")

            self.assertFalse(released)
            self.assertEqual(calls, len(expected_directories) + 1)
            self.assertEqual(
                {
                    target_id: Path(table.record_path(target_id)).read_bytes()
                    for _target, target_id in target_ids
                },
                snapshots,
            )

    def test_failed_group_heartbeat_restores_one_expiry_point(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = FencedTargetLeaseTable(tmp, shard_count=4, lease_ttl=10.0)
            group = (303, 404)
            token = table.claim_group(group, {"path": "seed"}, owner="master", now=10.0)
            original_write = table._write_record
            writes = 0

            def fail_second_once(work_id, record, **kwargs):
                nonlocal writes
                writes += 1
                if writes == 2:
                    raise OSError("injected heartbeat failure")
                original_write(work_id, record, **kwargs)

            with mock.patch.object(
                table, "_write_record", side_effect=fail_second_once
            ):
                self.assertFalse(table.heartbeat_group(group, token or "", now=19.0))

            for target in group:
                record = table._read_record(table.target_id(target))
                self.assertEqual(record["updated"], 10.0)
            self.assertIsNotNone(
                table.claim_group(group, {"path": "retry"}, owner="other", now=20.0)
            )


class StateShardCoordinatorTests(unittest.TestCase):
    def test_state_task_id_uses_digest_focus_target_and_actions(self):
        coord = StateShardCoordinator(shard_count=8, worker_count=4)
        digest = "a" * 64
        item = ("seed", "0-3", 123, ((77, "solve"), (88, "sample")))
        first = coord.describe_item(item, sha256=digest)
        second = coord.describe_item(
            ("other-path", "0-3", 123, [[77, "solve"], [88, "sample"]]),
            sha256=digest,
        )
        self.assertEqual(first["state_task_id"], second["state_task_id"])
        self.assertEqual(first["state_payload"]["input"], digest)
        self.assertEqual(first["state_payload"]["path"], "")

    def test_state_task_id_distinguishes_schedule_prefixes(self):
        coord = StateShardCoordinator(shard_count=8, worker_count=4)
        digest = "b" * 64
        first = coord.describe_item(("seed", None, 0, (), (1,)), sha256=digest)
        second = coord.describe_item(("seed", None, 0, (), (2,)), sha256=digest)
        self.assertNotEqual(first["state_task_id"], second["state_task_id"])
        self.assertEqual(first["state_payload"]["schedule_prefix"], [1])

    def test_live_continuation_descriptor_is_canonical_state_identity(self):
        coord = StateShardCoordinator(shard_count=8, worker_count=4)
        digest = "c" * 64
        root = "d" * 64
        descriptor = {
            "schema": "symcc-live-continuation-v1",
            "engine": "symcc",
            "frames": [
                {
                    "function": "parse",
                    "block": "dispatch",
                    "instruction": 17,
                    "call_depth": 1,
                }
            ],
            "path_condition_root": root,
            "symbolic_store_root": "e" * 64,
            "symbolic_memory_root": "f" * 64,
            "target_branch": 42,
        }
        checkpoint = LiveContinuationDescriptor.from_mapping(descriptor)
        self.assertIsNotNone(checkpoint)
        checkpoint_id = checkpoint.checkpoint_id()

        first = coord.describe_item(
            ("seed-a", "0-3", 42, (), (), descriptor),
            sha256=digest,
        )
        second = coord.describe_item(
            ("seed-b", "0-3", 42, (), (), dict(descriptor)),
            sha256=digest,
        )
        self.assertEqual(first["state_task_id"], second["state_task_id"])
        self.assertEqual(first["state_payload"]["continuation_id"], checkpoint_id)
        self.assertEqual(
            first["state_payload"]["continuation"]["path_condition_root"], root
        )

        moved = dict(descriptor)
        moved["frames"] = [dict(descriptor["frames"][0], instruction=18)]
        third = coord.describe_item(
            ("seed-a", "0-3", 42, (), (), moved),
            sha256=digest,
        )
        self.assertNotEqual(first["state_task_id"], third["state_task_id"])

    def test_worker_index_prefers_owned_shard_then_steals(self):
        coord = StateShardCoordinator(shard_count=16, worker_count=4, steal_window=8)
        queue = [(f"seed-{i}", None, i, ()) for i in range(1, 12)]
        index = coord.work_index(2, queue, 0, active_worker_count=4)
        self.assertIsNotNone(index)
        meta = coord.describe_item(queue[index], active_worker_count=4)
        self.assertEqual(meta["state_owner"], 2)

        no_owned = [
            item
            for item in queue
            if coord.describe_item(item, active_worker_count=4)["state_owner"] != 3
        ]
        steal = coord.work_index(3, no_owned, 0, active_worker_count=4)
        self.assertIsNotNone(steal)
        self.assertGreaterEqual(steal, 0)

    def test_leases_completion_snapshot_and_expiry(self):
        coord = StateShardCoordinator(shard_count=4, worker_count=2, lease_ttl=5.0)
        meta = coord.describe_item(("seed", None, 42, ()))
        task_id = meta["state_task_id"]
        coord.lease(task_id, worker=1, now=10.0)
        self.assertEqual(coord.recover_expired(now=12.0), [])
        self.assertEqual(coord.recover_expired(now=20.0), [task_id])

        coord.lease(task_id, worker=2, now=21.0)
        coord.complete(task_id, reward=1.0, generated=3, elapsed=0.5, now=22.0)
        snapshot = coord.to_mapping()
        restored = StateShardCoordinator(shard_count=4, worker_count=2)
        restored.restore(snapshot)
        self.assertEqual(restored.stats[task_id].completions, 1)
        self.assertEqual(restored.stats[task_id].total_generated, 3)
        self.assertNotIn(task_id, restored.leases)

    def test_abandon_and_discard_distinguish_unsent_from_stale_result(self):
        coord = StateShardCoordinator(shard_count=4, worker_count=2)
        task_id = coord.describe_item(("seed", None, 42, ()))["state_task_id"]
        coord.lease(task_id, worker=1, now=10.0)
        self.assertFalse(coord.abandon(task_id, worker=2))
        self.assertTrue(coord.abandon(task_id, worker=1))
        self.assertEqual(coord.stats[task_id].leases, 0)
        self.assertEqual(coord.stats[task_id].failures, 0)

        coord.lease(task_id, worker=2, now=20.0)
        self.assertTrue(coord.discard(task_id, worker=2, now=21.0))
        self.assertEqual(coord.stats[task_id].leases, 1)
        self.assertEqual(coord.stats[task_id].failures, 1)
        self.assertEqual(coord.stats[task_id].last_complete, 21.0)

    def test_state_lease_rejects_duplicate_and_fences_completion_owner(self):
        coord = StateShardCoordinator(shard_count=4, worker_count=2)
        task_id = coord.describe_item(("seed", None, 42, ()))["state_task_id"]
        self.assertTrue(coord.lease(task_id, worker=1, now=100.0))
        self.assertFalse(coord.lease(task_id, worker=2, now=101.0))
        self.assertFalse(
            coord.complete(task_id, worker=2, reward=1.0, generated=3, now=102.0)
        )
        self.assertIn(task_id, coord.leases)
        self.assertEqual(coord.stats[task_id].completions, 0)
        self.assertTrue(
            coord.complete(task_id, worker=1, reward=1.0, generated=3, now=103.0)
        )
        self.assertNotIn(task_id, coord.leases)
        self.assertEqual(coord.stats[task_id].completions, 1)

    def test_restored_state_leases_are_stale_across_coordinator_epoch(self):
        coord = StateShardCoordinator(shard_count=4, worker_count=2, lease_ttl=30.0)
        task_id = coord.describe_item(("seed", None, 42, ()))["state_task_id"]
        self.assertTrue(coord.lease(task_id, worker=1, now=9_000_000_000.0))
        snapshot = coord.to_mapping()
        self.assertEqual(snapshot["clock"], "unix")

        restored = StateShardCoordinator(shard_count=4, worker_count=2, lease_ttl=30.0)
        restored.restore(snapshot)
        self.assertEqual(restored.leases[task_id], (1, 0.0))
        self.assertEqual(restored.recover_expired(now=31.0), [task_id])

        legacy_snapshot = dict(snapshot)
        legacy_snapshot.pop("clock")
        legacy = StateShardCoordinator(shard_count=4, worker_count=2, lease_ttl=30.0)
        legacy.restore(legacy_snapshot)
        self.assertEqual(legacy.stats[task_id].last_lease, 0.0)
        self.assertEqual(legacy.leases[task_id], (1, 0.0))


class LiveStateStoreTests(unittest.TestCase):
    def test_restore_is_transitively_validated_and_graph_budgeted(self):
        state = sys.modules["distributed_state"]
        with tempfile.TemporaryDirectory() as tmp:
            store = LiveStateStore(
                tmp,
                page_size=64,
                max_graph_objects=32,
                max_graph_bytes=1024 * 1024,
            )
            expression = store.put_expression(
                {
                    "op": "equal",
                    "children": ["input[0]", 65],
                }
            )
            solver = store.put_solver_frame("", [expression, expression])
            symbolic_store = store.put_symbolic_store(
                {
                    "left": expression,
                    "right": expression,
                }
            )
            memory = store.create_memory(
                b"A" * 64,
                {0: expression, 1: expression},
            )
            page = dict(store.memory_pages(memory))[0]
            program = store.put_program(
                {
                    "entry": "main",
                    "functions": {},
                }
            )
            parent_descriptor = LiveContinuationDescriptor.from_mapping(
                {
                    "schema": "symcc-live-continuation-v1",
                    "engine": "symcc-continuation-ir",
                    "path_condition_root": solver,
                    "symbolic_store_root": symbolic_store,
                    "symbolic_memory_root": memory,
                    "program_root": program,
                }
            )
            assert parent_descriptor is not None
            parent = store.put_continuation(parent_descriptor)
            descriptor = LiveContinuationDescriptor.from_mapping(
                {
                    **parent_descriptor.to_mapping(),
                    "parent": parent,
                }
            )
            assert descriptor is not None
            checkpoint = store.put_continuation(descriptor)
            object_ids = {
                checkpoint,
                parent,
                expression,
                solver,
                symbolic_store,
                memory,
                page,
                program,
            }
            canonical_bytes = sum(
                Path(store.object_path(object_id)).stat().st_size
                for object_id in object_ids
            )

            with mock.patch.object(
                state,
                "stable_regular_file_snapshot",
                wraps=state.stable_regular_file_snapshot,
            ) as snapshots:
                bundle = store.restore_continuation(checkpoint)
            self.assertEqual(bundle.graph_object_count, len(object_ids))
            self.assertEqual(bundle.graph_canonical_bytes, canonical_bytes)
            self.assertEqual(snapshots.call_count, len(object_ids))

            object_limited = LiveStateStore(
                tmp,
                page_size=64,
                max_graph_objects=len(object_ids) - 1,
                max_graph_bytes=canonical_bytes,
            )
            with self.assertRaisesRegex(ValueError, "object budget"):
                object_limited.restore_continuation(checkpoint)

            byte_limited = LiveStateStore(
                tmp,
                page_size=64,
                max_graph_objects=len(object_ids),
                max_graph_bytes=canonical_bytes - 1,
            )
            with self.assertRaisesRegex(ValueError, "canonical-byte budget"):
                byte_limited.restore_continuation(checkpoint)

            exact_budget = LiveStateStore(
                tmp,
                page_size=64,
                max_graph_objects=len(object_ids),
                max_graph_bytes=canonical_bytes,
            )
            exact = exact_budget.restore_continuation(checkpoint)
            self.assertEqual(exact.graph_object_count, len(object_ids))
            self.assertEqual(exact.graph_canonical_bytes, canonical_bytes)

    def test_restore_rejects_deep_schema_and_semantic_cache_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LiveStateStore(tmp, page_size=64)
            expression = store.put_expression({"op": "bool", "value": True})
            malformed_memory = store._put_mapping(
                {
                    "schema": "symcc-live-memory-root-v1",
                    "page_size": 64,
                    "size": 64,
                    "pages": [[0, expression]],
                }
            )
            memory_descriptor = LiveContinuationDescriptor.from_mapping(
                {
                    "schema": "symcc-live-continuation-v1",
                    "engine": "symcc",
                    "symbolic_memory_root": malformed_memory,
                }
            )
            assert memory_descriptor is not None
            memory_checkpoint = store.put_continuation(memory_descriptor)
            with self.assertRaisesRegex(ValueError, "memory-page-v1"):
                store.restore_continuation(memory_checkpoint)
            self.assertNotIn(expression, store._object_store._verified_identities)

            duplicate_store = store._put_mapping(
                {
                    "schema": "symcc-live-symbolic-store-v1",
                    "entries": [["x", expression], ["x", expression]],
                }
            )
            store_descriptor = LiveContinuationDescriptor.from_mapping(
                {
                    "schema": "symcc-live-continuation-v1",
                    "engine": "symcc",
                    "symbolic_store_root": duplicate_store,
                }
            )
            assert store_descriptor is not None
            store_checkpoint = store.put_continuation(store_descriptor)
            with self.assertRaisesRegex(ValueError, "symbolic store reference"):
                store.restore_continuation(store_checkpoint)

            invalid_content = b"not-json"
            invalid_id = store._digest(invalid_content)
            invalid_path = Path(store.object_path(invalid_id))
            invalid_path.parent.mkdir(parents=True, exist_ok=True)
            invalid_path.write_bytes(invalid_content)
            with self.assertRaisesRegex(ValueError, "invalid live-state JSON"):
                store._get_mapping(invalid_id)
            self.assertNotIn(invalid_id, store._object_store._verified_identities)

    def test_graph_budget_configuration_requires_positive_integers(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, value in (
                ("max_graph_objects", 0),
                ("max_graph_objects", True),
                ("max_graph_bytes", -1),
                ("max_graph_bytes", 1.5),
            ):
                with self.subTest(name=name, value=value):
                    with self.assertRaisesRegex(ValueError, name):
                        LiveStateStore(tmp, **{name: value})

    def test_live_objects_repair_corruption_and_never_follow_symlinks(self):
        state = sys.modules["distributed_state"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = LiveStateStore(tmp, page_size=64)
            expression = {"op": "bool", "value": True}
            normalized = {
                "schema": "symcc-live-expression-v1",
                "expression": expression,
            }
            content = store._canonical(normalized)
            object_id = store._digest(content)
            path = Path(store.object_path(object_id))
            path.parent.mkdir(parents=True, exist_ok=True)

            outside = root / "outside-exact.json"
            outside.write_bytes(content)
            path.symlink_to(outside)
            observed = store.put_expression(expression)
            self.assertEqual(observed, object_id)
            self.assertFalse(path.is_symlink())
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual(outside.read_bytes(), content)

            path.write_bytes(b"x" * len(content))
            self.assertEqual(store.put_expression(expression), object_id)
            self.assertEqual(path.read_bytes(), content)

            with mock.patch.object(
                state,
                "stable_regular_file_snapshot",
                wraps=state.stable_regular_file_snapshot,
            ) as snapshot:
                self.assertEqual(store.get_expression(object_id), expression)
            snapshot.assert_called_once_with(
                object_id[2:] + ".json",
                max_bytes=store.max_object_bytes,
                retain_content=True,
                directory_fd=mock.ANY,
            )
            self.assertIn(object_id, store._object_store._verified_identities)

            path.unlink()
            path.symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "missing or unstable"):
                store.get_expression(object_id)
            self.assertFalse(store.has_object(object_id))
            self.assertNotIn(object_id, store._object_store._verified_identities)
            self.assertEqual(outside.read_bytes(), content)

    def test_live_read_rejects_replaced_shard_without_following_alias(self):
        state = sys.modules["distributed_state"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = LiveStateStore(str(root / "state"), page_size=64)
            expression = {"op": "bool", "value": True}
            object_id = store.put_expression(expression)
            object_path = Path(store.object_path(object_id))
            shard = object_path.parent
            leaf = object_path.name
            content = object_path.read_bytes()
            detached = root / "detached-live-shard"
            outside = root / "outside-live-shard"
            outside.mkdir()
            (outside / leaf).write_bytes(content)
            original_read = state.os.read
            replaced = False

            def replace_shard_after_first_read(descriptor, size):
                nonlocal replaced
                chunk = original_read(descriptor, size)
                if chunk and not replaced:
                    shard.rename(detached)
                    shard.symlink_to(outside, target_is_directory=True)
                    replaced = True
                return chunk

            with mock.patch.object(
                state.os, "read", side_effect=replace_shard_after_first_read
            ):
                with self.assertRaisesRegex(ValueError, "missing or unstable"):
                    store.get_expression(object_id)

            self.assertTrue(replaced)
            self.assertEqual((outside / leaf).read_bytes(), content)
            self.assertEqual((detached / leaf).read_bytes(), content)
            self.assertNotIn(object_id, store._object_store._verified_identities)

    def test_live_read_rejects_replaced_root_ancestor(self):
        state = sys.modules["distributed_state"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            namespace = root / "namespace"
            stable = namespace / "stable"
            store = LiveStateStore(str(stable / "state"), page_size=64)
            expression = {"op": "bool", "value": True}
            object_id = store.put_expression(expression)
            object_path = Path(store.object_path(object_id))
            content = object_path.read_bytes()
            relative_object = object_path.relative_to(stable)
            detached = root / "detached-live-ancestor"
            outside = root / "outside-live-ancestor"
            alias_object = outside / relative_object
            alias_object.parent.mkdir(parents=True)
            alias_object.write_bytes(content)
            original_read = state.os.read
            replaced = False

            def replace_ancestor_after_first_read(descriptor, size):
                nonlocal replaced
                chunk = original_read(descriptor, size)
                if chunk and not replaced:
                    stable.rename(detached)
                    stable.symlink_to(outside, target_is_directory=True)
                    replaced = True
                return chunk

            with mock.patch.object(
                state.os, "read", side_effect=replace_ancestor_after_first_read
            ):
                with self.assertRaisesRegex(ValueError, "missing or unstable"):
                    store.get_expression(object_id)

            self.assertTrue(replaced)
            self.assertEqual(alias_object.read_bytes(), content)
            self.assertEqual((detached / relative_object).read_bytes(), content)
            self.assertNotIn(object_id, store._object_store._verified_identities)

    def test_solver_stack_cow_memory_and_continuation_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LiveStateStore(tmp, page_size=64)
            first_expr = store.put_expression(
                {
                    "op": "equal",
                    "children": ["input[0]", 65],
                }
            )
            second_expr = store.put_expression(
                {
                    "op": "ult",
                    "children": ["input[1]", 10],
                }
            )
            frame_one = store.put_solver_frame("", [first_expr])
            frame_two = store.put_solver_frame(frame_one, [second_expr])
            symbolic_store = store.put_symbolic_store(
                {
                    "x": first_expr,
                    "y": second_expr,
                }
            )
            memory = store.create_memory(
                bytes(range(128)),
                {3: first_expr},
            )
            forked = store.fork_memory(
                memory,
                concrete_writes={70: b"\xaa\xbb"},
                symbolic_writes={71: second_expr},
            )
            self.assertEqual(store.memory_diff(memory, forked), (1,))
            original_pages = dict(store.memory_pages(memory))
            forked_pages = dict(store.memory_pages(forked))
            self.assertEqual(original_pages[0], forked_pages[0])
            self.assertNotEqual(original_pages[1], forked_pages[1])
            concrete, symbolic = store.read_memory(forked, 68, 6)
            self.assertEqual(concrete, bytes([68, 69, 0xAA, 0xBB, 72, 73]))
            self.assertEqual(symbolic, {3: second_expr})

            descriptor = LiveContinuationDescriptor.from_mapping(
                {
                    "schema": "symcc-live-continuation-v1",
                    "engine": "symcc",
                    "frames": [
                        {
                            "function": "parse",
                            "block": "dispatch",
                            "instruction": 17,
                            "call_depth": 1,
                        }
                    ],
                    "path_condition_root": frame_two,
                    "symbolic_store_root": symbolic_store,
                    "symbolic_memory_root": forked,
                    "target_branch": 42,
                }
            )
            self.assertIsNotNone(descriptor)
            assert descriptor is not None
            checkpoint_id = store.put_continuation(descriptor)
            restored = store.restore_continuation(checkpoint_id)
            self.assertEqual(restored.checkpoint_id, checkpoint_id)
            self.assertEqual(
                restored.solver_frames,
                ((first_expr,), (second_expr,)),
            )
            self.assertEqual(
                dict(restored.symbolic_store),
                {"x": first_expr, "y": second_expr},
            )
            self.assertEqual(restored.memory_size, 128)
            self.assertEqual(restored.memory_page_size, 64)
            self.assertEqual(
                restored.descriptor.target_branch,
                42,
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "util" / "symcc_live_state.py"),
                    tmp,
                    "--page-size",
                    "64",
                    "inspect",
                    checkpoint_id,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            inspected = json.loads(completed.stdout)
            self.assertTrue(inspected["verified"])
            self.assertFalse(inspected["native_resume_supported"])
            self.assertEqual(inspected["checkpoint_id"], checkpoint_id)
            self.assertEqual(
                inspected["graph_verification"]["unique_objects"],
                restored.graph_object_count,
            )
            self.assertEqual(
                inspected["graph_verification"]["canonical_bytes"],
                restored.graph_canonical_bytes,
            )

    def test_live_state_store_rejects_missing_and_tampered_objects(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LiveStateStore(tmp, page_size=64)
            expression = store.put_expression({"op": "bool", "value": True})
            with self.assertRaises(ValueError):
                store.put_solver_frame("", ["f" * 64])
            memory = store.create_memory(b"A" * 64, {0: expression})
            page_id = dict(store.memory_pages(memory))[0]
            Path(store.object_path(page_id)).write_text(
                '{"schema":"symcc-live-memory-page-v1"}',
                encoding="ascii",
            )
            with self.assertRaisesRegex(ValueError, "memory page|digest mismatch"):
                store.read_memory(memory, 0, 1)


class LiveContinuationExecutorTests(unittest.TestCase):
    @staticmethod
    def _program():
        return {
            "schema": "symcc-live-program-v1",
            "entry": "main",
            "input_size": 1,
            "memory_size": 2,
            "functions": {
                "main": {
                    "entry": "entry",
                    "blocks": {
                        "entry": [
                            {"op": "input", "dst": "byte", "offset": 0},
                            {
                                "op": "binary",
                                "operator": "eq",
                                "dst": "is_a",
                                "left": {"var": "byte"},
                                "right": {"const": 65, "bits": 8},
                                "bits": 1,
                            },
                            {
                                "op": "branch",
                                "condition": {"var": "is_a"},
                                "true": "hit",
                                "false": "miss",
                                "site": 99,
                            },
                        ],
                        "hit": [
                            {
                                "op": "store",
                                "address": 0,
                                "value": {"var": "byte"},
                            },
                            {
                                "op": "call",
                                "function": "increment",
                                "args": [{"var": "byte"}],
                                "dst": "result",
                            },
                            {"op": "halt", "value": {"var": "result"}},
                        ],
                        "miss": [
                            {"op": "const", "dst": "zero", "value": 0},
                            {
                                "op": "store",
                                "address": 0,
                                "value": {"var": "zero"},
                            },
                            {"op": "halt", "value": {"var": "zero"}},
                        ],
                    },
                },
                "increment": {
                    "entry": "entry",
                    "params": ["value"],
                    "blocks": {
                        "entry": [
                            {
                                "op": "binary",
                                "operator": "add",
                                "dst": "next",
                                "left": {"var": "value"},
                                "right": {"const": 1, "bits": 8},
                                "bits": 8,
                            },
                            {"op": "return", "value": {"var": "next"}},
                        ],
                    },
                },
            },
        }

    @staticmethod
    def _incremental_solver_program():
        return {
            "schema": "symcc-live-program-v1",
            "entry": "main",
            "input_size": 2,
            "functions": {
                "main": {
                    "entry": "entry",
                    "blocks": {
                        "entry": [
                            {"op": "input", "dst": "a", "offset": 0},
                            {"op": "input", "dst": "b", "offset": 1},
                            {
                                "op": "binary",
                                "operator": "eq",
                                "dst": "a_is_one",
                                "left": {"var": "a"},
                                "right": {"const": 1, "bits": 8},
                                "bits": 1,
                            },
                            {
                                "op": "branch",
                                "condition": {"var": "a_is_one"},
                                "true": "second",
                                "false": "second",
                            },
                        ],
                        "second": [
                            {
                                "op": "binary",
                                "operator": "eq",
                                "dst": "b_is_two",
                                "left": {"var": "b"},
                                "right": {"const": 2, "bits": 8},
                                "bits": 1,
                            },
                            {
                                "op": "branch",
                                "condition": {"var": "b_is_two"},
                                "true": "yes",
                                "false": "no",
                            },
                        ],
                        "yes": [{"op": "return", "value": 1}],
                        "no": [{"op": "return", "value": 0}],
                    },
                },
            },
        }

    def test_checkpoint_resume_forks_executes_calls_and_cow_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LiveStateStore(tmp, page_size=64)
            executor = LiveContinuationExecutor(store)
            root = executor.create(self._program(), input_bytes=b"A")
            initial = store.restore_continuation(root)
            self.assertTrue(initial.descriptor.program_root)

            paused = executor.resume(root, max_steps=2, max_states=8)
            self.assertTrue(paused["bounded"])
            self.assertEqual(len(paused["frontier"]), 1)
            checkpoint = paused["frontier"][0]
            restored = store.restore_continuation(checkpoint)
            self.assertEqual(restored.descriptor.frames[-1].instruction, 2)

            completed = executor.resume(checkpoint, max_steps=64, max_states=8)
            self.assertEqual(completed["forks"], 1)
            self.assertEqual(
                sorted(row["value"] for row in completed["halted"]),
                [0, 66],
            )
            self.assertEqual(len(completed["generated_checkpoints"]), 2)
            self.assertFalse(completed["frontier"])
            self.assertTrue(completed["continuation_ir_resume_supported"])
            self.assertFalse(completed["native_instruction_resume_supported"])
            hit = next(row for row in completed["halted"] if row["value"] == 66)
            _concrete, symbolic = store.read_memory(hit["memory_root"], 0, 1)
            self.assertIn(0, symbolic)

            program_path = Path(tmp) / "program.json"
            program_path.write_text(json.dumps(self._program()), encoding="utf-8")
            cli = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "util" / "symcc_live_state.py"),
                    tmp,
                    "--page-size",
                    "64",
                    "run-program",
                    str(program_path),
                    "--input-hex",
                    "41",
                    "--max-steps",
                    "64",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            cli_result = json.loads(cli.stdout)
            self.assertEqual(cli_result["forks"], 1)
            self.assertEqual(
                sorted(row["value"] for row in cli_result["halted"]),
                [0, 66],
            )

    def test_program_validation_rejects_missing_branch_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LiveStateStore(tmp, page_size=64)
            executor = LiveContinuationExecutor(store)
            program = self._program()
            program["functions"]["main"]["blocks"]["entry"][-1]["false"] = "missing"
            with self.assertRaisesRegex(ValueError, "branch target"):
                executor.create(program, input_bytes=b"A")

    def test_solver_prunes_derived_infeasible_branch(self):
        program = {
            "schema": "symcc-live-program-v1",
            "entry": "main",
            "input_size": 1,
            "functions": {
                "main": {
                    "entry": "entry",
                    "blocks": {
                        "entry": [
                            {"op": "input", "dst": "byte", "offset": 0},
                            {
                                "op": "binary",
                                "operator": "eq",
                                "dst": "is_65",
                                "left": {"var": "byte"},
                                "right": {"const": 65, "bits": 8},
                                "bits": 1,
                            },
                            {
                                "op": "branch",
                                "condition": {"var": "is_65"},
                                "true": "check_66",
                                "false": "other",
                            },
                        ],
                        "check_66": [
                            {
                                "op": "binary",
                                "operator": "eq",
                                "dst": "is_66",
                                "left": {"var": "byte"},
                                "right": {"const": 66, "bits": 8},
                                "bits": 1,
                            },
                            {
                                "op": "branch",
                                "condition": {"var": "is_66"},
                                "true": "impossible",
                                "false": "sixty_five",
                            },
                        ],
                        "impossible": [{"op": "return", "value": 1}],
                        "sixty_five": [{"op": "return", "value": 2}],
                        "other": [{"op": "return", "value": 0}],
                    },
                },
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            if executor._feasibility.name != "symcc-query-solver":
                self.skipTest("symcc-query-solver is unavailable")
            root = executor.create(program, input_bytes=b"A")
            result = executor.resume(root, max_steps=128, max_states=8)
        self.assertEqual(sorted(row["value"] for row in result["halted"]), [0, 2])
        self.assertGreaterEqual(result["infeasible_pruned"], 1)
        self.assertEqual(result["feasibility_unknown"], 0)

    def test_incremental_solver_reuses_exact_parent_and_resume_contexts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LiveStateStore(tmp, page_size=64)
            with LiveContinuationExecutor(store) as executor:
                if executor._feasibility.name != "symcc-query-solver":
                    self.skipTest("symcc-query-solver is unavailable")
                root = executor.create(
                    self._incremental_solver_program(),
                    input_bytes=b"\x01\x02",
                )
                result = executor.resume(root, max_steps=128, max_states=16)
                self.assertEqual(result["feasibility_mode"], "incremental")
                self.assertEqual(
                    sorted(row["value"] for row in result["halted"]),
                    [0, 0, 1, 1],
                )
                self.assertEqual(result["feasibility_checks"], 6)
                self.assertEqual(result["incremental_context_rebuilds"], 1)
                self.assertEqual(result["incremental_context_parent_hits"], 2)
                self.assertEqual(result["incremental_context_exact_hits"], 3)
                self.assertEqual(result["incremental_server_starts"], 1)
                self.assertEqual(result["incremental_server_failures"], 0)
                self.assertEqual(result["feasibility_oneshot_checks"], 0)

                checkpoint = result["generated_checkpoints"][0]
                warm = executor.resume(checkpoint, max_steps=32, max_states=8)
                self.assertEqual(
                    sorted(row["value"] for row in warm["halted"]),
                    [0, 1],
                )
                self.assertEqual(warm["incremental_context_exact_hits"], 2)
                self.assertEqual(warm["incremental_context_rebuilds"], 0)
                self.assertEqual(warm["incremental_server_starts"], 0)

            with LiveContinuationExecutor(store) as cold_executor:
                cold = cold_executor.resume(checkpoint, max_steps=32, max_states=8)
                self.assertEqual(
                    sorted(row["value"] for row in cold["halted"]),
                    [0, 1],
                )
                self.assertEqual(cold["incremental_context_rebuilds"], 1)
                self.assertEqual(cold["incremental_context_exact_hits"], 1)

    def test_incremental_solver_can_be_disabled_without_semantic_change(self):
        previous = os.environ.get("SYMCC_LIVE_INCREMENTAL_SOLVER")
        os.environ["SYMCC_LIVE_INCREMENTAL_SOLVER"] = "0"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with LiveContinuationExecutor(
                    LiveStateStore(tmp, page_size=64)
                ) as executor:
                    if executor._feasibility.name != "symcc-query-solver":
                        self.skipTest("symcc-query-solver is unavailable")
                    root = executor.create(
                        self._incremental_solver_program(),
                        input_bytes=b"\x01\x02",
                    )
                    result = executor.resume(root, max_steps=128, max_states=16)
            self.assertEqual(result["feasibility_mode"], "oneshot")
            self.assertEqual(
                sorted(row["value"] for row in result["halted"]),
                [0, 0, 1, 1],
            )
            self.assertEqual(result["feasibility_oneshot_checks"], 6)
            self.assertEqual(result["incremental_context_exact_hits"], 0)
            self.assertEqual(result["incremental_context_parent_hits"], 0)
            self.assertEqual(result["incremental_context_rebuilds"], 0)
        finally:
            if previous is None:
                os.environ.pop("SYMCC_LIVE_INCREMENTAL_SOLVER", None)
            else:
                os.environ["SYMCC_LIVE_INCREMENTAL_SOLVER"] = previous

    def test_incremental_solver_failure_opens_oneshot_circuit_breaker(self):
        previous_query = os.environ.get("SYMCC_QUERY_SOLVER")
        previous_incremental = os.environ.get("SYMCC_LIVE_INCREMENTAL_SOLVER")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                probe = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
                command = list(probe._feasibility.command)
                probe.close()
                if len(command) != 1:
                    self.skipTest("single query-solver helper is unavailable")
                wrapper = Path(tmp) / "oneshot_only.py"
                wrapper.write_text(
                    "import os,sys\n"
                    "helper=sys.argv[1]\n"
                    "args=sys.argv[2:]\n"
                    "if args == ['--server']:\n"
                    "    raise SystemExit(9)\n"
                    "os.execv(helper,[helper,*args])\n",
                    encoding="ascii",
                )
                os.environ["SYMCC_QUERY_SOLVER"] = (
                    f"{sys.executable} {wrapper} {command[0]}"
                )
                os.environ["SYMCC_LIVE_INCREMENTAL_SOLVER"] = "1"
                with LiveContinuationExecutor(
                    LiveStateStore(tmp, page_size=64)
                ) as executor:
                    root = executor.create(
                        self._incremental_solver_program(),
                        input_bytes=b"\x01\x02",
                    )
                    result = executor.resume(root, max_steps=128, max_states=16)
            self.assertEqual(result["feasibility_mode"], "oneshot")
            self.assertEqual(
                sorted(row["value"] for row in result["halted"]),
                [0, 0, 1, 1],
            )
            self.assertEqual(result["incremental_server_starts"], 1)
            self.assertEqual(result["incremental_server_failures"], 1)
            self.assertEqual(result["feasibility_oneshot_checks"], 6)
        finally:
            if previous_query is None:
                os.environ.pop("SYMCC_QUERY_SOLVER", None)
            else:
                os.environ["SYMCC_QUERY_SOLVER"] = previous_query
            if previous_incremental is None:
                os.environ.pop("SYMCC_LIVE_INCREMENTAL_SOLVER", None)
            else:
                os.environ["SYMCC_LIVE_INCREMENTAL_SOLVER"] = previous_incremental

    def test_bitvector_constants_are_normalized_to_their_width(self):
        program = {
            "schema": "symcc-live-program-v1",
            "entry": "main",
            "functions": {
                "main": {
                    "entry": "entry",
                    "blocks": {
                        "entry": [
                            {
                                "op": "binary",
                                "operator": "eq",
                                "dst": "same",
                                "left": {"const": -1, "bits": 8},
                                "right": {"const": 255, "bits": 8},
                                "bits": 1,
                            },
                            {"op": "return", "value": {"var": "same"}},
                        ],
                    },
                },
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            result = executor.resume(executor.create(program), max_steps=8)
        self.assertEqual([row["value"] for row in result["halted"]], [1])

    def test_signed_division_uses_exact_integer_truncation(self):
        program = {
            "schema": "symcc-live-program-v1",
            "entry": "main",
            "functions": {
                "main": {
                    "entry": "entry",
                    "blocks": {
                        "entry": [
                            {
                                "op": "binary",
                                "operator": "sdiv",
                                "dst": "quotient",
                                "left": {
                                    "const": (1 << 63) - 1,
                                    "bits": 64,
                                },
                                "right": {"const": 3, "bits": 64},
                                "bits": 64,
                            },
                            {
                                "op": "return",
                                "value": {"var": "quotient"},
                            },
                        ],
                    },
                },
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            result = executor.resume(executor.create(program), max_steps=8)
        self.assertEqual(
            [row["value"] for row in result["halted"]],
            [((1 << 63) - 1) // 3],
        )

    def test_defined_value_instruction_contract_rejects_tampering(self):
        program = {
            "schema": "symcc-live-program-v1",
            "entry": "main",
            "functions": {
                "main": {
                    "entry": "entry",
                    "blocks": {
                        "entry": [
                            {
                                "op": "binary",
                                "operator": "udiv",
                                "dst": "quotient",
                                "left": {"const": 8, "bits": 8},
                                "right": {"const": 2, "bits": 8},
                                "bits": 8,
                            },
                            {
                                "op": "assume",
                                "condition": {"const": 1, "bits": 1},
                            },
                            {
                                "op": "return",
                                "value": {"var": "quotient"},
                            },
                        ],
                    },
                },
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            result = executor.resume(executor.create(program), max_steps=8)
            self.assertEqual([row["value"] for row in result["halted"]], [4])

            invalid_operator = json.loads(json.dumps(program))
            invalid_operator["functions"]["main"]["blocks"]["entry"][0]["operator"] = (
                "host-divide"
            )
            with self.assertRaisesRegex(ValueError, "binary instruction is invalid"):
                executor.create(invalid_operator)

            wide_assume = json.loads(json.dumps(program))
            wide_assume["functions"]["main"]["blocks"]["entry"][1]["condition"] = {
                "const": 1,
                "bits": 8,
            }
            with self.assertRaisesRegex(ValueError, "assume condition width mismatch"):
                executor.resume(executor.create(wide_assume), max_steps=8)

    def test_nondeterministic_choice_is_dynamic_and_checkpoint_stable(self):
        program = {
            "schema": "symcc-live-program-v1",
            "entry": "main",
            "functions": {
                "main": {
                    "entry": "entry",
                    "blocks": {
                        "entry": [
                            {
                                "op": "const",
                                "dst": "iteration",
                                "value": 0,
                                "bits": 8,
                            },
                            {"op": "jump", "target": "loop"},
                        ],
                        "loop": [
                            {
                                "op": "nondet",
                                "dst": "choice",
                                "bits": 8,
                                "site": "loop.choice",
                            },
                            {
                                "op": "binary",
                                "operator": "add",
                                "dst": "iteration",
                                "left": {"var": "iteration"},
                                "right": {"const": 1, "bits": 8},
                                "bits": 8,
                            },
                            {
                                "op": "binary",
                                "operator": "ult",
                                "dst": "continue",
                                "left": {"var": "iteration"},
                                "right": {"const": 2, "bits": 8},
                                "bits": 1,
                            },
                            {
                                "op": "branch",
                                "condition": {"var": "continue"},
                                "true": "loop",
                                "false": "exit",
                            },
                        ],
                        "exit": [
                            {
                                "op": "halt",
                                "value": {"var": "choice"},
                            }
                        ],
                    },
                },
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            store = LiveStateStore(tmp, page_size=64)
            executor = LiveContinuationExecutor(store)
            paused = executor.resume(executor.create(program), max_steps=5)
            self.assertEqual(len(paused["frontier"]), 1)
            result = executor.resume(paused["frontier"][0], max_steps=16)
            self.assertEqual(len(result["halted"]), 1)
            expression = store.get_expression(result["halted"][0]["expression"])
            self.assertEqual(expression["op"], "nondet")
            self.assertEqual(expression["token"], "loop.choice:1")
            bundle = store.restore_continuation(result["halted"][0]["checkpoint"])
            symbolic_store = dict(bundle.symbolic_store)
            counter = store.get_expression(symbolic_store["@nondet:counter"])
            self.assertEqual(counter["value"], 2)

            duplicate = json.loads(json.dumps(program))
            duplicate["functions"]["main"]["blocks"]["entry"].insert(
                1,
                {
                    "op": "nondet",
                    "dst": "other",
                    "bits": 8,
                    "site": "loop.choice",
                },
            )
            with self.assertRaisesRegex(
                ValueError, "nondeterministic instruction is invalid"
            ):
                executor.create(duplicate)

    def test_multi_byte_big_endian_memory_load(self):
        program = {
            "schema": "symcc-live-program-v1",
            "entry": "main",
            "memory_hex": "1234",
            "memory_size": 2,
            "endianness": "big",
            "functions": {
                "main": {
                    "entry": "entry",
                    "blocks": {
                        "entry": [
                            {
                                "op": "load",
                                "dst": "word",
                                "address": {"const": 0, "bits": 64},
                                "bits": 16,
                                "bytes": 2,
                            },
                            {"op": "return", "value": {"var": "word"}},
                        ],
                    },
                },
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            result = executor.resume(executor.create(program), max_steps=8)
        self.assertEqual([row["value"] for row in result["halted"]], [0x1234])

    def test_symbolic_memory_address_is_rejected(self):
        program = {
            "schema": "symcc-live-program-v1",
            "entry": "main",
            "input_size": 1,
            "memory_size": 1,
            "functions": {
                "main": {
                    "entry": "entry",
                    "blocks": {
                        "entry": [
                            {"op": "input", "dst": "address", "offset": 0},
                            {
                                "op": "load",
                                "dst": "value",
                                "address": {"var": "address"},
                            },
                            {"op": "return", "value": {"var": "value"}},
                        ],
                    },
                },
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            checkpoint = executor.create(program, input_bytes=b"\0")
            with self.assertRaisesRegex(ValueError, "symbolic.*address"):
                executor.resume(checkpoint, max_steps=8)

    def test_memory_object_read_only_contract_is_rechecked(self):
        program = {
            "schema": "symcc-live-program-v1",
            "entry": "main",
            "memory_hex": "00" * 65,
            "memory_size": 65,
            "memory_objects": [
                {
                    "name": "constant",
                    "address": 64,
                    "size": 1,
                    "read_only": True,
                },
            ],
            "functions": {
                "main": {
                    "entry": "entry",
                    "blocks": {
                        "entry": [
                            {
                                "op": "store",
                                "address": {"const": 64, "bits": 64},
                                "value": {"const": 1, "bits": 8},
                                "bits": 8,
                                "bytes": 1,
                            },
                            {"op": "return", "value": 0},
                        ],
                    },
                },
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            checkpoint = executor.create(program)
            with self.assertRaisesRegex(ValueError, "read-only"):
                executor.resume(checkpoint, max_steps=8)

    @staticmethod
    def _input_buffer_program(address=64, capacity=4, load_offset=0):
        return {
            "schema": "symcc-live-program-v1",
            "entry": "main",
            "input_size": 0,
            "memory_size": address + capacity,
            "endianness": "little",
            "input_buffer": {
                "schema": "symcc-live-input-buffer-v1",
                "address": address,
                "capacity": capacity,
                "size_bits": 64,
            },
            "memory_objects": [
                {
                    "name": "$input",
                    "kind": "input",
                    "address": address,
                    "size": capacity,
                    "read_only": False,
                    "logical_size": "input-length",
                },
            ],
            "functions": {
                "main": {
                    "entry": "entry",
                    "blocks": {
                        "entry": [
                            {
                                "op": "input_size",
                                "dst": "size",
                                "bits": 64,
                            },
                            {
                                "op": "load",
                                "dst": "byte",
                                "address": {
                                    "const": address + load_offset,
                                    "bits": 64,
                                },
                                "bits": 8,
                                "bytes": 1,
                            },
                            {
                                "op": "binary",
                                "operator": "add",
                                "dst": "result",
                                "left": {"var": "byte"},
                                "right": {"var": "size"},
                                "bits": 8,
                            },
                            {
                                "op": "return",
                                "value": {"var": "result"},
                            },
                        ],
                    },
                },
            },
        }

    def test_input_buffer_maps_seed_to_symbolic_memory_and_length(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LiveStateStore(tmp, page_size=64)
            executor = LiveContinuationExecutor(store)
            checkpoint = executor.create(self._input_buffer_program(), input_bytes=b"A")
            initial = store.restore_continuation(checkpoint)
            self.assertIn("@input:length", dict(initial.symbolic_store))
            _concrete, symbolic = store.read_memory(
                initial.descriptor.symbolic_memory_root, 64, 1
            )
            self.assertIn(0, symbolic)
            result = executor.resume(checkpoint, max_steps=16)
        self.assertEqual([row["value"] for row in result["halted"]], [66])

    def test_input_buffer_rejects_capacity_and_logical_oob(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            program = self._input_buffer_program(load_offset=1)
            with self.assertRaisesRegex(ValueError, "capacity"):
                executor.create(program, input_bytes=b"ABCDE")
            checkpoint = executor.create(program, input_bytes=b"A")
            with self.assertRaisesRegex(ValueError, "outside declared objects"):
                executor.resume(checkpoint, max_steps=8)

    def test_input_buffer_contract_is_independently_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            program = self._input_buffer_program()
            program["memory_objects"][0]["logical_size"] = "capacity"
            with self.assertRaisesRegex(ValueError, "logical-size"):
                executor.create(program, input_bytes=b"A")

    @staticmethod
    def _stack_program(include_store=True):
        instructions = []
        if include_store:
            instructions.append(
                {
                    "op": "store",
                    "address": {"const": 64, "bits": 64},
                    "value": {"const": 41, "bits": 8},
                    "bits": 8,
                    "bytes": 1,
                }
            )
        instructions.extend(
            [
                {
                    "op": "load",
                    "dst": "value",
                    "address": {"const": 64, "bits": 64},
                    "bits": 8,
                    "bytes": 1,
                },
                {"op": "return", "value": {"var": "value"}},
            ]
        )
        return {
            "schema": "symcc-live-program-v1",
            "entry": "main",
            "memory_size": 65,
            "memory_objects": [
                {
                    "name": "main:slot",
                    "kind": "stack",
                    "function": "main",
                    "address": 64,
                    "size": 1,
                    "read_only": False,
                    "initialization": "runtime-write-tracked",
                },
            ],
            "functions": {
                "main": {
                    "entry": "entry",
                    "params": [],
                    "blocks": {"entry": instructions},
                },
            },
        }

    def test_stack_initialization_survives_pause_and_clears_on_return(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LiveStateStore(tmp, page_size=64)
            executor = LiveContinuationExecutor(store)
            root = executor.create(self._stack_program())
            paused = executor.resume(root, max_steps=1)
            self.assertEqual(len(paused["frontier"]), 1)
            checkpoint = paused["frontier"][0]
            paused_values = dict(store.restore_continuation(checkpoint).symbolic_store)
            self.assertIn("@stack:init:0:64", paused_values)

            completed = LiveContinuationExecutor(store).resume(checkpoint, max_steps=8)
            self.assertEqual([row["value"] for row in completed["halted"]], [41])
            final_values = dict(
                store.restore_continuation(
                    completed["halted"][0]["checkpoint"]
                ).symbolic_store
            )
            self.assertNotIn("@stack:init:0:64", final_values)
            self.assertFalse(any(name.startswith("0:") for name in final_values))

    @staticmethod
    def _cross_function_stack_program():
        program = LiveContinuationExecutorTests._stack_program()
        program["functions"]["main"]["blocks"]["entry"] = [
            {
                "op": "const",
                "dst": "pointer",
                "value": 64,
                "bits": 64,
            },
            {
                "op": "const",
                "dst": "input",
                "value": 41,
                "bits": 8,
            },
            {
                "op": "call",
                "function": "helper",
                "args": [
                    {"var": "pointer"},
                    {"var": "input"},
                ],
                "pointer_args": [{"index": 0, "bits": 64}],
                "dst": "result",
            },
            {"op": "return", "value": {"var": "result"}},
        ]
        program["functions"]["helper"] = {
            "entry": "entry",
            "params": ["pointer", "value"],
            "pointer_params": [{"index": 0, "bits": 64}],
            "blocks": {
                "entry": [
                    {
                        "op": "store",
                        "address": {"var": "pointer"},
                        "value": {"var": "value"},
                        "bits": 8,
                        "bytes": 1,
                    },
                    {
                        "op": "load",
                        "dst": "roundtrip",
                        "address": {"var": "pointer"},
                        "bits": 8,
                        "bytes": 1,
                    },
                    {
                        "op": "return",
                        "value": {"var": "roundtrip"},
                    },
                ],
            },
        }
        return program

    def test_cross_function_stack_owner_survives_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LiveStateStore(tmp, page_size=64)
            executor = LiveContinuationExecutor(store)
            paused = executor.resume(
                executor.create(self._cross_function_stack_program()),
                max_steps=4,
            )
            self.assertEqual(len(paused["frontier"]), 1)
            checkpoint = paused["frontier"][0]
            bundle = store.restore_continuation(checkpoint)
            self.assertEqual(
                [frame.function for frame in bundle.descriptor.frames],
                ["main", "helper"],
            )
            values = dict(bundle.symbolic_store)
            self.assertIn("@stack:init:0:64", values)
            self.assertNotIn("@stack:init:1:64", values)
            completed = LiveContinuationExecutor(store).resume(checkpoint, max_steps=8)
            self.assertEqual([row["value"] for row in completed["halted"]], [41])

    @staticmethod
    def _defined_return_program():
        return {
            "schema": "symcc-live-program-v1",
            "entry": "main",
            "functions": {
                "main": {
                    "entry": "entry",
                    "params": [],
                    "param_bits": [],
                    "return_bits": 8,
                    "blocks": {
                        "entry": [
                            {
                                "op": "call",
                                "function": "helper",
                                "args": [],
                                "dst": "result",
                                "result_bits": 8,
                                "defined_dst": "result_defined",
                            },
                            {
                                "op": "select",
                                "dst": "stable",
                                "condition": {"var": "result_defined"},
                                "true": {"var": "result"},
                                "false": {"const": 42, "bits": 8},
                                "bits": 8,
                            },
                            {
                                "op": "return",
                                "value": {"var": "stable"},
                            },
                        ],
                    },
                },
                "helper": {
                    "entry": "entry",
                    "params": [],
                    "param_bits": [],
                    "return_bits": 8,
                    "return_defined": True,
                    "blocks": {
                        "entry": [
                            {
                                "op": "return",
                                "value": {"const": 7, "bits": 8},
                                "defined": {"const": 0, "bits": 1},
                            },
                        ],
                    },
                },
            },
        }

    def test_defined_return_is_transported_across_call_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            result = executor.resume(executor.create(self._defined_return_program()))
            self.assertEqual([row["value"] for row in result["halted"]], [42])

    def test_defined_return_contract_rejects_missing_endpoint(self):
        for endpoint in ("defined_dst", "defined"):
            with self.subTest(endpoint=endpoint):
                program = self._defined_return_program()
                if endpoint == "defined_dst":
                    del program["functions"]["main"]["blocks"]["entry"][0][
                        "defined_dst"
                    ]
                else:
                    del program["functions"]["helper"]["blocks"]["entry"][0]["defined"]
                with tempfile.TemporaryDirectory() as tmp:
                    executor = LiveContinuationExecutor(
                        LiveStateStore(tmp, page_size=64)
                    )
                    with self.assertRaisesRegex(ValueError, "defined"):
                        executor.create(program)

    @staticmethod
    def _defined_argument_program():
        return {
            "schema": "symcc-live-program-v1",
            "entry": "main",
            "functions": {
                "main": {
                    "entry": "entry",
                    "params": [],
                    "param_bits": [],
                    "return_bits": 8,
                    "blocks": {
                        "entry": [
                            {
                                "op": "call",
                                "function": "helper",
                                "args": [
                                    {"const": 7, "bits": 8},
                                    {"const": 0, "bits": 1},
                                ],
                                "defined_args": [
                                    {"index": 0, "argument": 1},
                                ],
                                "dst": "result",
                                "result_bits": 8,
                            },
                            {
                                "op": "return",
                                "value": {"var": "result"},
                            },
                        ],
                    },
                },
                "helper": {
                    "entry": "entry",
                    "params": ["value", "value_defined"],
                    "param_bits": [8, 1],
                    "defined_params": [
                        {"index": 0, "parameter": 1},
                    ],
                    "return_bits": 8,
                    "blocks": {
                        "entry": [
                            {
                                "op": "select",
                                "dst": "stable",
                                "condition": {
                                    "var": "value_defined",
                                },
                                "true": {"var": "value"},
                                "false": {"const": 42, "bits": 8},
                                "bits": 8,
                            },
                            {
                                "op": "return",
                                "value": {"var": "stable"},
                            },
                        ],
                    },
                },
            },
        }

    def test_defined_argument_is_transported_into_call_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            result = executor.resume(executor.create(self._defined_argument_program()))
            self.assertEqual([row["value"] for row in result["halted"]], [42])

    def test_defined_argument_contract_rejects_missing_endpoint(self):
        for endpoint in ("defined_args", "defined_params"):
            with self.subTest(endpoint=endpoint):
                program = self._defined_argument_program()
                if endpoint == "defined_args":
                    del program["functions"]["main"]["blocks"]["entry"][0][
                        "defined_args"
                    ]
                else:
                    del program["functions"]["helper"]["defined_params"]
                with tempfile.TemporaryDirectory() as tmp:
                    executor = LiveContinuationExecutor(
                        LiveStateStore(tmp, page_size=64)
                    )
                    with self.assertRaisesRegex(ValueError, "arity|defined"):
                        executor.create(program)

    @staticmethod
    def _pointer_return_program():
        return {
            "schema": "symcc-live-program-v1",
            "entry": "main",
            "memory_size": 65,
            "memory_hex": "00" * 64 + "2a",
            "memory_objects": [
                {
                    "name": "byte",
                    "kind": "static",
                    "address": 64,
                    "size": 1,
                    "read_only": True,
                },
            ],
            "functions": {
                "main": {
                    "entry": "entry",
                    "params": [],
                    "blocks": {
                        "entry": [
                            {
                                "op": "call",
                                "function": "echo",
                                "args": [
                                    {"const": 64, "bits": 64},
                                ],
                                "pointer_args": [
                                    {"index": 0, "bits": 64},
                                ],
                                "pointer_result_bits": 64,
                                "dst": "pointer",
                            },
                            {
                                "op": "load",
                                "dst": "value",
                                "address": {"var": "pointer"},
                                "bits": 8,
                                "bytes": 1,
                            },
                            {
                                "op": "return",
                                "value": {"var": "value"},
                            },
                        ],
                    },
                },
                "echo": {
                    "entry": "entry",
                    "params": ["pointer"],
                    "pointer_params": [
                        {"index": 0, "bits": 64},
                    ],
                    "pointer_return_bits": 64,
                    "blocks": {
                        "entry": [
                            {
                                "op": "return",
                                "value": {"var": "pointer"},
                                "pointer_bits": 64,
                            },
                        ],
                    },
                },
            },
        }

    def test_cross_function_pointer_contract_is_rechecked(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            result = executor.resume(
                executor.create(self._pointer_return_program()),
                max_steps=8,
            )
            self.assertEqual([row["value"] for row in result["halted"]], [42])

            missing_argument = self._pointer_return_program()
            missing_argument["functions"]["main"]["blocks"]["entry"][0].pop(
                "pointer_args"
            )
            with self.assertRaisesRegex(ValueError, "pointer call.*callee contract"):
                executor.create(missing_argument)

            wrong_return = self._pointer_return_program()
            wrong_return["functions"]["echo"]["blocks"]["entry"][0]["pointer_bits"] = 32
            with self.assertRaisesRegex(
                ValueError, "pointer return.*function contract"
            ):
                executor.create(wrong_return)

    @classmethod
    def _pointer_domain_return_program(cls):
        program = cls._pointer_return_program()
        program["input_size"] = 1
        main = program["functions"]["main"]["blocks"]["entry"]
        call = main[0]
        main[0:0] = [
            {"op": "input", "dst": "index", "offset": 0},
            {
                "op": "unary",
                "operator": "zext",
                "dst": "wide_index",
                "value": {"var": "index"},
                "bits": 64,
            },
            {
                "op": "binary",
                "operator": "add",
                "dst": "pointer_argument",
                "left": {"const": 64, "bits": 64},
                "right": {"var": "wide_index"},
                "bits": 64,
            },
        ]
        call["args"] = [
            {"var": "pointer_argument"},
            {"const": 1, "bits": 1},
        ]
        call["pointer_domains"] = [{"index": 0, "argument": 1}]
        call["pointer_domain_dst"] = "pointer_domain"
        main[4]["alias_cases"] = [
            {
                "addresses": [64],
                "guards": [
                    {
                        "value": {"var": "pointer_domain"},
                        "equals": 1,
                        "bits": 1,
                    },
                ],
            },
        ]

        echo = program["functions"]["echo"]
        echo["params"].append("domain")
        echo["pointer_domains"] = [{"index": 0, "parameter": 1}]
        echo["pointer_return_domain"] = True
        echo["blocks"]["entry"][0]["pointer_domain"] = {"var": "domain"}
        return program

    def test_pointer_return_domain_is_transported_and_rechecked(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            valid = executor.resume(
                executor.create(
                    self._pointer_domain_return_program(),
                    input_bytes=b"\0",
                ),
                max_steps=8,
            )
            self.assertEqual([row["value"] for row in valid["halted"]], [42])

            false_certificate = self._pointer_domain_return_program()
            false_certificate["functions"]["main"]["blocks"]["entry"][3]["args"][1][
                "const"
            ] = 0
            infeasible = executor.resume(
                executor.create(false_certificate, input_bytes=b"\0"),
                max_steps=8,
            )
            self.assertEqual(
                [row["status"] for row in infeasible["halted"]],
                ["infeasible"],
            )

            missing_destination = self._pointer_domain_return_program()
            missing_destination["functions"]["main"]["blocks"]["entry"][3].pop(
                "pointer_domain_dst"
            )
            with self.assertRaisesRegex(
                ValueError, "pointer call result domain is invalid"
            ):
                executor.create(missing_destination)

            missing_return = self._pointer_domain_return_program()
            missing_return["functions"]["echo"]["blocks"]["entry"][0].pop(
                "pointer_domain"
            )
            with self.assertRaisesRegex(
                ValueError, "pointer return domain.*function contract"
            ):
                executor.create(missing_return)

    @staticmethod
    def _cross_function_domain_program(index):
        return {
            "schema": "symcc-live-program-v1",
            "entry": "main",
            "input_size": 1,
            "memory_size": 66,
            "memory_hex": "00" * 64 + "4142",
            "memory_objects": [
                {
                    "name": "bytes",
                    "kind": "static",
                    "address": 64,
                    "size": 2,
                    "read_only": True,
                },
            ],
            "functions": {
                "main": {
                    "entry": "entry",
                    "params": [],
                    "blocks": {
                        "entry": [
                            {"op": "input", "dst": "index", "offset": 0},
                            {
                                "op": "binary",
                                "operator": "eq",
                                "dst": "chosen",
                                "left": {"var": "index"},
                                "right": {"const": index, "bits": 8},
                                "bits": 1,
                            },
                            {
                                "op": "assume",
                                "condition": {"var": "chosen"},
                            },
                            {
                                "op": "pointer_offset",
                                "dst": "pointer",
                                "base": {"const": 64, "bits": 64},
                                "index": {"var": "index"},
                                "index_bits": 8,
                                "scale": {"const": 1, "bits": 64},
                                "bits": 64,
                            },
                            {
                                "op": "binary",
                                "operator": "sge",
                                "dst": "lower",
                                "left": {"var": "index"},
                                "right": {"const": 0, "bits": 8},
                                "bits": 1,
                            },
                            {
                                "op": "binary",
                                "operator": "sle",
                                "dst": "upper",
                                "left": {"var": "index"},
                                "right": {"const": 1, "bits": 8},
                                "bits": 1,
                            },
                            {
                                "op": "binary",
                                "operator": "and",
                                "dst": "domain",
                                "left": {"var": "lower"},
                                "right": {"var": "upper"},
                                "bits": 1,
                            },
                            {
                                "op": "call",
                                "function": "reader",
                                "args": [
                                    {"var": "pointer"},
                                    {"var": "domain"},
                                ],
                                "pointer_args": [
                                    {"index": 0, "bits": 64},
                                ],
                                "pointer_domains": [
                                    {"index": 0, "argument": 1},
                                ],
                                "dst": "result",
                            },
                            {
                                "op": "return",
                                "value": {"var": "result"},
                            },
                        ],
                    },
                },
                "reader": {
                    "entry": "entry",
                    "params": ["pointer", "domain"],
                    "pointer_params": [{"index": 0, "bits": 64}],
                    "pointer_domains": [{"index": 0, "parameter": 1}],
                    "blocks": {
                        "entry": [
                            {
                                "op": "load",
                                "dst": "value",
                                "address": {"var": "pointer"},
                                "alias_cases": [
                                    {
                                        "addresses": [64, 65],
                                        "guards": [
                                            {
                                                "value": {"var": "domain"},
                                                "equals": 1,
                                                "bits": 1,
                                            },
                                        ],
                                    },
                                ],
                                "bits": 8,
                                "bytes": 1,
                            },
                            {
                                "op": "return",
                                "value": {"var": "value"},
                            },
                        ],
                    },
                },
            },
        }

    def test_cross_function_pointer_domain_is_transport_and_rechecked(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            valid = executor.resume(
                executor.create(
                    self._cross_function_domain_program(0),
                    input_bytes=b"\0",
                ),
                max_steps=32,
            )
            self.assertEqual([row["value"] for row in valid["halted"]], [65])

            invalid = executor.resume(
                executor.create(
                    self._cross_function_domain_program(255),
                    input_bytes=b"\xff",
                ),
                max_steps=32,
            )
            self.assertEqual(
                [row["status"] for row in invalid["halted"]],
                ["infeasible"],
            )

            missing = self._cross_function_domain_program(0)
            missing["functions"]["main"]["blocks"]["entry"][7].pop("pointer_domains")
            with self.assertRaisesRegex(
                ValueError, "pointer call domain.*callee contract"
            ):
                executor.create(missing)

    @staticmethod
    def _indirect_call_program():
        return {
            "schema": "symcc-live-program-v1",
            "entry": "main",
            "input_size": 1,
            "functions": {
                "main": {
                    "entry": "entry",
                    "params": [],
                    "blocks": {
                        "entry": [
                            {
                                "op": "input",
                                "dst": "raw_choice",
                                "offset": 0,
                            },
                            {
                                "op": "unary",
                                "operator": "trunc",
                                "dst": "choice",
                                "value": {"var": "raw_choice"},
                                "bits": 1,
                            },
                            {
                                "op": "select",
                                "dst": "target",
                                "condition": {"var": "choice"},
                                "true": {"const": 1, "bits": 64},
                                "false": {"const": 2, "bits": 64},
                                "bits": 64,
                            },
                            {
                                "op": "indirect_call",
                                "target": {"var": "target"},
                                "target_bits": 64,
                                "targets": [
                                    {
                                        "function": "left",
                                        "id": 1,
                                        "guards": [
                                            {
                                                "value": {"var": "choice"},
                                                "equals": 1,
                                                "bits": 1,
                                            },
                                        ],
                                    },
                                    {
                                        "function": "right",
                                        "id": 2,
                                        "guards": [
                                            {
                                                "value": {"var": "choice"},
                                                "equals": 0,
                                                "bits": 1,
                                            },
                                        ],
                                    },
                                ],
                                "args": [{"const": 5, "bits": 8}],
                                "dst": "result",
                                "result_bits": 8,
                            },
                            {
                                "op": "return",
                                "value": {"var": "result"},
                            },
                        ],
                    },
                },
                "left": {
                    "entry": "entry",
                    "function_id": 1,
                    "params": ["value"],
                    "param_bits": [8],
                    "return_bits": 8,
                    "blocks": {
                        "entry": [
                            {
                                "op": "binary",
                                "operator": "add",
                                "dst": "result",
                                "left": {"var": "value"},
                                "right": {"const": 1, "bits": 8},
                                "bits": 8,
                            },
                            {
                                "op": "return",
                                "value": {"var": "result"},
                            },
                        ],
                    },
                },
                "right": {
                    "entry": "entry",
                    "function_id": 2,
                    "params": ["value"],
                    "param_bits": [8],
                    "return_bits": 8,
                    "blocks": {
                        "entry": [
                            {
                                "op": "binary",
                                "operator": "add",
                                "dst": "result",
                                "left": {"var": "value"},
                                "right": {"const": 2, "bits": 8},
                                "bits": 8,
                            },
                            {
                                "op": "return",
                                "value": {"var": "result"},
                            },
                        ],
                    },
                },
            },
        }

    def test_indirect_call_dispatch_forks_and_rechecks_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            result = executor.resume(
                executor.create(
                    self._indirect_call_program(),
                    input_bytes=b"\0",
                ),
                max_steps=16,
            )
            self.assertEqual(
                sorted(row["value"] for row in result["halted"]),
                [6, 7],
            )
            self.assertEqual(result["forks"], 1)

            wrong_identifier = self._indirect_call_program()
            wrong_identifier["functions"]["main"]["blocks"]["entry"][3]["targets"][1][
                "id"
            ] = 3
            with self.assertRaisesRegex(ValueError, "target identifier is invalid"):
                executor.create(wrong_identifier)

            mismatched_signature = self._indirect_call_program()
            mismatched_signature["functions"]["right"]["params"].append("extra")
            mismatched_signature["functions"]["right"]["param_bits"].append(8)
            with self.assertRaisesRegex(ValueError, "target signatures differ"):
                executor.create(mismatched_signature)

    def test_call_normal_target_controls_return_and_is_rechecked(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            program = self._indirect_call_program()
            main_blocks = program["functions"]["main"]["blocks"]
            call = main_blocks["entry"][3]
            call["normal_target"] = "resume"
            main_blocks["entry"][4] = {
                "op": "return",
                "value": {"const": 99, "bits": 8},
            }
            main_blocks["resume"] = [
                {
                    "op": "return",
                    "value": {"var": "result"},
                }
            ]
            result = executor.resume(
                executor.create(program, input_bytes=b"\0"),
                max_steps=16,
            )
            self.assertEqual(
                sorted(row["value"] for row in result["halted"]),
                [6, 7],
            )

            missing = self._indirect_call_program()
            missing["functions"]["main"]["blocks"]["entry"][3]["normal_target"] = (
                "missing"
            )
            with self.assertRaisesRegex(
                ValueError, "call normal target does not exist"
            ):
                executor.create(missing)

    @staticmethod
    def _indirect_pointer_return_program():
        targets = [
            {
                "function": "left",
                "id": 1,
                "guards": [
                    {
                        "value": {"var": "choice"},
                        "equals": 1,
                        "bits": 1,
                    },
                ],
            },
            {
                "function": "right",
                "id": 2,
                "guards": [
                    {
                        "value": {"var": "choice"},
                        "equals": 0,
                        "bits": 1,
                    },
                ],
            },
        ]
        functions = {
            "main": {
                "entry": "entry",
                "params": [],
                "param_bits": [],
                "return_bits": 8,
                "blocks": {
                    "entry": [
                        {"op": "input", "dst": "raw", "offset": 0},
                        {
                            "op": "unary",
                            "operator": "trunc",
                            "dst": "choice",
                            "value": {"var": "raw"},
                            "bits": 1,
                        },
                        {
                            "op": "select",
                            "dst": "target",
                            "condition": {"var": "choice"},
                            "true": {"const": 1, "bits": 64},
                            "false": {"const": 2, "bits": 64},
                            "bits": 64,
                        },
                        {
                            "op": "indirect_call",
                            "target": {"var": "target"},
                            "target_bits": 64,
                            "targets": targets,
                            "args": [],
                            "dst": "pointer",
                            "pointer_result_bits": 64,
                            "pointer_domain_dst": "pointer_domain",
                            "result_bits": 64,
                        },
                        {
                            "op": "load",
                            "dst": "value",
                            "address": {"var": "pointer"},
                            "alias_cases": [
                                {
                                    "addresses": [64],
                                    "guards": [
                                        {
                                            "value": {"var": "pointer_domain"},
                                            "equals": 1,
                                            "bits": 1,
                                        },
                                    ],
                                },
                                {
                                    "addresses": [65],
                                    "guards": [
                                        {
                                            "value": {"var": "pointer_domain"},
                                            "equals": 1,
                                            "bits": 1,
                                        },
                                    ],
                                },
                            ],
                            "bits": 8,
                            "bytes": 1,
                        },
                        {
                            "op": "return",
                            "value": {"var": "value"},
                        },
                    ],
                },
            },
        }
        for name, identifier, address in (
            ("left", 1, 64),
            ("right", 2, 65),
        ):
            functions[name] = {
                "entry": "entry",
                "function_id": identifier,
                "params": [],
                "param_bits": [],
                "return_bits": 64,
                "pointer_return_bits": 64,
                "pointer_return_domain": True,
                "blocks": {
                    "entry": [
                        {
                            "op": "return",
                            "value": {"const": address, "bits": 64},
                            "pointer_bits": 64,
                            "pointer_domain": {
                                "const": 1,
                                "bits": 1,
                            },
                        },
                    ],
                },
            }
        return {
            "schema": "symcc-live-program-v1",
            "entry": "main",
            "input_size": 1,
            "memory_size": 66,
            "memory_hex": "00" * 64 + "4142",
            "memory_objects": [
                {
                    "name": "left",
                    "kind": "static",
                    "address": 64,
                    "size": 1,
                    "read_only": True,
                },
                {
                    "name": "right",
                    "kind": "static",
                    "address": 65,
                    "size": 1,
                    "read_only": True,
                },
            ],
            "functions": functions,
        }

    def test_indirect_pointer_return_domain_is_target_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            result = executor.resume(
                executor.create(
                    self._indirect_pointer_return_program(),
                    input_bytes=b"\0",
                ),
                max_steps=16,
            )
            self.assertEqual(
                sorted(row["value"] for row in result["halted"]),
                [65, 66],
            )
            self.assertEqual(result["forks"], 1)

            missing_destination = self._indirect_pointer_return_program()
            missing_destination["functions"]["main"]["blocks"]["entry"][3][
                "pointer_domain_dst"
            ] = ""
            with self.assertRaisesRegex(
                ValueError, "pointer call result domain is invalid"
            ):
                executor.create(missing_destination)

            mismatched_target = self._indirect_pointer_return_program()
            mismatched_target["functions"]["right"].pop("pointer_return_domain")
            with self.assertRaisesRegex(
                ValueError,
                "pointer return domain|target signatures differ",
            ):
                executor.create(mismatched_target)

    @staticmethod
    def _guarded_load_program(guard):
        return {
            "schema": "symcc-live-program-v1",
            "entry": "main",
            "memory_size": 65,
            "memory_hex": "00" * 64 + "2a",
            "memory_objects": [
                {
                    "name": "byte",
                    "kind": "static",
                    "address": 64,
                    "size": 1,
                    "read_only": True,
                },
            ],
            "functions": {
                "main": {
                    "entry": "entry",
                    "params": [],
                    "param_bits": [],
                    "return_bits": 8,
                    "blocks": {
                        "entry": [
                            {
                                "op": "load",
                                "dst": "value",
                                "address": {
                                    "const": 65,
                                    "bits": 64,
                                },
                                "guard": guard,
                                "bits": 8,
                                "bytes": 1,
                            },
                            {
                                "op": "return",
                                "value": {"var": "value"},
                            },
                        ],
                    },
                },
            },
        }

    def test_guarded_load_requires_only_active_access_domain(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            inactive = executor.resume(
                executor.create(
                    self._guarded_load_program(
                        {
                            "const": 0,
                            "bits": 1,
                        }
                    )
                ),
                max_steps=4,
            )
            self.assertEqual(
                [row["value"] for row in inactive["halted"]],
                [0],
            )

            active = executor.resume(
                executor.create(
                    self._guarded_load_program(
                        {
                            "const": 1,
                            "bits": 1,
                        }
                    )
                ),
                max_steps=4,
            )
            self.assertEqual(
                [row["status"] for row in active["halted"]],
                ["infeasible"],
            )

            malformed = self._guarded_load_program(
                {
                    "const": 0,
                    "bits": 8,
                }
            )
            with self.assertRaisesRegex(ValueError, "condition width mismatch"):
                executor.resume(
                    executor.create(malformed),
                    max_steps=4,
                )

    @staticmethod
    def _guarded_store_program(guard):
        return {
            "schema": "symcc-live-program-v1",
            "entry": "main",
            "memory_size": 66,
            "memory_hex": "00" * 64 + "2a00",
            "memory_objects": [
                {
                    "name": "byte",
                    "kind": "static",
                    "address": 64,
                    "size": 1,
                    "read_only": False,
                },
            ],
            "functions": {
                "main": {
                    "entry": "entry",
                    "params": [],
                    "param_bits": [],
                    "return_bits": 8,
                    "blocks": {
                        "entry": [
                            {
                                "op": "store",
                                "address": {
                                    "const": 65,
                                    "bits": 64,
                                },
                                "value": {"const": 99, "bits": 8},
                                "guard": guard,
                                "bits": 8,
                                "bytes": 1,
                            },
                            {
                                "op": "load",
                                "dst": "value",
                                "address": {
                                    "const": 64,
                                    "bits": 64,
                                },
                                "bits": 8,
                                "bytes": 1,
                            },
                            {
                                "op": "return",
                                "value": {"var": "value"},
                            },
                        ],
                    },
                },
            },
        }

    def test_guarded_store_requires_only_active_access_domain(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            inactive = executor.resume(
                executor.create(
                    self._guarded_store_program(
                        {
                            "const": 0,
                            "bits": 1,
                        }
                    )
                ),
                max_steps=5,
            )
            self.assertEqual(
                [row["value"] for row in inactive["halted"]],
                [42],
            )

            active = executor.resume(
                executor.create(
                    self._guarded_store_program(
                        {
                            "const": 1,
                            "bits": 1,
                        }
                    )
                ),
                max_steps=5,
            )
            self.assertEqual(
                [row["status"] for row in active["halted"]],
                ["infeasible"],
            )

            malformed = self._guarded_store_program(
                {
                    "const": 0,
                    "bits": 8,
                }
            )
            with self.assertRaisesRegex(ValueError, "condition width mismatch"):
                executor.resume(
                    executor.create(malformed),
                    max_steps=5,
                )

    def test_stack_uninitialized_and_inactive_accesses_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            uninitialized = self._stack_program(include_store=False)
            checkpoint = executor.create(uninitialized)
            with self.assertRaisesRegex(ValueError, "uninitialized"):
                executor.resume(checkpoint, max_steps=8)

            inactive = self._stack_program()
            inactive["memory_objects"][0]["function"] = "helper"
            inactive["functions"]["helper"] = {
                "entry": "entry",
                "params": [],
                "blocks": {
                    "entry": [{"op": "return", "value": 0}],
                },
            }
            checkpoint = executor.create(inactive)
            with self.assertRaisesRegex(ValueError, "inactive stack"):
                executor.resume(checkpoint, max_steps=8)

    def test_stack_contract_rejects_recursive_call_graph(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            program = self._stack_program()
            program["functions"]["main"]["blocks"]["entry"] = [
                {
                    "op": "call",
                    "function": "main",
                    "args": [],
                    "dst": "again",
                },
                {"op": "return", "value": 0},
            ]
            with self.assertRaisesRegex(ValueError, "acyclic call graph"):
                executor.create(program)

    @staticmethod
    def _heap_program(instructions=None):
        if instructions is None:
            instructions = [
                {
                    "op": "heap_alloc",
                    "dst": "pointer",
                    "address": 64,
                    "size": 4,
                    "site": "main:heap:0",
                    "bits": 64,
                },
                {
                    "op": "store",
                    "address": {"var": "pointer"},
                    "value": {"const": 41, "bits": 8},
                    "bits": 8,
                    "bytes": 1,
                },
                {
                    "op": "load",
                    "dst": "value",
                    "address": {"var": "pointer"},
                    "bits": 8,
                    "bytes": 1,
                },
                {
                    "op": "heap_free",
                    "address": {"var": "pointer"},
                },
                {"op": "return", "value": {"var": "value"}},
            ]
        return {
            "schema": "symcc-live-program-v1",
            "entry": "main",
            "memory_size": 68,
            "memory_objects": [
                {
                    "name": "heap:main:0",
                    "kind": "heap",
                    "site": "main:heap:0",
                    "address": 64,
                    "size": 4,
                    "read_only": False,
                    "lifetime": "runtime-alloc-free",
                    "allocation": "bounded-infallible",
                },
            ],
            "functions": {
                "main": {
                    "entry": "entry",
                    "params": [],
                    "blocks": {"entry": instructions},
                },
            },
        }

    def test_heap_lifetime_survives_pause_and_clears_on_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LiveStateStore(tmp, page_size=64)
            executor = LiveContinuationExecutor(store)
            root = executor.create(self._heap_program())
            paused = executor.resume(root, max_steps=2)
            self.assertEqual(len(paused["frontier"]), 1)
            checkpoint = paused["frontier"][0]
            paused_values = dict(store.restore_continuation(checkpoint).symbolic_store)
            self.assertIn("@heap:live:64", paused_values)
            self.assertIn("@heap:init:64", paused_values)

            completed = LiveContinuationExecutor(store).resume(checkpoint, max_steps=8)
            self.assertEqual([row["value"] for row in completed["halted"]], [41])
            final_values = dict(
                store.restore_continuation(
                    completed["halted"][0]["checkpoint"]
                ).symbolic_store
            )
            self.assertNotIn("@heap:live:64", final_values)
            self.assertNotIn("@heap:init:64", final_values)

        certified_program = {
            "schema": "symcc-live-program-v1",
            "entry": "main",
            "input_size": 1,
            "memory_size": 72,
            "memory_objects": [
                {
                    "name": f"heap:site:{index}",
                    "kind": "heap",
                    "site": f"site:{index}",
                    "slot": 0,
                    "capacity": 1,
                    "address": address,
                    "size": 4,
                    "read_only": False,
                    "lifetime": "runtime-alloc-free",
                    "allocation": "bounded-pool-infallible",
                }
                for index, address in enumerate((64, 68))
            ],
            "functions": {
                "main": {
                    "entry": "entry",
                    "params": [],
                    "blocks": {
                        "entry": [
                            {
                                "op": "heap_alloc",
                                "dst": "left",
                                "addresses": [64],
                                "capacity": 1,
                                "size": 4,
                                "site": "site:0",
                                "allocator": "malloc",
                                "bits": 64,
                            },
                            {
                                "op": "heap_alloc",
                                "dst": "right",
                                "addresses": [68],
                                "capacity": 1,
                                "size": 4,
                                "site": "site:1",
                                "allocator": "malloc",
                                "bits": 64,
                            },
                            {"op": "input", "dst": "raw", "offset": 0},
                            {
                                "op": "binary",
                                "operator": "ne",
                                "dst": "choose_left",
                                "left": {"var": "raw"},
                                "right": {"const": 0, "bits": 8},
                                "bits": 1,
                            },
                            {
                                "op": "select",
                                "dst": "victim",
                                "condition": {"var": "choose_left"},
                                "true": {"var": "left"},
                                "false": {"var": "right"},
                                "bits": 64,
                            },
                            {
                                "op": "heap_free",
                                "address": {"var": "victim"},
                                "addresses": [64, 68],
                                "bits": 64,
                            },
                            {"op": "return", "value": 0},
                        ],
                    },
                },
            },
            "lowering": {
                "capabilities": [
                    "bounded-heap-lifetime",
                    "bounded-heap-lifetime-pointer-union",
                ],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            store = LiveStateStore(tmp, page_size=64)
            executor = LiveContinuationExecutor(store)
            result = executor.resume(
                executor.create(certified_program), max_steps=16
            )
            self.assertEqual([row["value"] for row in result["halted"]], [0])
            values = dict(store.restore_continuation(
                result["halted"][0]["checkpoint"]
            ).symbolic_store)

            def evaluate(digest, raw):
                expression = store.get_expression(digest)
                op = expression["op"]
                if op == "const":
                    return int(expression["value"])
                if op == "input":
                    return raw
                children = [
                    evaluate(child, raw)
                    for child in expression.get("children", ())
                ]
                if op == "not":
                    return int(not children[0])
                if op == "and":
                    return children[0] & children[1]
                if op == "ne":
                    return int(children[0] != children[1])
                if op == "eq":
                    return int(children[0] == children[1])
                if op == "ite":
                    return children[1] if children[0] else children[2]
                raise AssertionError(f"unexpected heap condition op: {op}")

            left = values["@heap:live:64"]
            right = values["@heap:live:68"]
            self.assertEqual(
                [(evaluate(left, raw), evaluate(right, raw)) for raw in (0, 1)],
                [(1, 0), (0, 1)],
            )

            missing_capability = json.loads(json.dumps(certified_program))
            missing_capability["lowering"]["capabilities"].pop()
            with self.assertRaisesRegex(ValueError, "certificate is invalid"):
                executor.create(missing_capability)

            foreign_base = json.loads(json.dumps(certified_program))
            free = foreign_base["functions"]["main"]["blocks"]["entry"][-2]
            free["addresses"] = [64, 72]
            with self.assertRaisesRegex(ValueError, "certificate is invalid"):
                executor.create(foreign_base)

            width_mismatch = json.loads(json.dumps(certified_program))
            free = width_mismatch["functions"]["main"]["blocks"]["entry"][-2]
            free["bits"] = 32
            checkpoint = executor.create(width_mismatch)
            with self.assertRaisesRegex(ValueError, "operand width mismatch"):
                executor.resume(checkpoint, max_steps=16)

    def test_heap_rejects_uninitialized_uaf_and_double_free(self):
        allocation = {
            "op": "heap_alloc",
            "dst": "pointer",
            "address": 64,
            "size": 4,
            "site": "main:heap:0",
            "bits": 64,
        }
        free = {"op": "heap_free", "address": {"var": "pointer"}}
        load = {
            "op": "load",
            "dst": "value",
            "address": {"var": "pointer"},
            "bits": 8,
            "bytes": 1,
        }
        store = {
            "op": "store",
            "address": {"var": "pointer"},
            "value": {"const": 1, "bits": 8},
            "bits": 8,
            "bytes": 1,
        }
        cases = (
            (
                [
                    allocation,
                    load,
                    free,
                    {
                        "op": "return",
                        "value": 0,
                    },
                ],
                "uninitialized",
            ),
            (
                [
                    allocation,
                    store,
                    free,
                    load,
                    {
                        "op": "return",
                        "value": 0,
                    },
                ],
                "inactive heap",
            ),
            (
                [
                    allocation,
                    free,
                    free,
                    {
                        "op": "return",
                        "value": 0,
                    },
                ],
                "not allocated",
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            for instructions, diagnostic in cases:
                checkpoint = executor.create(self._heap_program(instructions))
                with self.assertRaisesRegex(ValueError, diagnostic):
                    executor.resume(checkpoint, max_steps=8)

    def test_heap_callsite_can_be_reallocated_after_free(self):
        program = self._heap_program()
        program["functions"]["main"]["blocks"] = {
            "entry": [
                {"op": "const", "dst": "iteration", "value": 0, "bits": 8},
                {"op": "jump", "target": "loop"},
            ],
            "loop": [
                {
                    "op": "heap_alloc",
                    "dst": "pointer",
                    "address": 64,
                    "size": 4,
                    "site": "main:heap:0",
                    "bits": 64,
                },
                {
                    "op": "store",
                    "address": {"var": "pointer"},
                    "value": {"var": "iteration"},
                    "bits": 8,
                    "bytes": 1,
                },
                {
                    "op": "load",
                    "dst": "result",
                    "address": {"var": "pointer"},
                    "bits": 8,
                    "bytes": 1,
                },
                {"op": "heap_free", "address": {"var": "pointer"}},
                {
                    "op": "binary",
                    "operator": "add",
                    "dst": "iteration",
                    "left": {"var": "iteration"},
                    "right": {"const": 1, "bits": 8},
                    "bits": 8,
                },
                {
                    "op": "binary",
                    "operator": "ult",
                    "dst": "again",
                    "left": {"var": "iteration"},
                    "right": {"const": 2, "bits": 8},
                    "bits": 1,
                },
                {
                    "op": "branch",
                    "condition": {"var": "again"},
                    "true": "loop",
                    "false": "exit",
                },
            ],
            "exit": [
                {"op": "return", "value": {"var": "result"}},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            result = executor.resume(executor.create(program), max_steps=32)
        self.assertEqual([row["value"] for row in result["halted"]], [1])

    @staticmethod
    def _heap_pool_program(limit=2):
        return {
            "schema": "symcc-live-program-v1",
            "entry": "main",
            "memory_size": 72,
            "memory_objects": [
                {
                    "name": f"heap:main:0:{slot}",
                    "kind": "heap",
                    "site": "main:heap:0",
                    "slot": slot,
                    "capacity": 2,
                    "address": 64 + slot * 4,
                    "size": 4,
                    "read_only": False,
                    "lifetime": "runtime-alloc-free",
                    "allocation": "bounded-pool-infallible",
                }
                for slot in range(2)
            ],
            "functions": {
                "main": {
                    "entry": "entry",
                    "params": [],
                    "blocks": {
                        "entry": [
                            {
                                "op": "const",
                                "dst": "iteration",
                                "value": 0,
                                "bits": 8,
                            },
                            {
                                "op": "const",
                                "dst": "sum",
                                "value": 0,
                                "bits": 8,
                            },
                            {"op": "jump", "target": "loop"},
                        ],
                        "loop": [
                            {
                                "op": "heap_alloc",
                                "dst": "pointer",
                                "addresses": [64, 68],
                                "capacity": 2,
                                "size": 4,
                                "site": "main:heap:0",
                                "bits": 64,
                            },
                            {
                                "op": "store",
                                "address": {"var": "pointer"},
                                "value": {"var": "iteration"},
                                "bits": 8,
                                "bytes": 1,
                            },
                            {
                                "op": "load",
                                "dst": "value",
                                "address": {"var": "pointer"},
                                "bits": 8,
                                "bytes": 1,
                            },
                            {
                                "op": "binary",
                                "operator": "add",
                                "dst": "biased",
                                "left": {"var": "value"},
                                "right": {"const": 10, "bits": 8},
                                "bits": 8,
                            },
                            {
                                "op": "binary",
                                "operator": "add",
                                "dst": "sum",
                                "left": {"var": "sum"},
                                "right": {"var": "biased"},
                                "bits": 8,
                            },
                            {
                                "op": "binary",
                                "operator": "add",
                                "dst": "iteration",
                                "left": {"var": "iteration"},
                                "right": {"const": 1, "bits": 8},
                                "bits": 8,
                            },
                            {
                                "op": "binary",
                                "operator": "ult",
                                "dst": "again",
                                "left": {"var": "iteration"},
                                "right": {"const": limit, "bits": 8},
                                "bits": 1,
                            },
                            {
                                "op": "branch",
                                "condition": {"var": "again"},
                                "true": "loop",
                                "false": "exit",
                            },
                        ],
                        "exit": [
                            {"op": "return", "value": {"var": "sum"}},
                        ],
                    },
                },
            },
        }

    def test_heap_pool_keeps_multiple_live_instances_across_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LiveStateStore(tmp, page_size=64)
            executor = LiveContinuationExecutor(store)
            paused = executor.resume(
                executor.create(self._heap_pool_program()), max_steps=11
            )
            self.assertEqual(len(paused["frontier"]), 1)
            checkpoint = paused["frontier"][0]
            paused_values = dict(store.restore_continuation(checkpoint).symbolic_store)
            self.assertIn("@heap:live:64", paused_values)
            self.assertNotIn("@heap:live:68", paused_values)

            completed = LiveContinuationExecutor(store).resume(checkpoint, max_steps=32)
            self.assertEqual([row["value"] for row in completed["halted"]], [21])
            final_values = dict(
                store.restore_continuation(
                    completed["halted"][0]["checkpoint"]
                ).symbolic_store
            )
            for address in (64, 68):
                self.assertIn(f"@heap:live:{address}", final_values)
                self.assertIn(f"@heap:init:{address}", final_values)

    def test_heap_pool_rejects_exhaustion_and_malformed_slots(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            checkpoint = executor.create(self._heap_pool_program(limit=3))
            with self.assertRaisesRegex(ValueError, "pool is exhausted"):
                executor.resume(checkpoint, max_steps=64)

            missing = self._heap_pool_program()
            missing["memory_objects"].pop()
            with self.assertRaisesRegex(ValueError, "pool contract"):
                executor.create(missing)

            wrong_addresses = self._heap_pool_program()
            wrong_addresses["functions"]["main"]["blocks"]["loop"][0]["addresses"] = [
                68,
                64,
            ]
            with self.assertRaisesRegex(ValueError, "memory pool"):
                executor.create(wrong_addresses)

            narrow_pointer = self._heap_pool_program()
            narrow_pointer["functions"]["main"]["blocks"]["loop"][0]["bits"] = 6
            with self.assertRaisesRegex(ValueError, "memory pool"):
                executor.create(narrow_pointer)

            missing_capacity = self._heap_pool_program()
            missing_capacity["functions"]["main"]["blocks"]["loop"][0].pop("capacity")
            with self.assertRaisesRegex(ValueError, "memory pool"):
                executor.create(missing_capacity)

    @staticmethod
    def _nullable_heap_program(instructions, *, capacity=2, object_size=4):
        return {
            "schema": "symcc-live-program-v1",
            "entry": "main",
            "input_size": 1,
            "memory_size": 64 + capacity * object_size,
            "memory_objects": [
                {
                    "name": f"heap:nullable:{slot}",
                    "kind": "heap",
                    "site": "main:nullable:0",
                    "slot": slot,
                    "capacity": capacity,
                    "address": 64 + slot * object_size,
                    "size": object_size,
                    "logical_size": "runtime-allocation-size",
                    "read_only": False,
                    "lifetime": "runtime-alloc-free",
                    "allocation": "bounded-pool-nullable",
                }
                for slot in range(capacity)
            ],
            "functions": {
                "main": {
                    "entry": "entry",
                    "params": [],
                    "blocks": instructions,
                },
            },
        }

    def test_nullable_heap_models_zero_and_bounded_oom(self):
        blocks = {
            "entry": [
                {"op": "input", "dst": "size", "offset": 0},
                {
                    "op": "heap_alloc",
                    "dst": "pointer",
                    "addresses": [64, 68],
                    "capacity": 2,
                    "size": {"var": "size"},
                    "size_bits": 8,
                    "max_size": 4,
                    "nullable": True,
                    "site": "main:nullable:0",
                    "bits": 64,
                },
                {
                    "op": "binary",
                    "operator": "eq",
                    "dst": "is_null",
                    "left": {"var": "pointer"},
                    "right": {"const": 0, "bits": 64},
                    "bits": 1,
                },
                {"op": "return", "value": {"var": "is_null"}},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            store = LiveStateStore(tmp, page_size=64)
            executor = LiveContinuationExecutor(store)
            results = []
            checkpoints = []
            for size in (0, 3, 5):
                completed = executor.resume(
                    executor.create(
                        self._nullable_heap_program(blocks),
                        input_bytes=bytes([size]),
                    ),
                    max_steps=8,
                )
                results.append(completed["halted"][0]["value"])
                checkpoints.append(completed["halted"][0]["checkpoint"])
            self.assertEqual(results, [1, 0, 1])
            live_values = dict(
                store.restore_continuation(checkpoints[1]).symbolic_store
            )
            self.assertIn("@heap:live:64", live_values)
            self.assertIn("@heap:size:64", live_values)

    def test_nullable_heap_exhaustion_returns_null(self):
        blocks = {
            "entry": [
                {
                    "op": "const",
                    "dst": "size",
                    "value": 1,
                    "bits": 8,
                },
                {"op": "jump", "target": "allocate"},
            ],
            "allocate": [
                {
                    "op": "heap_alloc",
                    "dst": "pointer",
                    "addresses": [64, 68],
                    "capacity": 2,
                    "size": {"var": "size"},
                    "size_bits": 8,
                    "max_size": 4,
                    "nullable": True,
                    "site": "main:nullable:0",
                    "bits": 64,
                },
                {
                    "op": "binary",
                    "operator": "eq",
                    "dst": "is_null",
                    "left": {"var": "pointer"},
                    "right": {"const": 0, "bits": 64},
                    "bits": 1,
                },
                {
                    "op": "branch",
                    "condition": {"var": "is_null"},
                    "true": "exit",
                    "false": "again",
                },
            ],
            "again": [{"op": "jump", "target": "allocate"}],
            "exit": [{"op": "return", "value": {"var": "is_null"}}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            completed = executor.resume(
                executor.create(self._nullable_heap_program(blocks)),
                max_steps=32,
            )
        self.assertEqual([row["value"] for row in completed["halted"]], [1])

    def test_calloc_checks_overflow_and_zero_initializes(self):
        def blocks(count, element_size, tail):
            return {
                "entry": [
                    {
                        "op": "heap_alloc",
                        "dst": "pointer",
                        "addresses": [64],
                        "capacity": 1,
                        "count": {"const": count, "bits": 8},
                        "element_size": {
                            "const": element_size,
                            "bits": 8,
                        },
                        "size_bits": 8,
                        "max_size": 4,
                        "nullable": True,
                        "zero_initialize": True,
                        "allocator": "calloc",
                        "site": "main:nullable:0",
                        "bits": 64,
                    },
                    *tail,
                ],
            }

        zero_tail = [
            {
                "op": "load",
                "dst": "zero",
                "address": {"var": "pointer"},
                "bits": 8,
                "bytes": 1,
            },
            {"op": "return", "value": {"var": "zero"}},
        ]
        overflow_tail = [
            {
                "op": "binary",
                "operator": "eq",
                "dst": "is_null",
                "left": {"var": "pointer"},
                "right": {"const": 0, "bits": 64},
                "bits": 1,
            },
            {"op": "return", "value": {"var": "is_null"}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            values = []
            for program_blocks in (
                blocks(2, 1, zero_tail),
                blocks(128, 2, overflow_tail),
            ):
                result = executor.resume(
                    executor.create(
                        self._nullable_heap_program(program_blocks, capacity=1)
                    ),
                    max_steps=8,
                )
                values.append(result["halted"][0]["value"])
        self.assertEqual(values, [0, 1])

    def test_nullable_heap_enforces_logical_object_size(self):
        blocks = {
            "entry": [
                {
                    "op": "heap_alloc",
                    "dst": "pointer",
                    "addresses": [64],
                    "capacity": 1,
                    "size": {"const": 1, "bits": 8},
                    "size_bits": 8,
                    "max_size": 4,
                    "nullable": True,
                    "site": "main:nullable:0",
                    "bits": 64,
                },
                {
                    "op": "store",
                    "address": {"var": "pointer"},
                    "value": {"const": 7, "bits": 16},
                    "bits": 16,
                    "bytes": 2,
                },
                {"op": "return", "value": 0},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            checkpoint = executor.create(
                self._nullable_heap_program(blocks, capacity=1)
            )
            with self.assertRaisesRegex(ValueError, "logical allocation"):
                executor.resume(checkpoint, max_steps=8)

            narrow_blocks = {
                "entry": [
                    {
                        "op": "heap_alloc",
                        "dst": "pointer",
                        "addresses": [64],
                        "capacity": 1,
                        "size": {"const": 255, "bits": 8},
                        "size_bits": 8,
                        "max_size": 300,
                        "nullable": True,
                        "site": "main:nullable:0",
                        "bits": 64,
                    },
                    {
                        "op": "store",
                        "address": {"const": 320, "bits": 64},
                        "value": {"const": 7, "bits": 8},
                        "bits": 8,
                        "bytes": 1,
                    },
                    {"op": "return", "value": 0},
                ],
            }
            narrow = executor.create(
                self._nullable_heap_program(narrow_blocks, capacity=1, object_size=300)
            )
            with self.assertRaisesRegex(ValueError, "logical allocation"):
                executor.resume(narrow, max_steps=8)

    def test_bounded_realloc_preserves_failure_and_invalidates_growth(self):
        allocation = {
            "op": "heap_alloc",
            "dst": "pointer",
            "addresses": [64],
            "capacity": 1,
            "size": {"const": 1, "bits": 8},
            "size_bits": 8,
            "max_size": 4,
            "nullable": True,
            "site": "main:nullable:0",
            "bits": 64,
        }
        store = {
            "op": "store",
            "address": {"var": "pointer"},
            "value": {"const": 7, "bits": 8},
            "bits": 8,
            "bytes": 1,
        }

        def resize(size):
            return {
                "op": "heap_realloc",
                "dst": "resized",
                "address": {"var": "pointer"},
                "addresses": [64],
                "size": {"const": size, "bits": 8},
                "size_bits": 8,
                "bits": 64,
                "strategy": "bounded-in-place",
                "nullable": True,
            }

        failed_blocks = {
            "entry": [
                allocation,
                store,
                resize(5),
                {
                    "op": "load",
                    "dst": "preserved",
                    "address": {"var": "pointer"},
                    "bits": 8,
                    "bytes": 1,
                },
                {
                    "op": "binary",
                    "operator": "eq",
                    "dst": "is_null",
                    "left": {"var": "resized"},
                    "right": {"const": 0, "bits": 64},
                    "bits": 1,
                },
                {"op": "return", "value": {"var": "is_null"}},
            ],
        }
        growth_blocks = {
            "entry": [
                allocation,
                store,
                resize(3),
                {
                    "op": "load",
                    "dst": "new_byte",
                    "address": {"const": 65, "bits": 64},
                    "bits": 8,
                    "bytes": 1,
                },
                {"op": "return", "value": {"var": "new_byte"}},
            ],
        }
        zero_blocks = {
            "entry": [
                allocation,
                store,
                resize(0),
                {
                    "op": "load",
                    "dst": "released",
                    "address": {"var": "pointer"},
                    "bits": 8,
                    "bytes": 1,
                },
                {"op": "return", "value": {"var": "released"}},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            failed = executor.resume(
                executor.create(self._nullable_heap_program(failed_blocks, capacity=1)),
                max_steps=16,
            )
            self.assertEqual([row["value"] for row in failed["halted"]], [1])
            for blocks, diagnostic in (
                (growth_blocks, "uninitialized"),
                (zero_blocks, "inactive heap"),
            ):
                checkpoint = executor.create(
                    self._nullable_heap_program(blocks, capacity=1)
                )
                with self.assertRaisesRegex(ValueError, diagnostic):
                    executor.resume(checkpoint, max_steps=16)

    def test_symbolic_free_does_not_occupy_unrelated_fixed_pool(self):
        program = {
            "schema": "symcc-live-program-v1",
            "entry": "main",
            "input_size": 1,
            "memory_size": 76,
            "memory_objects": [
                {
                    "name": "heap:dynamic:0",
                    "kind": "heap",
                    "site": "dynamic",
                    "slot": 0,
                    "capacity": 1,
                    "address": 64,
                    "size": 4,
                    "logical_size": "runtime-allocation-size",
                    "read_only": False,
                    "lifetime": "runtime-alloc-free",
                    "allocation": "bounded-pool-nullable",
                },
                *[
                    {
                        "name": f"heap:fixed:{slot}",
                        "kind": "heap",
                        "site": "fixed",
                        "slot": slot,
                        "capacity": 2,
                        "address": 68 + slot * 4,
                        "size": 4,
                        "read_only": False,
                        "lifetime": "runtime-alloc-free",
                        "allocation": "bounded-pool-infallible",
                    }
                    for slot in range(2)
                ],
            ],
            "functions": {
                "main": {
                    "entry": "entry",
                    "params": [],
                    "blocks": {
                        "entry": [
                            {
                                "op": "input",
                                "dst": "size",
                                "offset": 0,
                            },
                            {
                                "op": "heap_alloc",
                                "dst": "dynamic",
                                "addresses": [64],
                                "capacity": 1,
                                "size": {"var": "size"},
                                "size_bits": 8,
                                "max_size": 4,
                                "nullable": True,
                                "site": "dynamic",
                                "bits": 64,
                            },
                            {
                                "op": "heap_free",
                                "address": {"var": "dynamic"},
                            },
                            {
                                "op": "heap_alloc",
                                "dst": "fixed",
                                "addresses": [68, 72],
                                "capacity": 2,
                                "size": 4,
                                "site": "fixed",
                                "bits": 64,
                            },
                            {
                                "op": "return",
                                "value": {"var": "fixed"},
                            },
                        ],
                    },
                },
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            result = executor.resume(
                executor.create(program, input_bytes=b"\x01"),
                max_steps=16,
            )
        self.assertEqual([row["value"] for row in result["halted"]], [68])

    def test_heap_contract_and_recursive_reentry_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            invalid = self._heap_program()
            invalid["memory_objects"][0]["lifetime"] = "static"
            with self.assertRaisesRegex(ValueError, "heap.*contract"):
                executor.create(invalid)

            duplicate_site = self._heap_program()
            duplicate_site["memory_size"] = 72
            duplicate_site["memory_objects"].append(
                {
                    "name": "heap:main:1",
                    "kind": "heap",
                    "site": "main:heap:0",
                    "address": 68,
                    "size": 4,
                    "read_only": False,
                    "lifetime": "runtime-alloc-free",
                    "allocation": "bounded-infallible",
                }
            )
            with self.assertRaisesRegex(ValueError, "heap.*contract"):
                executor.create(duplicate_site)

            recursive = self._heap_program()
            recursive["functions"]["main"]["blocks"]["entry"] = [
                {
                    "op": "heap_alloc",
                    "dst": "pointer",
                    "address": 64,
                    "size": 4,
                    "site": "main:heap:0",
                    "bits": 64,
                },
                {"op": "heap_free", "address": {"var": "pointer"}},
                {
                    "op": "call",
                    "function": "main",
                    "args": [],
                    "dst": "again",
                },
                {"op": "return", "value": 0},
            ]
            with self.assertRaisesRegex(ValueError, "acyclic call graph"):
                executor.create(recursive)

    @staticmethod
    def _symbolic_alias_program():
        return {
            "schema": "symcc-live-program-v1",
            "entry": "main",
            "input_size": 1,
            "memory_size": 68,
            "memory_hex": ("00" * 64) + "01020304",
            "memory_objects": [
                {
                    "name": "bytes",
                    "kind": "static",
                    "address": 64,
                    "size": 4,
                    "read_only": False,
                },
            ],
            "functions": {
                "main": {
                    "entry": "entry",
                    "params": [],
                    "blocks": {
                        "entry": [
                            {
                                "op": "input",
                                "dst": "index",
                                "offset": 0,
                            },
                            {
                                "op": "pointer_offset",
                                "dst": "pointer",
                                "base": {"const": 64, "bits": 64},
                                "index": {"var": "index"},
                                "index_bits": 8,
                                "scale": {"const": 1, "bits": 64},
                                "bits": 64,
                            },
                            {
                                "op": "load",
                                "dst": "value",
                                "address": {"var": "pointer"},
                                "aliases": [64, 65, 66, 67],
                                "alias_index": {"var": "index"},
                                "alias_index_bits": 8,
                                "alias_index_min": 0,
                                "alias_index_max": 3,
                                "bits": 8,
                                "bytes": 1,
                            },
                            {
                                "op": "binary",
                                "operator": "eq",
                                "dst": "matched",
                                "left": {"var": "value"},
                                "right": {"const": 3, "bits": 8},
                                "bits": 1,
                            },
                            {
                                "op": "branch",
                                "condition": {"var": "matched"},
                                "true": "hit",
                                "false": "miss",
                            },
                        ],
                        "hit": [{"op": "return", "value": 1}],
                        "miss": [{"op": "return", "value": 0}],
                    },
                },
            },
        }

    def test_symbolic_alias_load_builds_finite_ite_and_forks(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            result = executor.resume(
                executor.create(
                    self._symbolic_alias_program(),
                    input_bytes=b"\0",
                ),
                max_steps=32,
                max_states=8,
            )
        self.assertEqual(sorted(row["value"] for row in result["halted"]), [0, 1])
        self.assertGreaterEqual(result["forks"], 1)

    def test_symbolic_alias_store_is_a_guarded_memory_update(self):
        program = self._symbolic_alias_program()
        program["functions"]["main"]["blocks"]["entry"][2:4] = [
            {
                "op": "store",
                "address": {"var": "pointer"},
                "aliases": [64, 65, 66, 67],
                "alias_index": {"var": "index"},
                "alias_index_bits": 8,
                "alias_index_min": 0,
                "alias_index_max": 3,
                "value": {"const": 9, "bits": 8},
                "bits": 8,
                "bytes": 1,
            },
            {
                "op": "load",
                "dst": "value",
                "address": {"const": 66, "bits": 64},
                "bits": 8,
                "bytes": 1,
            },
            {
                "op": "binary",
                "operator": "eq",
                "dst": "matched",
                "left": {"var": "value"},
                "right": {"const": 9, "bits": 8},
                "bits": 1,
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            result = executor.resume(
                executor.create(program, input_bytes=b"\0"),
                max_steps=32,
                max_states=8,
            )
        self.assertEqual(sorted(row["value"] for row in result["halted"]), [0, 1])

    def test_symbolic_alias_domain_rejects_wrapped_index(self):
        program = self._symbolic_alias_program()
        entry = program["functions"]["main"]["blocks"]["entry"]
        entry[1]["scale"] = {"const": 1 << 57, "bits": 64}
        entry[2]["aliases"] = [64]
        entry[2]["alias_index_min"] = 0
        entry[2]["alias_index_max"] = 0
        entry[1:1] = [
            {
                "op": "binary",
                "operator": "eq",
                "dst": "wrapped",
                "left": {"var": "index"},
                "right": {"const": 0x80, "bits": 8},
                "bits": 1,
            },
            {"op": "assume", "condition": {"var": "wrapped"}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            result = executor.resume(
                executor.create(program, input_bytes=b"\x80"),
                max_steps=16,
                max_states=4,
            )
        self.assertEqual(
            [row["status"] for row in result["halted"]],
            ["infeasible"],
        )

    @staticmethod
    def _pointer_union_program():
        return {
            "schema": "symcc-live-program-v1",
            "entry": "main",
            "input_size": 1,
            "memory_size": 66,
            "memory_hex": ("00" * 64) + "010a",
            "memory_objects": [
                {
                    "name": "left",
                    "kind": "static",
                    "address": 64,
                    "size": 1,
                    "read_only": False,
                },
                {
                    "name": "right",
                    "kind": "static",
                    "address": 65,
                    "size": 1,
                    "read_only": False,
                },
            ],
            "functions": {
                "main": {
                    "entry": "entry",
                    "params": [],
                    "blocks": {
                        "entry": [
                            {"op": "input", "dst": "raw", "offset": 0},
                            {
                                "op": "binary",
                                "operator": "ne",
                                "dst": "choose",
                                "left": {"var": "raw"},
                                "right": {"const": 0, "bits": 8},
                                "bits": 1,
                            },
                            {
                                "op": "select",
                                "dst": "pointer",
                                "condition": {"var": "choose"},
                                "true": {"const": 64, "bits": 64},
                                "false": {"const": 65, "bits": 64},
                                "bits": 64,
                            },
                            {
                                "op": "load",
                                "dst": "value",
                                "address": {"var": "pointer"},
                                "alias_cases": [
                                    {
                                        "addresses": [64],
                                        "guards": [
                                            {
                                                "value": {"var": "choose"},
                                                "equals": 1,
                                                "bits": 1,
                                            }
                                        ],
                                    },
                                    {
                                        "addresses": [65],
                                        "guards": [
                                            {
                                                "value": {"var": "choose"},
                                                "equals": 0,
                                                "bits": 1,
                                            }
                                        ],
                                    },
                                ],
                                "bits": 8,
                                "bytes": 1,
                            },
                            {
                                "op": "binary",
                                "operator": "eq",
                                "dst": "matched",
                                "left": {"var": "value"},
                                "right": {"const": 1, "bits": 8},
                                "bits": 1,
                            },
                            {
                                "op": "branch",
                                "condition": {"var": "matched"},
                                "true": "hit",
                                "false": "miss",
                            },
                        ],
                        "hit": [{"op": "return", "value": 1}],
                        "miss": [{"op": "return", "value": 0}],
                    },
                },
            },
        }

    def test_pointer_union_load_uses_case_guards_across_objects(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            result = executor.resume(
                executor.create(self._pointer_union_program(), input_bytes=b"\0"),
                max_steps=32,
                max_states=8,
            )
        self.assertEqual(sorted(row["value"] for row in result["halted"]), [0, 1])
        self.assertGreaterEqual(result["forks"], 1)

    def test_pointer_union_store_is_guarded_per_object(self):
        program = self._pointer_union_program()
        entry = program["functions"]["main"]["blocks"]["entry"]
        cases = entry[3]["alias_cases"]
        entry[3:5] = [
            {
                "op": "store",
                "address": {"var": "pointer"},
                "alias_cases": cases,
                "value": {"const": 7, "bits": 8},
                "bits": 8,
                "bytes": 1,
            },
            {
                "op": "load",
                "dst": "value",
                "address": {"const": 64, "bits": 64},
                "bits": 8,
                "bytes": 1,
            },
            {
                "op": "binary",
                "operator": "eq",
                "dst": "matched",
                "left": {"var": "value"},
                "right": {"const": 7, "bits": 8},
                "bits": 1,
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            result = executor.resume(
                executor.create(program, input_bytes=b"\0"),
                max_steps=32,
                max_states=8,
            )
        self.assertEqual(sorted(row["value"] for row in result["halted"]), [0, 1])

    def test_pointer_union_contract_rejects_unguarded_or_mixed_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            unguarded = self._pointer_union_program()
            unguarded["functions"]["main"]["blocks"]["entry"][3]["alias_cases"][0][
                "guards"
            ] = []
            with self.assertRaisesRegex(ValueError, "guards"):
                executor.create(unguarded, input_bytes=b"\0")

            mixed = self._pointer_union_program()
            mixed["functions"]["main"]["blocks"]["entry"][3]["alias_cases"][0][
                "addresses"
            ] = [64, 65]
            with self.assertRaisesRegex(ValueError, "one compatible"):
                executor.create(mixed, input_bytes=b"\0")

            conflicting = self._pointer_union_program()
            conflicting["functions"]["main"]["blocks"]["entry"][3]["aliases"] = [64]
            with self.assertRaisesRegex(ValueError, "conflicting"):
                executor.create(conflicting, input_bytes=b"\0")

    def test_overlapping_multibyte_alias_store_composes_byte_ites(self):
        program = self._symbolic_alias_program()
        program["functions"]["main"]["blocks"]["entry"] = [
            {
                "op": "input",
                "dst": "index",
                "offset": 0,
            },
            {
                "op": "pointer_offset",
                "dst": "pointer",
                "base": {"const": 64, "bits": 64},
                "index": {"var": "index"},
                "index_bits": 8,
                "scale": {"const": 1, "bits": 64},
                "bits": 64,
            },
            {
                "op": "store",
                "address": {"var": "pointer"},
                "aliases": [64, 65, 66],
                "alias_index": {"var": "index"},
                "alias_index_bits": 8,
                "alias_index_min": 0,
                "alias_index_max": 2,
                "value": {"const": 0xA1B2, "bits": 16},
                "bits": 16,
                "bytes": 2,
            },
            {"op": "return", "value": 0},
        ]
        expected = {0: 0xA1, 1: 0xB2, 2: 0x02}
        for concrete_index, expected_byte in expected.items():
            with tempfile.TemporaryDirectory() as tmp:
                store = LiveStateStore(tmp, page_size=64)
                executor = LiveContinuationExecutor(store)
                root = executor.create(program, input_bytes=bytes([concrete_index]))
                paused = executor.resume(root, max_steps=3)
                restored = store.restore_continuation(paused["frontier"][0])
                _concrete, symbolic = store.read_memory(
                    restored.descriptor.symbolic_memory_root, 65, 1
                )
                self.assertIn(0, symbolic)
                self.assertEqual(executor._concrete(symbolic[0]), expected_byte)

    def test_symbolic_heap_initialization_survives_checkpoint(self):
        program = self._heap_program()
        program["input_size"] = 1
        program["functions"]["main"]["blocks"]["entry"] = [
            {
                "op": "heap_alloc",
                "dst": "heap",
                "address": 64,
                "size": 4,
                "site": "main:heap:0",
                "bits": 64,
            },
            {"op": "input", "dst": "index", "offset": 0},
            {
                "op": "pointer_offset",
                "dst": "pointer",
                "base": {"var": "heap"},
                "index": {"var": "index"},
                "index_bits": 8,
                "scale": {"const": 1, "bits": 64},
                "bits": 64,
            },
            {
                "op": "store",
                "address": {"var": "pointer"},
                "aliases": [64, 65, 66, 67],
                "alias_index": {"var": "index"},
                "alias_index_bits": 8,
                "alias_index_min": 0,
                "alias_index_max": 3,
                "value": {"const": 7, "bits": 8},
                "bits": 8,
                "bytes": 1,
            },
            {
                "op": "load",
                "dst": "value",
                "address": {"var": "pointer"},
                "aliases": [64, 65, 66, 67],
                "alias_index": {"var": "index"},
                "alias_index_bits": 8,
                "alias_index_min": 0,
                "alias_index_max": 3,
                "bits": 8,
                "bytes": 1,
            },
            {"op": "heap_free", "address": {"var": "heap"}},
            {"op": "return", "value": {"var": "value"}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            store = LiveStateStore(tmp, page_size=64)
            executor = LiveContinuationExecutor(store)
            root = executor.create(program, input_bytes=b"\0")
            paused = executor.resume(root, max_steps=4)
            checkpoint = paused["frontier"][0]
            values = dict(store.restore_continuation(checkpoint).symbolic_store)
            self.assertTrue(
                all(f"@heap:init:{address}" in values for address in range(64, 68))
            )
            completed = LiveContinuationExecutor(store).resume(checkpoint, max_steps=16)
            self.assertEqual([row["value"] for row in completed["halted"]], [7])
            final_values = dict(
                store.restore_continuation(
                    completed["halted"][0]["checkpoint"]
                ).symbolic_store
            )
            self.assertFalse(any(name.startswith("@heap:") for name in final_values))

    def test_symbolic_alias_contract_rejects_duplicate_addresses(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            program = self._symbolic_alias_program()
            program["functions"]["main"]["blocks"]["entry"][2]["aliases"] = [64, 64]
            with self.assertRaisesRegex(ValueError, "duplicates"):
                executor.create(program, input_bytes=b"\0")

            invalid_scale = self._symbolic_alias_program()
            invalid_scale["functions"]["main"]["blocks"]["entry"][1]["scale"] = {
                "const": 0,
                "bits": 64,
            }
            with self.assertRaisesRegex(ValueError, "pointer-offset"):
                executor.create(invalid_scale, input_bytes=b"\0")

            missing_domain = self._symbolic_alias_program()
            del missing_domain["functions"]["main"]["blocks"]["entry"][2][
                "alias_index_min"
            ]
            with self.assertRaisesRegex(ValueError, "index domain"):
                executor.create(missing_domain, input_bytes=b"\0")


if __name__ == "__main__":
    unittest.main()
