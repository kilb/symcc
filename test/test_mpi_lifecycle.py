# RUN: python3 %s

import importlib
import hashlib
import os
import pickle
from pathlib import Path
import random
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
UTIL = ROOT / "util"
sys.path.insert(0, str(UTIL))


class MpiLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.lifecycle = importlib.import_module("mpi_lifecycle")
        self.runner = importlib.import_module("mpi_concolic_execution")

    def test_frontends_share_one_lifecycle_implementation(self):
        helper = importlib.import_module("mpi_fuzzing_helper")

        self.assertIs(
            helper._cooperative_shutdown_workers,
            self.lifecycle._cooperative_shutdown_workers,
        )
        self.assertIs(
            self.runner._cooperative_shutdown_workers,
            self.lifecycle._cooperative_shutdown_workers,
        )

    def test_hybrid_import_tolerates_invalid_startup_integer_controls(self):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(UTIL)
        environment["SYMCC_TIMEOUT"] = "not-an-integer"
        environment["SYMCC_MAX_DEPTH"] = "-9"
        output = subprocess.check_output(
            [
                sys.executable,
                "-c",
                "import mpi_fuzzing_helper as h; "
                "print(h.TIMEOUT_SEC, h.MAX_GENERATION_DEPTH)",
            ],
            env=environment,
            text=True,
            timeout=10,
        )
        self.assertEqual(output.strip(), "30 0")

    def test_hybrid_master_exception_stops_workers_before_abort(self):
        helper = importlib.import_module("mpi_fuzzing_helper")
        comm = mock.Mock()
        comm.Get_size.return_value = 3
        shutdown = {
            "acknowledged": {1, 2},
            "pending": (),
        }
        with mock.patch.object(
            helper, "master", side_effect=ValueError("invalid configuration")
        ), mock.patch.object(
            helper,
            "_cooperative_shutdown_workers",
            return_value=shutdown,
        ) as stop:
            self.assertFalse(
                helper._run_mpi_role(comm, mock.Mock(), 0)
            )
        stop.assert_called_once()

    def test_hybrid_master_rejects_a_coordinator_without_workers(self):
        helper = importlib.import_module("mpi_fuzzing_helper")
        comm = mock.Mock()
        comm.Get_size.return_value = 1

        self.assertFalse(helper.master(comm, mock.Mock()))

    def test_hybrid_master_reports_startup_conflicts_as_failure(self):
        helper = importlib.import_module("mpi_fuzzing_helper")
        comm = mock.Mock()
        comm.Get_size.return_value = 2
        shutdown_result = {
            "clean": True,
            "acknowledged": (1,),
            "pending": (),
            "elapsed": 0.01,
        }
        with tempfile.TemporaryDirectory() as temporary:
            arguments = mock.Mock()
            arguments.output_dir = temporary
            arguments.fuzzer_name = "afl-main"
            arguments.name = "symcc"
            (Path(temporary) / "symcc").mkdir()
            with mock.patch.dict(
                os.environ, {"SYMCC_RESUME": "0"}
            ), mock.patch.object(
                helper,
                "_cooperative_shutdown_workers",
                return_value=shutdown_result,
            ) as shutdown:
                self.assertFalse(helper.master(comm, arguments))
            shutdown.assert_called_once()

        with tempfile.TemporaryDirectory() as temporary:
            arguments.output_dir = temporary
            with mock.patch.object(
                helper,
                "_acquire_master_queue_lock",
                side_effect=RuntimeError("already owned"),
            ), mock.patch.object(
                helper,
                "_cooperative_shutdown_workers",
                return_value=shutdown_result,
            ) as shutdown:
                self.assertFalse(helper.master(comm, arguments))
            shutdown.assert_called_once()

    def test_master_queue_lock_fences_concurrent_writers_and_releases(self):
        helper = importlib.import_module("mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as temporary:
            path = os.path.join(temporary, "master.lock")
            first = helper._acquire_master_queue_lock(path)
            try:
                with self.assertRaisesRegex(RuntimeError, "already owns"):
                    helper._acquire_master_queue_lock(path)
            finally:
                first.close()
            replacement = helper._acquire_master_queue_lock(path)
            replacement.close()
        self.assertIs(
            helper._bounded_mpi_barrier,
            self.runner._bounded_mpi_barrier,
        )

    def test_live_state_graph_limits_are_shared_and_bounded(self):
        helper = importlib.import_module("mpi_fuzzing_helper")
        self.assertEqual(helper._live_state_graph_limits({}), {
            "max_graph_objects": 262_144,
            "max_graph_bytes": 256 * 1024 * 1024,
        })
        self.assertEqual(helper._live_state_graph_limits({
            "SYMCC_LIVE_GRAPH_MAX_OBJECTS": "0",
            "SYMCC_LIVE_GRAPH_MAX_BYTES": "invalid",
        }), {
            "max_graph_objects": 1,
            "max_graph_bytes": 256 * 1024 * 1024,
        })
        self.assertEqual(helper._live_state_graph_limits({
            "SYMCC_LIVE_GRAPH_MAX_OBJECTS": "999999999",
            "SYMCC_LIVE_GRAPH_MAX_BYTES": str(2 ** 60),
        }), {
            "max_graph_objects": 10_000_000,
            "max_graph_bytes": helper._MAX_HYBRID_RESULT_BYTES,
        })

    def test_hybrid_shared_filesystem_preflight_enumerates_exact_roots(self):
        helper = importlib.import_module("mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            symcc = os.path.join(tmp, "symcc")
            work = os.path.join(tmp, "shared", "work")
            target = os.path.join(tmp, "shared", "target")
            coverage = os.path.join(tmp, "shared", "coverage")
            environment = {
                "SYMCC_MULTI_MASTER_LEASES": "1",
                "SYMCC_MULTI_MASTER_TARGET_LEASES": "1",
                "SYMCC_MULTI_MASTER_LEASE_DIR": work,
                "SYMCC_MULTI_MASTER_TARGET_LEASE_DIR": target,
                "SYMCC_COVERAGE_GOSSIP": "1",
                "SYMCC_COVERAGE_OWNER_DIR": coverage,
            }

            self.assertEqual(
                helper._shared_filesystem_preflight_roots(
                    symcc, environment),
                tuple(map(os.path.realpath, (work, target, coverage))),
            )
            contracts = helper._shared_filesystem_preflight_contracts(
                symcc, environment)
            self.assertIs(
                contracts[0][1], helper.LEASE_SHARED_FILESYSTEM_REQUIREMENTS)
            self.assertIs(
                contracts[1][1], helper.LEASE_SHARED_FILESYSTEM_REQUIREMENTS)
            self.assertIs(
                contracts[2][1],
                helper.COVERAGE_SHARED_FILESYSTEM_REQUIREMENTS,
            )

            environment["SYMCC_MULTI_MASTER_TARGET_LEASES"] = "0"
            environment["SYMCC_COVERAGE_OWNER_DIR"] = work
            self.assertEqual(
                helper._shared_filesystem_preflight_roots(
                    symcc, environment),
                (os.path.realpath(work),),
            )
            co_located = helper._shared_filesystem_preflight_contracts(
                symcc, environment)
            self.assertEqual(len(co_located), 1)
            self.assertIs(
                co_located[0][1], helper.LEASE_SHARED_FILESYSTEM_REQUIREMENTS)

            self.assertEqual(
                helper._shared_filesystem_preflight_roots(symcc, {}),
                (),
            )

    def test_hybrid_shared_filesystem_failure_precedes_service_startup(self):
        helper = importlib.import_module("mpi_fuzzing_helper")
        communication = mock.Mock()
        communication.Get_size.return_value = 2
        shutdown_result = {
            "clean": True,
            "acknowledged": (1,),
            "pending": (),
            "elapsed": 0.01,
        }
        with tempfile.TemporaryDirectory() as tmp:
            arguments = mock.Mock()
            arguments.output_dir = tmp
            arguments.fuzzer_name = "afl-main"
            arguments.name = "symcc"
            environment = {
                "SYMCC_MULTI_MASTER_LEASES": "1",
                "SYMCC_MULTI_MASTER_TARGET_LEASES": "1",
                "SYMCC_COVERAGE_GOSSIP": "1",
                "SYMCC_SHARED_STATE_FS_PROBE": "1",
                # A service would be started later if preflight did not gate it.
                "SYMCC_ASYNC_QUERY_WORKERS": "2",
            }
            with mock.patch.dict(os.environ, environment), mock.patch.object(
                    helper,
                    "probe_shared_state_filesystem",
                    side_effect=RuntimeError("incompatible storage"),
            ) as probe, mock.patch.object(
                    helper,
                    "_cooperative_shutdown_workers",
                    return_value=shutdown_result,
            ) as shutdown:
                self.assertFalse(helper.master(communication, arguments))

            probe.assert_called_once()
            self.assertIs(
                probe.call_args.kwargs["requirements"],
                helper.LEASE_SHARED_FILESYSTEM_REQUIREMENTS,
            )
            shutdown.assert_called_once()
            output = Path(tmp) / "symcc"
            self.assertFalse((output / ".objects").exists())
            self.assertFalse((output / ".query_service.log").exists())

    def test_invalid_afl_configuration_fails_and_releases_master_lock(self):
        helper = importlib.import_module("mpi_fuzzing_helper")
        communication = mock.Mock()
        communication.Get_size.return_value = 2
        shutdown_result = {
            "clean": True,
            "acknowledged": (1,),
            "pending": (),
            "elapsed": 0.01,
        }
        with tempfile.TemporaryDirectory() as temporary:
            arguments = mock.Mock()
            arguments.output_dir = temporary
            arguments.fuzzer_name = "missing-afl"
            arguments.name = "symcc"
            with mock.patch.object(
                helper,
                "_shared_filesystem_preflight_contracts",
                return_value=(),
            ), mock.patch.object(
                helper,
                "AflConfig",
                side_effect=ValueError("invalid campaign"),
            ), mock.patch.object(
                helper,
                "_cooperative_shutdown_workers",
                return_value=shutdown_result,
            ) as shutdown:
                self.assertFalse(helper.master(communication, arguments))

            shutdown.assert_called_once()
            lock_path = Path(temporary) / "symcc" / ".master-queue.lock"
            replacement = helper._acquire_master_queue_lock(str(lock_path))
            replacement.close()

    def test_exited_query_service_is_retired_and_disables_dependents(self):
        helper = importlib.import_module("mpi_fuzzing_helper")
        process = mock.Mock()
        process.poll.return_value = 17
        log_stream = mock.Mock()
        dependent = mock.Mock()
        dependent.query_store = "active"

        with mock.patch.object(helper.os, "killpg") as killpg:
            retired, retired_log, returncode = helper._retire_exited_query_service(
                process,
                log_stream,
                dependent,
            )

        self.assertIsNone(retired)
        self.assertIsNone(retired_log)
        self.assertEqual(returncode, 17)
        log_stream.close.assert_called_once_with()
        self.assertIsNone(dependent.query_store)
        killpg.assert_called_once_with(process.pid, helper.signal.SIGKILL)

        process.poll.return_value = None
        running, running_log, returncode = helper._retire_exited_query_service(
            process,
            log_stream,
            dependent,
        )
        self.assertIs(running, process)
        self.assertIs(running_log, log_stream)
        self.assertIsNone(returncode)

    def test_query_service_termination_escalates_for_complete_session(self):
        helper = importlib.import_module("mpi_fuzzing_helper")
        process = mock.Mock()
        process.pid = 12345
        process.wait.side_effect = (
            subprocess.TimeoutExpired("query-service", 0.1),
            0,
        )
        with mock.patch.object(helper.os, "killpg") as killpg:
            helper._terminate_query_service(process, timeout=0.1)
        self.assertEqual(killpg.call_args_list, [
            mock.call(12345, helper.signal.SIGTERM),
            mock.call(12345, helper.signal.SIGKILL),
        ])
        self.assertEqual(process.wait.call_count, 2)
        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    def test_afl_config_preserves_quoted_and_embedded_placeholder_arguments(self):
        helper = importlib.import_module("mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "queue").mkdir()
            (root / "fuzzer_stats").write_text(
                "command_line : afl-fuzz -i in -o out -- "
                "'/tmp/target with space' --input=@@ 'literal argument'\n",
                encoding="ascii",
            )
            config = helper.AflConfig(str(root), max_input_bytes=64)

            self.assertEqual(config.target_command, [
                "/tmp/target with space",
                "--input=@@",
                "literal argument",
            ])
            self.assertFalse(config.use_stdin)

            with mock.patch.object(
                helper.subprocess,
                "run",
                return_value=mock.Mock(returncode=2),
            ) as run:
                result_type, bitmap = config.run_showmap(
                    "/tmp/seed file",
                    str(root / "bitmap"),
                )
            self.assertEqual((result_type, bitmap), ("crash", None))
            command = run.call_args.args[0]
            self.assertIn("--input=/tmp/seed file", command)

            for command_line, message in (
                ("command_line :\n", "empty"),
                ("command_line : afl-fuzz -i in --\n", "no target"),
                ("command_line_extra : afl-fuzz -- /bin/true\n", "find"),
            ):
                with self.subTest(command_line=command_line):
                    (root / "fuzzer_stats").write_text(
                        command_line,
                        encoding="ascii",
                    )
                    with self.assertRaisesRegex(RuntimeError, message):
                        helper.AflConfig(str(root), max_input_bytes=64)

    def test_ready_is_parked_until_active_result_retires(self):
        gate = self.runner._WorkerAvailabilityGate((1, 2))
        active = {1: "seed-a"}

        self.assertEqual(gate.observe_ready(1), "ready")
        self.assertEqual(gate.observe_ready(1), "duplicate")
        self.assertEqual(gate.observe_ready(9), "unowned")
        self.assertIsNone(gate.claim(active))
        self.assertEqual(gate.ready, (1,))

        active.pop(1)
        self.assertEqual(gate.claim(active), 1)
        self.assertEqual(gate.ready, ())

        self.assertTrue(gate.quarantine(2))
        self.assertFalse(gate.quarantine(9))
        self.assertEqual(gate.observe_ready(2), "quarantined")
        self.assertIsNone(gate.claim({}))
        self.assertEqual(gate.ready, (2,))

    def test_symcc_cpu_affinity_gives_master_reserved_pool(self):
        helper = importlib.import_module("mpi_fuzzing_helper")
        assigned = []
        with mock.patch.dict(os.environ, {"SYMCC_CPU_LIST": "8,9,10"}), \
                mock.patch.object(
                    helper.os,
                    "sched_setaffinity",
                    side_effect=lambda pid, cpus: assigned.append((pid, set(cpus))),
                    create=True,
                ):
            helper._pin_self_to_reserved_core(0)
            helper._pin_self_to_reserved_core(2)
        self.assertEqual(assigned, [(0, {8, 9, 10}), (0, {10})])

    def test_hybrid_returncode_classification_uses_fatal_signal_set(self):
        helper = importlib.import_module("mpi_fuzzing_helper")

        self.assertTrue(helper._returncode_indicates_crash(139))
        self.assertTrue(
            helper._returncode_indicates_crash(-helper.signal.SIGABRT)
        )
        self.assertFalse(helper._returncode_indicates_crash(143))
        self.assertFalse(
            helper._returncode_indicates_crash(-helper.signal.SIGTERM)
        )
        self.assertFalse(helper._returncode_indicates_crash(130))
        self.assertFalse(helper._returncode_indicates_crash(137))
        self.assertFalse(helper._returncode_indicates_crash(139, killed=True))
        self.assertTrue(helper._returncode_indicates_timeout(124))
        self.assertTrue(helper._returncode_indicates_timeout(137))

    def test_failed_assignment_is_requeued_under_the_exact_fence(self):
        runner = self.runner
        work_hash = hashlib.sha256(b"retry-exact-work").hexdigest()
        token = "lease-token"
        pending = runner.deque()
        availability = runner._WorkerAvailabilityGate((1, 2))

        self.assertEqual(runner._requeue_failed_assignment(
            pending,
            (work_hash, token),
            availability,
            1,
            current_lease_token=token,
        ), "requeued")
        self.assertEqual(tuple(pending), ((work_hash, token),))
        self.assertEqual(availability.usable, (2,))
        availability.observe_ready(1)
        availability.observe_ready(2)
        self.assertEqual(availability.claim({}), 2)

        stale = runner.deque()
        self.assertEqual(runner._requeue_failed_assignment(
            stale,
            (work_hash, token),
            runner._WorkerAvailabilityGate((1, 2)),
            1,
            current_lease_token="replacement-token",
        ), "stale")
        self.assertEqual(tuple(stale), ())

        exhausted = runner.deque()
        self.assertEqual(runner._requeue_failed_assignment(
            exhausted,
            (work_hash, ""),
            runner._WorkerAvailabilityGate((1,)),
            1,
        ), "exhausted")
        self.assertEqual(tuple(exhausted), ((work_hash, ""),))

    def test_standalone_ulfm_hot_path_binds_dispatch_and_result(self):
        runner = self.runner
        with tempfile.TemporaryDirectory() as tmp:
            durable, shards = runner._initialize_ulfm_hot_path(
                run_id="standalone-test-session",
                master_global_rank=0,
                worker_global_ranks=(1, 2),
                endpoint_hosts={0: "node-a", 1: "node-a", 2: "node-b"},
                store_root=str(Path(tmp) / "query-store"),
            )
            work_hash = hashlib.sha256(b"generation-fenced-work").hexdigest()
            lease = durable.dispatch(shards[1], work_hash)
            message = runner.build_work_envelope(
                {"input_hash": work_hash}, lease
            )
            decoded_hash, fence = runner._decode_standalone_work_message(message)
            self.assertEqual(decoded_hash, work_hash)
            self.assertIsNotNone(fence)
            self.assertEqual(durable.classify_result(fence), "current")

            result = runner._with_ulfm_result_fence(
                {
                    "new_hashes": [],
                    "num_generated": 0,
                    "staged_bytes": 0,
                    "input_hash": work_hash,
                    "staging_id": "",
                    "protocol_error": "",
                },
                fence,
            )
            self.assertEqual(result["ulfm_fence"], fence)
            durable.finish(fence, hashlib.sha256(b"commit").hexdigest())
            self.assertEqual(durable.classify_result(fence), "stale")

            legacy_hash, legacy_fence = runner._decode_standalone_work_message(
                work_hash
            )
            self.assertEqual(legacy_hash, work_hash)
            self.assertIsNone(legacy_fence)
            self.assertNotIn(
                "ulfm_fence",
                runner._with_ulfm_result_fence({"ok": True}, None),
            )

            tampered = dict(message)
            tampered["payload"] = {"input_hash": hashlib.sha256(b"x").hexdigest()}
            with self.assertRaisesRegex(ValueError, "envelope"):
                runner._decode_standalone_work_message(tampered)

    def test_standalone_ulfm_restore_rebinds_survivors_and_priority_work(self):
        runner = self.runner
        recovery = importlib.import_module("mpi_ulfm_recovery")
        hosts = {0: "node-a", 1: "node-a", 2: "node-b"}
        with tempfile.TemporaryDirectory() as tmp:
            store_root = str(Path(tmp) / "query-store")
            durable, shards = runner._initialize_ulfm_hot_path(
                run_id="standalone-recovery-session",
                master_global_rank=0,
                worker_global_ranks=(1, 2),
                endpoint_hosts=hosts,
                store_root=store_root,
            )
            work_hash = hashlib.sha256(b"recover-this-dispatch").hexdigest()
            old_lease = durable.dispatch(shards[1], work_hash)
            plan = durable.prepare_recovery(["rank-1"])
            attestations = []
            for new_rank, stable_rank in enumerate((0, 2)):
                member = plan["base_members"][f"rank-{stable_rank}"]
                endpoint = runner.EndpointIdentity(
                    endpoint_id=f"rank-{stable_rank}",
                    initial_rank=int(member["rank"]),
                    incarnation_sha256=member["incarnation_sha256"],
                    host_id=member["host_id"],
                )
                attestations.append(recovery.build_endpoint_attestation(
                    run_id=plan["run_id"],
                    base_generation=plan["base_generation"],
                    base_generation_token=plan["base_generation_token"],
                    endpoint=endpoint,
                    old_rank=int(member["rank"]),
                    new_rank=new_rank,
                ))
            durable.commit_recovery(plan["plan_sha256"], attestations)

            restored, repaired_shards = runner._initialize_ulfm_hot_path(
                run_id="standalone-recovery-session",
                master_global_rank=0,
                worker_global_ranks=(1, 2),
                endpoint_hosts=hosts,
                store_root=store_root,
                transport_endpoint_ranks={0: 0, 1: 2},
            )
            self.assertEqual(restored.controller.generation, 1)
            self.assertEqual(set(repaired_shards), {1})
            replay = restored.dispatch(repaired_shards[1], work_hash)
            self.assertNotEqual(replay["lease_token"], old_lease["lease_token"])
            self.assertEqual(restored.classify_result(
                runner.build_work_fence(replay)), "current")
            self.assertEqual(restored.controller.snapshot()["recovery_queue"], [])

    def test_global_ulfm_membership_and_generation_layout_preserve_identity(self):
        runner = self.runner
        recovery = importlib.import_module("mpi_ulfm_recovery")
        hosts = {
            0: "node-a", 1: "node-a", 2: "node-a", 3: "node-a",
            4: "node-b", 5: "node-b", 6: "node-b", 7: "node-b",
        }
        with tempfile.TemporaryDirectory() as tmp:
            store_root = str(Path(tmp) / "global-membership")
            durable = runner._initialize_ulfm_membership(
                run_id="global-membership-test",
                endpoint_hosts=hosts,
                store_root=store_root,
                transport_endpoint_ranks={rank: rank for rank in hosts},
            )
            plan = durable.prepare_recovery(["rank-2"])
            attestations = []
            survivors = (0, 1, 3, 4, 5, 6, 7)
            for new_rank, stable_rank in enumerate(survivors):
                member = plan["base_members"][f"rank-{stable_rank}"]
                endpoint = runner.EndpointIdentity(
                    endpoint_id=f"rank-{stable_rank}",
                    initial_rank=int(member["rank"]),
                    incarnation_sha256=member["incarnation_sha256"],
                    host_id=member["host_id"],
                )
                attestations.append(recovery.build_endpoint_attestation(
                    run_id=plan["run_id"],
                    base_generation=plan["base_generation"],
                    base_generation_token=plan["base_generation_token"],
                    endpoint=endpoint,
                    old_rank=int(member["rank"]),
                    new_rank=new_rank,
                ))
            durable.commit_recovery(plan["plan_sha256"], attestations)
            transport = {
                rank: stable for rank, stable in enumerate(survivors)
            }
            restored = runner._initialize_ulfm_membership(
                run_id="global-membership-test",
                endpoint_hosts=hosts,
                store_root=store_root,
                transport_endpoint_ranks=transport,
            )
            self.assertEqual(restored.controller.generation, 1)

            masters, transport_groups, stable_groups = (
                runner._ulfm_generation_layout(transport, 3)
            )
            self.assertEqual(masters, [0, 1])
            self.assertEqual(transport_groups, {0: [2, 4, 6], 1: [3, 5]})
            self.assertEqual(stable_groups, {0: [3, 5, 7], 1: [4, 6]})
            assigned = [worker for group in stable_groups.values()
                        for worker in group]
            self.assertEqual(sorted(assigned), [3, 4, 5, 6, 7])
            with self.assertRaisesRegex(
                runner.UlfmRuntimeError, "dense and unique"
            ):
                runner._ulfm_generation_layout({0: 0, 2: 3}, 3)

            initial_transport = {rank: rank for rank in range(8)}
            initial_masters, initial_groups, _stable = (
                runner._ulfm_generation_layout(
                    {rank: rank for rank in range(6)}, 3
                )
            )
            manifest = runner._ulfm_generation_manifest(
                generation=0,
                active_budget=6,
                transport_endpoint_ranks=initial_transport,
                master_ranks=initial_masters,
                transport_worker_groups=initial_groups,
            )
            self.assertEqual(manifest["active_endpoints"], [0, 1, 2, 3, 4, 5])
            self.assertEqual(manifest["standby_endpoints"], [6, 7])
            path = runner._record_ulfm_generation_manifest(
                store_root, manifest
            )
            self.assertEqual(
                runner._record_ulfm_generation_manifest(store_root, manifest),
                path,
            )
            observed = runner.json.loads(
                Path(path).read_text(encoding="ascii")
            )
            self.assertEqual(observed, manifest)

            repaired_masters, repaired_groups, _stable = (
                runner._ulfm_generation_layout(
                    {rank: transport[rank] for rank in range(6)}, 3
                )
            )
            promoted = runner._ulfm_generation_manifest(
                generation=1,
                active_budget=6,
                transport_endpoint_ranks=transport,
                master_ranks=repaired_masters,
                transport_worker_groups=repaired_groups,
            )
            self.assertEqual(promoted["active_endpoints"], [0, 1, 3, 4, 5, 6])
            self.assertEqual(promoted["standby_endpoints"], [7])

    def test_ulfm_survivor_discovery_does_not_require_local_failed_group(self):
        runner = self.runner

        class Request:
            freed = False

            def Test(self):
                return True

            def Free(self):
                self.freed = True

        class Repaired:
            freed = False
            agreements = 0

            def Set_errhandler(self, _handler):
                return None

            def Get_size(self):
                return 3

            def Iallgather(self, _send, receive):
                receive[0][:] = runner.array("q", [0, 1, 3])
                return Request()

            def Iagree(self, flag):
                self.agreements += 1
                return Request()

            def Free(self):
                self.freed = True

        class Failed:
            revoked = 0

            def Set_errhandler(self, _handler):
                return None

            def Revoke(self):
                self.revoked += 1

            def Shrink(self):
                return repaired

            def Ishrink(self):
                return repaired, Request()

            def Get_failed(self):
                raise AssertionError("local failed group must not be consulted")

        repaired = Repaired()
        failed = Failed()
        members = {
            f"rank-{rank}": {
                "rank": rank,
                "incarnation_sha256": hashlib.sha256(
                    f"rank-{rank}".encode("ascii")
                ).hexdigest(),
                "host_id": "node-a" if rank < 3 else "node-b",
            }
            for rank in range(4)
        }
        policy = runner.UlfmRecoveryPolicy(
            max_endpoints=4,
            max_shards=3,
            max_recovery_queue=3,
            collective_timeout_seconds=1.0,
        )
        observed_comm, failed_endpoints = (
            runner._shrink_and_discover_ulfm_failures(
                failed,
                local_endpoint_rank=3,
                members=members,
                policy=policy,
            )
        )
        self.assertIs(observed_comm, repaired)
        self.assertEqual(failed_endpoints, ("rank-2",))
        self.assertEqual(failed.revoked, 1)
        self.assertEqual(repaired.agreements, 1)

    def test_ulfm_hot_path_store_is_generation_scoped(self):
        runner = self.runner
        root0 = runner._ulfm_hot_path_store_root("/state", 4, 0)
        root1 = runner._ulfm_hot_path_store_root("/state", 4, 1)
        self.assertEqual(
            root0,
            "/state/ulfm-query-store/master-4/generation-0",
        )
        self.assertEqual(
            root1,
            "/state/ulfm-query-store/master-4/generation-1",
        )
        self.assertNotEqual(root0, root1)
        with self.assertRaisesRegex(ValueError, "scope"):
            runner._ulfm_hot_path_store_root("/state", 4, -1)

    def test_worker_control_wait_polls_global_ulfm_membership(self):
        runner = self.runner

        class Group:
            def iprobe(self, *, source, tag):
                return False

        class FailedGroup:
            def Get_size(self):
                return 1

            def Free(self):
                return None

        class Recovery:
            revoked = False

            def Ack_failed(self):
                return None

            def Get_failed(self):
                return FailedGroup()

            def Get_size(self):
                return 8

            def Revoke(self):
                self.revoked = True

        recovery_comm = Recovery()
        ticks = iter((0.1, 0.2))
        with mock.patch.dict(
            os.environ, {"SYMCC_ULFM_FAILURE_POLL_INTERVAL": "0.05"}
        ):
            with self.assertRaisesRegex(
                runner._UlfmProcessFailureDetected, "1 failed member"
            ):
                runner._receive_worker_control(
                    Group(),
                    recovery_comm,
                    status=object(),
                    monotonic=lambda: next(ticks),
                    sleep=lambda _seconds: None,
                )
        self.assertTrue(recovery_comm.revoked)

    def test_ulfm_transport_sentinel_revokes_global_membership(self):
        runner = self.runner

        class Failure(Exception):
            def Get_error_class(self):
                return runner.MPI.ERR_PROC_FAILED

        class FailedRequest:
            def Test(self):
                raise Failure("worker transport closed")

        class Recovery:
            revoked = False

            def Revoke(self):
                self.revoked = True

        recovery = Recovery()
        sentinels = {2: (bytearray(1), FailedRequest())}
        with self.assertRaisesRegex(
            runner._UlfmProcessFailureDetected,
            "failed worker 2",
        ):
            runner._poll_ulfm_failure_sentinels(
                sentinels,
                group_comm=object(),
                recovery_comm=recovery,
            )
        self.assertTrue(recovery.revoked)

    def test_ulfm_transport_sentinels_are_posted_and_retired(self):
        runner = self.runner

        class Request:
            cancelled = False
            waited = False

            def Cancel(self):
                self.cancelled = True

            def Wait(self):
                self.waited = True

        class Group:
            def __init__(self):
                self.calls = []
                self.requests = []

            def Irecv(self, buffer, *, source, tag):
                request = Request()
                self.calls.append((buffer, source, tag))
                self.requests.append(request)
                return request

        group = Group()
        sentinels = runner._start_ulfm_failure_sentinels(group, (1, 3))
        self.assertEqual(set(sentinels), {1, 3})
        self.assertEqual(
            [(source, tag) for _buffer, source, tag in group.calls],
            [
                (1, runner.TAG_ULFM_FAILURE_SENTINEL),
                (3, runner.TAG_ULFM_FAILURE_SENTINEL),
            ],
        )
        runner._cancel_ulfm_failure_sentinels(sentinels)
        self.assertTrue(all(request.cancelled for request in group.requests))
        self.assertTrue(all(request.waited for request in group.requests))
        with self.assertRaisesRegex(ValueError, "unique and positive"):
            runner._start_ulfm_failure_sentinels(group, (1, 1))

    def test_ulfm_snapshot_file_mode_is_strict_octal(self):
        runner = self.runner
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SYMCC_ULFM_SNAPSHOT_FILE_MODE", None)
            self.assertEqual(runner._ulfm_snapshot_file_mode(), 0o600)
        with mock.patch.dict(
            os.environ, {"SYMCC_ULFM_SNAPSHOT_FILE_MODE": "0666"}
        ):
            self.assertEqual(runner._ulfm_snapshot_file_mode(), 0o666)
        for invalid in ("0777", "0400", "0o660", "not-octal"):
            with mock.patch.dict(
                os.environ,
                {"SYMCC_ULFM_SNAPSHOT_FILE_MODE": invalid},
            ), self.assertRaises(ValueError):
                runner._ulfm_snapshot_file_mode()

    def test_ulfm_physical_failure_injection_is_strict_and_one_shot(self):
        runner = self.runner
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"SYMCC_ULFM_TEST_FAIL_INITIAL_RANK": "7"},
        ):
            self.assertFalse(runner._claim_ulfm_test_failure(tmp, 6))
            self.assertTrue(runner._claim_ulfm_test_failure(tmp, 7))
            self.assertFalse(runner._claim_ulfm_test_failure(tmp, 7))
            self.assertEqual(
                (Path(tmp) / ".ulfm-test-failure-rank-7").read_text(
                    encoding="ascii"
                ),
                "claimed\n",
            )
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"SYMCC_ULFM_TEST_FAIL_INITIAL_RANK": "not-an-integer"},
        ):
            with self.assertRaisesRegex(ValueError, "must be an integer"):
                runner._claim_ulfm_test_failure(tmp, 7)

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {
                "SYMCC_ULFM_TEST_FAILURE_SCHEDULE": "0:7,1:3,2:0",
                "SYMCC_ULFM_TEST_FAIL_INITIAL_RANK": "99",
            },
        ):
            self.assertTrue(runner._claim_ulfm_test_failure(tmp, 7, 0))
            self.assertFalse(runner._claim_ulfm_test_failure(tmp, 7, 0))
            self.assertTrue(runner._claim_ulfm_test_failure(tmp, 3, 1))
            self.assertFalse(runner._claim_ulfm_test_failure(tmp, 3, 2))
            self.assertTrue(runner._claim_ulfm_test_failure(tmp, 0, 2))
            self.assertEqual(
                (Path(tmp) /
                 ".ulfm-test-failure-generation-1-rank-3").read_bytes(),
                b"claimed\n",
            )
        with mock.patch.dict(
            os.environ,
            {"SYMCC_ULFM_TEST_FAILURE_SCHEDULE": "0:7,0:3"},
        ):
            with self.assertRaisesRegex(ValueError, "unique non-negative"):
                runner._ulfm_test_failure_target(0)

    def test_quiescence_refresh_observes_last_moment_work(self):
        runner = self.runner
        pending = []
        events = []

        def import_external():
            events.append("external")

        def scan_corpus():
            events.append("corpus")
            pending.append("late-input")

        def recover_lease():
            events.append("lease")

        self.assertFalse(runner._refresh_quiescence_frontier(
            (import_external, scan_corpus, recover_lease),
            lambda: not pending,
        ))
        self.assertEqual(events, ["external", "corpus", "lease"])
        self.assertTrue(runner._refresh_quiescence_frontier(
            (lambda: None,), lambda: True))

    def test_atomic_corpus_publish_is_verified_and_reports_failure(self):
        runner = self.runner
        state = importlib.import_module("distributed_state")
        content = b"content-addressed-object"
        with tempfile.TemporaryDirectory() as tmp:
            dest = str(Path(tmp) / hashlib.sha256(content).hexdigest())
            runner._atomic_write(dest, content)
            self.assertEqual(runner._file_sha256(dest), Path(dest).name)

            failed = str(Path(tmp) / "failed")
            with mock.patch.object(
                    runner.os, "replace", side_effect=OSError("offline")):
                with self.assertRaisesRegex(OSError, "offline"):
                    runner._atomic_write(failed, content)
            self.assertFalse(Path(failed).exists())
            self.assertEqual(list(Path(tmp).glob("failed.tmp.*")), [])

            uncertain = str(Path(tmp) / "uncertain")
            with mock.patch.object(
                    state, "fsync_directory",
                    side_effect=OSError("directory-offline")):
                with self.assertRaisesRegex(OSError, "directory-offline"):
                    runner._atomic_write(uncertain, content)
            self.assertEqual(Path(uncertain).read_bytes(), content)
            self.assertEqual(list(Path(tmp).glob("uncertain.tmp.*")), [])

    def test_corpus_digest_is_bound_to_a_real_opened_regular_inode(self):
        runner = self.runner
        content = b"descriptor-bound-content"
        work_hash = hashlib.sha256(content).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            regular = root / work_hash
            regular.write_bytes(content)
            alias = root / "alias"
            alias.symlink_to(regular)

            self.assertEqual(runner._file_sha256(str(regular)), work_hash)
            self.assertEqual(runner._file_sha256(str(alias)), "")
            self.assertEqual(runner._file_sha256(str(root)), "")

        with mock.patch.object(
                runner.os, "O_NOFOLLOW", None), mock.patch.object(
                runner.os, "open") as opened:
            self.assertEqual(runner._file_sha256("untrusted"), "")
            opened.assert_not_called()

        with mock.patch.object(
                runner.os, "open", return_value=73), mock.patch.object(
                runner.os, "fstat", side_effect=OSError("metadata")), \
                mock.patch.object(runner.os, "close") as closed:
            self.assertEqual(runner._file_sha256("unreadable"), "")
            closed.assert_called_once_with(73)

    def test_input_snapshot_publish_and_worker_copy_are_streamed_and_bounded(self):
        runner = self.runner
        content = b"s" * (runner._RESULT_STREAM_CHUNK_BYTES + 137)
        work_hash = hashlib.sha256(content).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "seed"
            source.write_bytes(content)
            shared = root / "shared"
            shared.mkdir()
            worker_input = root / "worker" / "current_input"
            worker_input.parent.mkdir()

            original_read = os.read
            read_sizes = []

            def bounded_read(descriptor, size):
                read_sizes.append(size)
                return original_read(descriptor, size)

            with mock.patch.object(
                    runner.os, "read", side_effect=bounded_read):
                snapshot = runner._input_file_snapshot(
                    str(source), max_bytes=len(content))
                self.assertIsNotNone(snapshot)
                self.assertEqual(snapshot[:2], (work_hash, len(content)))
                published = runner._stream_publish_input_file(
                    str(source),
                    str(shared),
                    expected_hash=work_hash,
                    expected_identity=snapshot[2],
                    max_bytes=len(content),
                )
                copied = runner._stream_copy_verified_input(
                    str(shared / work_hash),
                    str(worker_input),
                    expected_hash=work_hash,
                    max_bytes=len(content),
                )

            self.assertEqual(published, (work_hash, len(content)))
            self.assertEqual(copied, (work_hash, len(content)))
            self.assertEqual(worker_input.read_bytes(), content)
            self.assertTrue(read_sizes)
            self.assertLessEqual(
                max(read_sizes), runner._RESULT_STREAM_CHUNK_BYTES)
            self.assertEqual(tuple(shared.glob(".input.*.tmp")), ())
            self.assertEqual(
                tuple(worker_input.parent.glob("current_input.tmp.*")), ())

            with self.assertRaises(runner._InputBudgetExceeded) as overflow:
                runner._input_file_snapshot(
                    str(source), max_bytes=len(content) - 1)
            self.assertEqual(overflow.exception.payload(), {
                "observed": len(content), "limit": len(content) - 1,
            })

            alias = root / "seed-alias"
            alias.symlink_to(source)
            self.assertIsNone(runner._input_file_snapshot(
                str(alias), max_bytes=len(content)))

    def test_simulation_mutations_stream_match_legacy_random_semantics(self):
        runner = self.runner
        content = bytes(range(251)) + b"cross-chunk-simulation-tail"
        seed = 0xF343
        expected = []
        legacy_rng = random.Random(seed)
        for _ in range(7):
            mutated = bytearray(content)
            count = legacy_rng.randint(1, min(3, len(mutated)))
            for _ in range(count):
                position = legacy_rng.randint(0, len(mutated) - 1)
                mutated[position] = legacy_rng.randint(0, 255)
            expected.append(bytes(mutated))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "input"
            output = root / "output"
            source.write_bytes(content)
            requested = []
            original_read = os.read

            def observed_read(descriptor, size):
                requested.append(size)
                return original_read(descriptor, size)

            original_write = os.write

            def partial_write(descriptor, data):
                return original_write(descriptor, data[:7])

            with mock.patch.object(
                    runner, "_RESULT_STREAM_CHUNK_BYTES", 31), \
                    mock.patch.object(
                        runner, "_SIMULATION_OUTPUT_BATCH_SIZE", 2), \
                    mock.patch.object(runner.os, "read", observed_read), \
                    mock.patch.object(runner.os, "write", partial_write):
                paths = runner._simulate_mutations(
                    str(source),
                    str(output),
                    num_mutations=7,
                    max_objects=7,
                    max_bytes=len(content) * 7,
                    _rng=random.Random(seed),
                )

            self.assertEqual(
                [Path(path).name for path in paths],
                [f"sim_{index:04d}" for index in range(7)],
            )
            self.assertEqual(
                [Path(path).read_bytes() for path in paths], expected)
            self.assertTrue(requested)
            self.assertLessEqual(max(requested), 31)
            self.assertEqual(tuple(output.glob(".sim.*.tmp")), ())

    def test_simulation_mutations_preflight_budgets_and_preserve_destinations(self):
        runner = self.runner
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "input"
            source.write_bytes(b"budget")

            for invalid in (-1, True, 1.5):
                with self.subTest(invalid=invalid):
                    with self.assertRaises(ValueError):
                        runner._simulate_mutations(
                            str(source),
                            str(root / f"invalid-{invalid}"),
                            invalid,
                        )

            object_output = root / "objects"
            with self.assertRaises(runner._ResultBudgetExceeded) as objects:
                runner._simulate_mutations(
                    str(source),
                    str(object_output),
                    3,
                    max_objects=2,
                    max_bytes=100,
                )
            self.assertEqual(objects.exception.payload(), {
                "resource": "objects", "observed": 3, "limit": 2,
            })
            self.assertFalse(object_output.exists())

            byte_output = root / "bytes"
            with self.assertRaises(runner._ResultBudgetExceeded) as size:
                runner._simulate_mutations(
                    str(source),
                    str(byte_output),
                    3,
                    max_objects=3,
                    max_bytes=17,
                )
            self.assertEqual(size.exception.payload(), {
                "resource": "bytes", "observed": 18, "limit": 17,
            })
            self.assertFalse(byte_output.exists())

            alias = root / "input-alias"
            alias.symlink_to(source)
            alias_output = root / "alias-output"
            self.assertEqual(runner._simulate_mutations(
                str(alias), str(alias_output), 1), [])
            self.assertFalse(alias_output.exists())

            empty = root / "empty"
            empty.write_bytes(b"")
            empty_output = root / "empty-output"
            self.assertEqual(runner._simulate_mutations(
                str(empty), str(empty_output), 1), [])
            self.assertFalse(empty_output.exists())

            occupied = root / "occupied"
            occupied.mkdir()
            existing = occupied / "sim_0000"
            existing.write_bytes(b"keep")
            with self.assertRaises(FileExistsError):
                runner._simulate_mutations(
                    str(source),
                    str(occupied),
                    1,
                    max_objects=1,
                    max_bytes=len(b"budget"),
                )
            self.assertEqual(existing.read_bytes(), b"keep")
            self.assertEqual(
                {path.name for path in occupied.iterdir()}, {"sim_0000"})

            raced = root / "raced"
            real_link = os.link

            def publish_after_racer(source_path, destination_path, **kwargs):
                Path(destination_path).write_bytes(b"racer")
                return real_link(source_path, destination_path, **kwargs)

            with mock.patch.object(
                    runner.os, "link", side_effect=publish_after_racer):
                with self.assertRaises(FileExistsError):
                    runner._simulate_mutations(
                        str(source),
                        str(raced),
                        1,
                        max_objects=1,
                        max_bytes=len(b"budget"),
                    )
            self.assertEqual(
                (raced / "sim_0000").read_bytes(), b"racer")
            self.assertEqual(
                {path.name for path in raced.iterdir()}, {"sim_0000"})

            rollback = root / "rollback"
            publication_calls = 0

            def fail_second_publication(source_path, destination_path, **kwargs):
                nonlocal publication_calls
                publication_calls += 1
                if publication_calls == 2:
                    raise OSError("injected simulation publication failure")
                return real_link(source_path, destination_path, **kwargs)

            with mock.patch.object(
                    runner.os, "link", side_effect=fail_second_publication):
                with self.assertRaisesRegex(
                        OSError, "injected simulation publication failure"):
                    runner._simulate_mutations(
                        str(source),
                        str(rollback),
                        3,
                        max_objects=3,
                        max_bytes=len(b"budget") * 3,
                    )
            self.assertTrue(rollback.is_dir())
            self.assertEqual(tuple(rollback.iterdir()), ())

    def test_simulation_mutations_clean_partial_outputs_on_failure(self):
        runner = self.runner
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "input"
            output = root / "output"
            source.write_bytes(b"stream-failure" * 20)
            original_write = runner._write_all_descriptor
            calls = 0

            def fail_second_write(descriptor, content):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected simulation write failure")
                return original_write(descriptor, content)

            with mock.patch.object(
                    runner,
                    "_write_all_descriptor",
                    side_effect=fail_second_write,
            ):
                with self.assertRaisesRegex(
                        OSError, "injected simulation write failure"):
                    runner._simulate_mutations(
                        str(source),
                        str(output),
                        3,
                        max_objects=3,
                        max_bytes=source.stat().st_size * 3,
                        _rng=random.Random(1),
                    )

            self.assertTrue(output.is_dir())
            self.assertEqual(tuple(output.iterdir()), ())

            identity = runner._regular_file_identity(source.stat())
            changed = runner._RegularFileIdentity(
                identity.device,
                identity.inode,
                identity.size,
                identity.mtime_ns,
                identity.ctime_ns + 1,
            )
            with mock.patch.object(
                    runner,
                    "_regular_file_identity",
                    side_effect=(identity, changed),
            ):
                with self.assertRaises(runner._SourceSnapshotChanged):
                    runner._simulate_mutations(
                        str(source),
                        str(output),
                        2,
                        max_objects=2,
                        max_bytes=source.stat().st_size * 2,
                        _rng=random.Random(2),
                    )
            self.assertEqual(tuple(output.iterdir()), ())

    def test_simulation_mutations_have_input_size_independent_python_heap(self):
        runner = self.runner
        input_size = 8 * 1024 * 1024
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "input"
            output = root / "output"
            source.write_bytes(b"m" * input_size)

            tracemalloc.start()
            try:
                with mock.patch.object(
                        runner, "_RESULT_STREAM_CHUNK_BYTES", 64 * 1024):
                    paths = runner._simulate_mutations(
                        str(source),
                        str(output),
                        5,
                        max_objects=5,
                        max_bytes=input_size * 5,
                        _rng=random.Random(3),
                    )
                _current, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()

            self.assertEqual(len(paths), 5)
            self.assertTrue(all(Path(path).stat().st_size == input_size
                                for path in paths))
            self.assertLess(peak, 2 * 1024 * 1024)

    def test_hybrid_worker_result_scan_is_preflight_bounded(self):
        helper = importlib.import_module("mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "case").write_bytes(b"abc")
            (root / "case.hints").write_text("0:00:01\n", encoding="ascii")
            (root / ".solver-control").write_bytes(b"ignored")
            (root / "alias").symlink_to(root / "case")

            candidates, hints = helper._scan_worker_output_candidates(
                str(root), max_objects=1, max_bytes=64, max_object_bytes=16)
            self.assertEqual([candidate.name for candidate in candidates], ["case"])
            self.assertEqual([candidate.name for candidate in hints], ["case.hints"])

            (root / "second").write_bytes(b"d")
            with self.assertRaises(
                    helper._WorkerResultBudgetExceeded) as objects:
                helper._scan_worker_output_candidates(
                    str(root), max_objects=1, max_bytes=64,
                    max_object_bytes=16)
            self.assertEqual(objects.exception.payload(), {
                "resource": "objects", "observed": 2, "limit": 1,
                "objects": 2,
            })
            (root / "second").unlink()

            (root / "second.hints").write_text("1:01:02\n", encoding="ascii")
            with self.assertRaises(
                    helper._WorkerResultBudgetExceeded) as hint_files:
                helper._scan_worker_output_candidates(
                    str(root), max_objects=1, max_bytes=64,
                    max_object_bytes=16)
            self.assertEqual(hint_files.exception.resource, "hint_files")
            (root / "second.hints").unlink()

            for invalid in (0, -1, True, 1.5):
                with self.subTest(invalid=invalid):
                    with self.assertRaises(ValueError):
                        helper._scan_worker_output_candidates(
                            str(root), max_objects=invalid, max_bytes=64,
                            max_object_bytes=16)

    def test_hybrid_worker_result_snapshot_rejects_path_replacement(self):
        helper = importlib.import_module("mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "output"
            output.mkdir()
            source = output / "case"
            source.write_bytes(b"stable-worker-output")
            candidates, _ = helper._scan_worker_output_candidates(
                str(output), max_objects=2, max_bytes=64,
                max_object_bytes=64)
            self.assertEqual(
                helper._read_worker_output_snapshot(
                    candidates[0], max_bytes=64),
                b"stable-worker-output",
            )

            replacement = root / "replacement"
            replacement.write_bytes(b"replacement-content")
            os.replace(replacement, source)
            self.assertIsNone(helper._read_worker_output_snapshot(
                candidates[0], max_bytes=64))

            candidates, _ = helper._scan_worker_output_candidates(
                str(output), max_objects=2, max_bytes=64,
                max_object_bytes=64)
            source.unlink()
            source.symlink_to(root / "external")
            (root / "external").write_bytes(b"replacement-content")
            self.assertIsNone(helper._read_worker_output_snapshot(
                candidates[0], max_bytes=64))

    def test_hybrid_worker_result_admission_rejects_whole_parent(self):
        helper = importlib.import_module("mpi_fuzzing_helper")

        def invoke(writer, *, environment_overrides=None, **limits):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                input_file = root / "input"
                output = root / "output"
                input_file.write_bytes(b"seed")
                engine = mock.Mock()
                environment = {
                    "SYMCC_STRING_SOLVER_ENABLE": "0",
                    "SYMCC_MAX_TRANSPORT_INPUT": "64",
                }
                environment.update(environment_overrides or {})
                engine.wrap_run.return_value = (
                    ["synthetic-target"], environment, False)

                def execute(*_args, **_kwargs):
                    writer(output)
                    return mock.Mock(returncode=0)

                with mock.patch.object(
                        helper, "get_engine", return_value=engine), \
                        mock.patch.object(
                            helper.subprocess, "run", side_effect=execute):
                    return helper.run_symcc_worker(
                        ["synthetic-target"], str(input_file), str(output),
                        1, False, base_env=environment, **limits)

        def write_valid(output):
            output.mkdir(exist_ok=True)
            (output / "case-a").write_bytes(b"first")
            (output / "case-a.hints").write_text(
                "0:00:01\n", encoding="ascii")
            (output / "case-b").write_bytes(b"second")

        accepted = invoke(
            write_valid, result_max_objects=2, result_max_bytes=64,
            result_max_hints=2)
        self.assertEqual(accepted[1:3], (2, 0))
        self.assertEqual(
            sorted(item["content"] for item in accepted[0]),
            [b"first", b"second"],
        )
        hinted = next(item for item in accepted[0]
                      if item["content"] == b"first")
        self.assertEqual(hinted["hints"], [(0, 0, 1)])

        with self.assertRaises(
                helper._WorkerResultBudgetExceeded) as objects:
            invoke(
                write_valid, result_max_objects=1, result_max_bytes=64,
                result_max_hints=2)
        self.assertEqual(objects.exception.resource, "objects")
        self.assertEqual(objects.exception.retcode, 0)
        self.assertEqual(objects.exception.objects, 2)

        def write_bytes_overflow(output):
            output.mkdir(exist_ok=True)
            (output / "case-a").write_bytes(b"abc")
            (output / "case-b").write_bytes(b"def")

        with mock.patch.object(
                helper, "_read_worker_output_snapshot") as read_snapshot:
            with self.assertRaises(
                    helper._WorkerResultBudgetExceeded) as total_bytes:
                invoke(
                    write_bytes_overflow, result_max_objects=2,
                    result_max_bytes=5, result_max_hints=2)
        self.assertEqual(total_bytes.exception.payload(), {
            "resource": "bytes", "observed": 6, "limit": 5,
            "objects": 2,
        })
        read_snapshot.assert_not_called()

        def write_hint_overflow(output):
            output.mkdir(exist_ok=True)
            (output / "case").write_bytes(b"data")
            (output / "case.hints").write_text(
                "0:00:01\n1:01:02\n", encoding="ascii")

        with self.assertRaises(
                helper._WorkerResultBudgetExceeded) as hints:
            invoke(
                write_hint_overflow, result_max_objects=1,
                result_max_bytes=64, result_max_hints=1)
        self.assertEqual(hints.exception.payload(), {
            "resource": "hints", "observed": 2, "limit": 1,
            "objects": 1,
        })

        class FailingShowmap:
            _afl_showmap = None
            _target_cmd = []

            @staticmethod
            def get_result(_content):
                raise OSError("injected showmap failure")

        def write_retryable(output):
            output.mkdir(exist_ok=True)
            (output / "case").write_bytes(b"retry-after-showmap-error")

        worker_seen = set()
        failed = invoke(
            write_retryable,
            streaming_showmap=FailingShowmap(),
            worker_coverage=helper.CoverageBitmap(),
            worker_seen=worker_seen,
            result_max_objects=1,
            result_max_bytes=64,
            result_max_hints=1,
        )
        self.assertEqual(failed[0], [])
        self.assertEqual(worker_seen, set())

        def write_budget_retry(output):
            output.mkdir(exist_ok=True)
            (output / "case-b").write_bytes(b"budget-candidate-b")
            (output / "case-a").write_bytes(b"budget-candidate-a")

        budget_seen = set()
        with mock.patch.object(
                helper.time, "monotonic",
                side_effect=[0.0, 0.0, 0.0, 3.0, 3.0]):
            budget_limited = invoke(
                write_budget_retry,
                environment_overrides={
                    "SYMCC_WORKER_POSTPROCESS_BUDGET_SEC": "2.0",
                },
                worker_seen=budget_seen,
                result_max_objects=2,
                result_max_bytes=64,
                result_max_hints=2,
            )
        self.assertEqual(budget_limited[0], [])
        self.assertEqual(budget_seen, set())

        retried = invoke(
            write_budget_retry,
            environment_overrides={
                "SYMCC_WORKER_POSTPROCESS_BUDGET_SEC": "0",
            },
            worker_seen=budget_seen,
            result_max_objects=2,
            result_max_bytes=64,
            result_max_hints=2,
        )
        self.assertEqual(
            [item["content"] for item in retried[0]],
            [b"budget-candidate-a", b"budget-candidate-b"],
        )
        self.assertEqual(len(budget_seen), 2)

        class TerminalShowmap:
            _afl_showmap = None
            _target_cmd = []

            @staticmethod
            def get_result(_content):
                return type("Result", (), {
                    "status": "crash",
                    "status_detail": 11,
                    "edges": (),
                })()

        terminal_seen = set()
        terminal = invoke(
            write_retryable,
            streaming_showmap=TerminalShowmap(),
            worker_coverage=helper.CoverageBitmap(),
            worker_seen=terminal_seen,
            result_max_objects=1,
            result_max_bytes=64,
            result_max_hints=1,
        )
        self.assertEqual(terminal[0], [{
            "content": b"retry-after-showmap-error",
            "terminal_status": "crash",
            "terminal_detail": 11,
        }])
        repeated_terminal = invoke(
            write_retryable,
            streaming_showmap=TerminalShowmap(),
            worker_coverage=helper.CoverageBitmap(),
            worker_seen=terminal_seen,
            result_max_objects=1,
            result_max_bytes=64,
            result_max_hints=1,
        )
        self.assertEqual(repeated_terminal[0], [])

        class BatchTerminalShowmap(TerminalShowmap):
            _afl_showmap = "/fake/afl-showmap"
            _target_cmd = ["/fake/target"]

            @staticmethod
            def get_result(content):
                if content == b"terminal":
                    return type("Result", (), {
                        "status": "crash",
                        "status_detail": 11,
                        "edges": (),
                    })()
                return type("Result", (), {
                    "status": "ok",
                    "status_detail": 0,
                    "edges": ((7, 1),),
                })()

        def write_mixed(output):
            output.mkdir(exist_ok=True)
            (output / "case-normal").write_bytes(b"normal")
            (output / "case-terminal").write_bytes(b"terminal")

        def partial_batch(_showmap, _target, paths, _work):
            return {
                path: [(7, 1)]
                for path in paths
                if Path(path).read_bytes() == b"normal"
            }

        with mock.patch.object(
            helper, "batch_showmap_edges", side_effect=partial_batch
        ):
            mixed = invoke(
                write_mixed,
                streaming_showmap=BatchTerminalShowmap(),
                worker_coverage=helper.CoverageBitmap(),
                result_max_objects=2,
                result_max_bytes=64,
                result_max_hints=1,
            )
        self.assertEqual(
            sorted((item["content"], item.get("terminal_status")) for item in mixed[0]),
            [(b"normal", None), (b"terminal", "crash")],
        )

        class BatchMappedTerminalShowmap(TerminalShowmap):
            _afl_showmap = "/fake/afl-showmap"
            _target_cmd = ["/fake/target"]

            @staticmethod
            def get_result(content):
                if content == b"terminal":
                    return type("Result", (), {
                        "status": "crash",
                        "status_detail": 6,
                        "edges": (),
                    })()
                return type("Result", (), {
                    "status": "ok",
                    "status_detail": 0,
                    "edges": ((9, 1),),
                })()

        def full_batch(_showmap, _target, paths, _work):
            return {path: [(7, 1)] for path in paths}

        mapped_seen = set()
        with mock.patch.object(
            helper, "batch_showmap_edges", side_effect=full_batch
        ):
            mapped_terminal = invoke(
                write_mixed,
                environment_overrides={"SYMCC_BATCH_VERIFY_NEW": "0"},
                streaming_showmap=BatchMappedTerminalShowmap(),
                worker_coverage=helper.CoverageBitmap(),
                worker_seen=mapped_seen,
                result_max_objects=2,
                result_max_bytes=64,
                result_max_hints=1,
            )
        self.assertEqual(
            sorted(
                (item["content"], item.get("terminal_status"))
                for item in mapped_terminal[0]
            ),
            [(b"normal", None), (b"terminal", "crash")],
        )

    def test_hybrid_worker_uninteresting_outputs_use_one_object_heap(self):
        helper = importlib.import_module("mpi_fuzzing_helper")

        class RedundantShowmap:
            _afl_showmap = None
            _target_cmd = []

            @staticmethod
            def get_result(_content):
                return type("Result", (), {
                    "status": "ok",
                    "edges": ((0, 1),),
                })()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_file = root / "input"
            output = root / "output"
            input_file.write_bytes(b"seed")
            output.mkdir()
            object_size = 1024 * 1024
            for index in range(32):
                with open(output / f"case-{index:04d}", "wb") as stream:
                    stream.seek(object_size - 1)
                    stream.write(bytes((index,)))

            engine = mock.Mock()
            environment = {
                "SYMCC_STRING_SOLVER_ENABLE": "0",
                "SYMCC_MAX_TRANSPORT_INPUT": str(2 * object_size),
                "SYMCC_BATCH_SHOWMAP": "0",
            }
            engine.wrap_run.return_value = (
                ["synthetic-target"], environment, False)
            coverage = helper.CoverageBitmap()
            coverage.data = bytearray(b"\x01")

            tracemalloc.start()
            try:
                with mock.patch.object(
                        helper, "get_engine", return_value=engine), \
                        mock.patch.object(
                            helper.subprocess,
                            "run",
                            return_value=mock.Mock(returncode=0),
                        ):
                    result = helper.run_symcc_worker(
                        ["synthetic-target"], str(input_file), str(output),
                        1, False, base_env=environment,
                        streaming_showmap=RedundantShowmap(),
                        worker_coverage=coverage,
                        result_max_objects=32,
                        result_max_bytes=32 * object_size,
                        result_max_hints=1,
                    )
                _current, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()

            self.assertEqual(result[0], [])
            self.assertEqual(result[1], 32)
            self.assertLess(peak, 5 * 1024 * 1024)

    def test_hybrid_worker_transmits_only_incremental_coverage_rows(self):
        helper = importlib.import_module("mpi_fuzzing_helper")

        class ProgressiveShowmap:
            _afl_showmap = None
            _target_cmd = []

            @staticmethod
            def get_result(content):
                edges = (
                    ((1, 1), (2, 1))
                    if content == b"first"
                    else ((1, 1), (2, 3), (3, 1))
                )
                return type("Result", (), {
                    "status": "ok",
                    "edges": edges,
                })()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_file = root / "input"
            output = root / "output"
            input_file.write_bytes(b"seed")
            output.mkdir()
            (output / "case-a").write_bytes(b"first")
            (output / "case-b").write_bytes(b"second")
            engine = mock.Mock()
            environment = {
                "SYMCC_STRING_SOLVER_ENABLE": "0",
                "SYMCC_MAX_TRANSPORT_INPUT": "64",
                "SYMCC_BATCH_SHOWMAP": "0",
            }
            engine.wrap_run.return_value = (
                ["synthetic-target"], environment, False
            )
            with mock.patch.object(
                helper, "get_engine", return_value=engine
            ), mock.patch.object(
                helper.subprocess,
                "run",
                return_value=mock.Mock(returncode=0),
            ):
                result = helper.run_symcc_worker(
                    ["synthetic-target"],
                    str(input_file),
                    str(output),
                    1,
                    False,
                    base_env=environment,
                    streaming_showmap=ProgressiveShowmap(),
                    worker_coverage=helper.CoverageBitmap(),
                    result_max_objects=2,
                    result_max_bytes=64,
                    result_max_hints=1,
                )

            self.assertEqual(
                [candidate["bitmap"] for candidate in result[0]],
                [[(1, 1), (2, 1)], [(2, 2), (3, 1)]],
            )

    def test_hybrid_worker_coverage_budget_failure_rolls_back_local_map(self):
        helper = importlib.import_module("mpi_fuzzing_helper")

        class DistinctShowmap:
            _afl_showmap = None
            _target_cmd = []

            @staticmethod
            def get_result(content):
                edge = 1 if content == b"first" else 2
                return type("Result", (), {
                    "status": "ok",
                    "edges": ((edge, 1),),
                })()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_file = root / "input"
            output = root / "output"
            input_file.write_bytes(b"seed")
            output.mkdir()
            (output / "case-a").write_bytes(b"first")
            (output / "case-b").write_bytes(b"second")
            engine = mock.Mock()
            environment = {
                "SYMCC_STRING_SOLVER_ENABLE": "0",
                "SYMCC_MAX_TRANSPORT_INPUT": "64",
                "SYMCC_BATCH_SHOWMAP": "0",
            }
            engine.wrap_run.return_value = (
                ["synthetic-target"], environment, False
            )
            coverage = helper.CoverageBitmap()
            worker_seen = set()
            with mock.patch.object(
                helper, "get_engine", return_value=engine
            ), mock.patch.object(
                helper.subprocess,
                "run",
                return_value=mock.Mock(returncode=0),
            ), mock.patch.object(helper.StreamingShowmap, "MAX_EDGES", 1):
                with self.assertRaisesRegex(
                    helper._WorkerResultBudgetExceeded, "coverage_rows"
                ):
                    helper.run_symcc_worker(
                        ["synthetic-target"],
                        str(input_file),
                        str(output),
                        1,
                        False,
                        base_env=environment,
                        streaming_showmap=DistinctShowmap(),
                        worker_coverage=coverage,
                        worker_seen=worker_seen,
                        result_max_objects=2,
                        result_max_bytes=64,
                        result_max_hints=1,
                    )
            self.assertEqual(coverage.feature_count, 0)
            self.assertIsNone(coverage.data)
            self.assertEqual(worker_seen, set())

    def test_hybrid_result_objects_are_referenced_and_reverified(self):
        helper = importlib.import_module("mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            store = helper.ContentAddressedInputStore(
                str(Path(tmp) / "result-objects"), 2_000_000
            )
            candidates = [
                {"content": b"first" * 200_000, "bitmap": [(3, 1)]},
                {"content": b"second", "hints": [(0, 0, 1)]},
            ]
            legacy_wire_bytes = len(pickle.dumps(candidates))
            self.assertEqual(
                helper._stage_hybrid_result_objects(candidates, store), 2
            )
            self.assertTrue(all("content" not in item for item in candidates))
            self.assertLess(len(pickle.dumps(candidates)), legacy_wire_bytes // 100)
            result = {
                "new_tests": candidates,
                "total_generated": 2,
                "retcode": 0,
                "elapsed": 0.1,
                "killed": False,
            }
            limits = {
                "max_objects": 2,
                "max_bytes": 2_000_000,
                "max_object_bytes": 2_000_000,
                "max_hints": 2,
                "result_object_store": store,
            }
            self.assertIs(
                helper._validate_hybrid_worker_result(result, **limits),
                result,
            )
            self.assertEqual(
                [
                    helper._hybrid_candidate_content(
                        item, store, max_bytes=2_000_000
                    )
                    for item in candidates
                ],
                [b"first" * 200_000, b"second"],
            )

            object_id = candidates[0]["object_id"]
            Path(store.object_path(object_id)).write_bytes(b"wrong")
            with self.assertRaisesRegex(ValueError, "failed verification"):
                helper._validate_hybrid_worker_result(result, **limits)

    def test_hybrid_result_object_gc_preserves_pending_and_young_objects(self):
        helper = importlib.import_module("mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            store = helper.ContentAddressedInputStore(
                str(Path(tmp) / "result-objects"), 1024
            )
            old_id, old_path = store.put(b"old-result")
            protected_id, protected_path = store.put(b"protected-result")
            young_id, _young_path = store.put(b"young-result")
            old_ns = time.time_ns() - 120_000_000_000
            os.utime(old_path, ns=(old_ns, old_ns))
            os.utime(protected_path, ns=(old_ns, old_ns))

            service = helper._HybridResultAdmissionService(
                max_workers=1,
                capacity=1,
                max_objects=1,
                max_bytes=1024,
                max_object_bytes=1024,
                max_hints=1,
                max_timeout_sites=1,
                max_schedule_trace_bytes=1,
                result_object_store=store,
            )
            try:
                service.submit_many(
                    [
                        (
                            1,
                            "dispatch",
                            {
                                "new_tests": [
                                    {
                                        "object_id": protected_id,
                                        "object_size": len(b"protected-result"),
                                    }
                                ],
                                "total_generated": 1,
                                "retcode": 0,
                                "elapsed": 0.1,
                                "killed": False,
                            },
                        )
                    ]
                )
                stats = helper._collect_hybrid_result_objects(
                    store,
                    protected_object_ids=service.protected_object_ids(),
                    max_entries=100,
                    min_age_seconds=60.0,
                )
            finally:
                service.close()

            self.assertEqual(stats["deleted"], 1)
            self.assertEqual(stats["skipped_protected"], 1)
            self.assertEqual(stats["skipped_young"], 1)
            self.assertFalse(Path(store.object_path(old_id)).exists())
            self.assertTrue(Path(store.object_path(protected_id)).exists())
            self.assertTrue(Path(store.object_path(young_id)).exists())

    def test_hybrid_input_admission_refreshes_identity_and_fences_cache(self):
        helper = importlib.import_module("mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            afl = root / "afl"
            queue = afl / "queue"
            queue.mkdir(parents=True)
            (afl / "fuzzer_stats").write_text(
                "command_line : /bin/true -- /bin/true @@\n",
                encoding="ascii",
            )
            seed = queue / "id:000000,orig:seed"
            seed.write_bytes(b"first-stable-input")
            config = helper.AflConfig(str(afl), max_input_bytes=64)
            self.assertEqual(config.best_new_testcases(set()), [str(seed)])
            first_hash = config._file_cache[str(seed)]["hash"]
            master_store = helper.ContentAddressedInputStore(
                str(root / "master-objects"), 64)

            replacement = root / "replacement"
            replacement.write_bytes(b"second-stable-input")
            os.replace(replacement, seed)
            with self.assertRaisesRegex(
                    ValueError, "changed after queue scoring"):
                helper._admit_hybrid_master_input(
                    master_store,
                    config._file_cache,
                    str(seed),
                )
            self.assertNotIn(str(seed), config._file_cache)
            self.assertEqual(config.best_new_testcases(set()), [str(seed)])
            second_hash = config._file_cache[str(seed)]["hash"]
            self.assertNotEqual(first_hash, second_hash)
            self.assertEqual(
                second_hash,
                hashlib.sha256(b"second-stable-input").hexdigest(),
            )

            oversized = queue / "id:000001,orig:oversized"
            oversized.write_bytes(b"x" * 65)
            alias = queue / "id:000002,orig:alias"
            alias.symlink_to(seed)
            self.assertEqual(config.best_new_testcases(set()), [str(seed)])
            ranked = []
            for index in (3, 4, 5):
                marker = "+cov," if index == 3 else ""
                candidate = queue / f"id:{index:06d},{marker}orig:ranked"
                candidate.write_bytes(b"r" * len(b"second-stable-input"))
                ranked.append(candidate)
            with mock.patch.object(helper, "MAX_FILE_CACHE", 2):
                selected = config.best_new_testcases(set(), batch_size=2)
            self.assertEqual(selected, [str(ranked[0]), str(ranked[2])])
            self.assertLessEqual(len(config._file_cache), 2)
            self.assertEqual(set(config._file_cache), set(selected))
            selected_replacement = root / "selected-replacement"
            selected_replacement.write_bytes(b"changed-after-top-k-selection")
            os.replace(selected_replacement, ranked[0])
            with self.assertRaisesRegex(
                    ValueError, "changed after queue scoring"):
                helper._admit_hybrid_master_input(
                    master_store,
                    config._file_cache,
                    str(ranked[0]),
                )

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
            expected_checkpoint = (
                helper.LiveContinuationDescriptor.from_mapping(
                    continuation).checkpoint_id()
            )
            with mock.patch.object(
                    master_store, "import_path",
                    side_effect=AssertionError("continuation read seed path")):
                self.assertEqual(
                    helper._admit_hybrid_master_work(
                        master_store,
                        config._file_cache,
                        "/missing/self-contained-seed",
                        continuation,
                    ),
                    (None, expected_checkpoint, b"", 0),
                )

            object_id, _stored, content = master_store.import_path(str(seed))
            worker_store = helper.ContentAddressedInputStore(
                str(root / "worker-objects"), 64)
            local_input, materialized = helper._materialize_hybrid_worker_input(
                worker_store,
                {
                    "sha256": object_id,
                    "object_id": object_id,
                    "object_content": content,
                },
                str(seed),
            )
            self.assertEqual(materialized, object_id)
            self.assertEqual(Path(local_input).read_bytes(), content)

            Path(local_input).write_bytes(b"corrupt-worker-cache")
            with self.assertRaises(ValueError):
                helper._materialize_hybrid_worker_input(
                    worker_store,
                    {"sha256": object_id, "object_id": object_id},
                    str(seed),
                )
            repaired_input, _ = helper._materialize_hybrid_worker_input(
                worker_store,
                {
                    "sha256": object_id,
                    "object_id": object_id,
                    "object_content": content,
                },
                str(seed),
            )
            self.assertEqual(Path(repaired_input).read_bytes(), content)

            worker_objects = {1: set()}
            self.assertFalse(helper._acknowledge_worker_input_object(
                worker_objects, 1, object_id, ""))
            self.assertEqual(worker_objects[1], set())
            self.assertTrue(helper._acknowledge_worker_input_object(
                worker_objects, 1, object_id, object_id))
            self.assertEqual(worker_objects[1], {object_id})
            self.assertFalse(helper._acknowledge_worker_input_object(
                worker_objects, 1, object_id, "0" * 64))
            self.assertEqual(worker_objects[1], set())
            second_object_id = hashlib.sha256(b"second-object").hexdigest()
            with mock.patch.object(helper, "_WORKER_OBJECT_CACHE_CAP", 1):
                self.assertTrue(helper._acknowledge_worker_input_object(
                    worker_objects, 1, object_id, object_id))
                self.assertTrue(helper._acknowledge_worker_input_object(
                    worker_objects, 1, second_object_id, second_object_id))
            self.assertEqual(worker_objects[1], {second_object_id})

            path_replacement = root / "path-replacement"
            path_replacement.write_bytes(b"different-after-master-admission")
            os.replace(path_replacement, seed)
            with self.assertRaises(ValueError):
                helper._materialize_hybrid_worker_input(
                    worker_store,
                    {"sha256": object_id},
                    str(seed),
                )

    def test_queue_scan_limit_counts_unseen_work_not_seen_history(self):
        helper = importlib.import_module("mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            afl = root / "afl"
            queue = afl / "queue"
            queue.mkdir(parents=True)
            (afl / "fuzzer_stats").write_text(
                "command_line : /bin/true -- /bin/true @@\n",
                encoding="ascii",
            )
            history = []
            for index in range(4):
                path = queue / f"id:{index:06d},orig:history"
                path.write_bytes(bytes((index,)))
                history.append(str(path))
            tail = queue / "id:000004,+cov,orig:tail"
            tail.write_bytes(b"new-tail")

            config = helper.AflConfig(str(afl), max_input_bytes=64)
            real_entries = sorted(os.scandir(queue), key=lambda entry: entry.name)
            with mock.patch.dict(
                    os.environ,
                    {"SYMCC_QUEUE_SCAN_MAX": "1",
                     "SYMCC_QUEUE_SCAN_BUDGET_SEC": "0"}), \
                    mock.patch.object(
                        helper.os, "scandir", return_value=real_entries):
                selected = config.best_new_testcases(set(history), batch_size=1)

            self.assertEqual(selected, [str(tail)])

    def test_queue_scan_time_budget_excludes_seen_history(self):
        helper = importlib.import_module("mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            afl = root / "afl"
            queue = afl / "queue"
            queue.mkdir(parents=True)
            (afl / "fuzzer_stats").write_text(
                "command_line : /bin/true -- /bin/true @@\n",
                encoding="ascii",
            )
            history = []
            for index in range(32):
                path = queue / f"id:{index:06d},orig:history"
                path.write_bytes(bytes((index,)))
                history.append(str(path))
            tail = queue / "id:000032,+cov,orig:tail"
            tail.write_bytes(b"new-tail")

            config = helper.AflConfig(str(afl), max_input_bytes=64)
            real_entries = sorted(os.scandir(queue), key=lambda entry: entry.name)
            clock = iter((100.0, 100.25))
            with mock.patch.dict(
                    os.environ,
                    {"SYMCC_QUEUE_SCAN_MAX": "1",
                     "SYMCC_QUEUE_SCAN_BUDGET_SEC": "0.1"}), \
                    mock.patch.object(
                        helper.os, "scandir", return_value=real_entries), \
                    mock.patch.object(
                        helper.time, "monotonic", side_effect=lambda: next(clock)):
                selected = config.best_new_testcases(set(history), batch_size=1)

            self.assertEqual(selected, [str(tail)])

    def test_afl_coverage_bridge_builds_union_and_only_replays_new_entries(self):
        helper = importlib.import_module("mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            afl = root / "afl"
            queue = afl / "queue"
            queue.mkdir(parents=True)
            (afl / "fuzzer_stats").write_text(
                "command_line : /bin/true -- /bin/true\n",
                encoding="ascii",
            )
            first = queue / "id:000000,orig:first"
            second = queue / "id:000001,orig:second"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            config = helper.AflConfig(str(afl), max_input_bytes=64)
            coverage = helper.CoverageBitmap()
            bridge = helper.AflCoverageBridge(
                config, coverage, str(afl / ".triage_bitmap")
            )

            with mock.patch.object(
                    helper,
                    "corpus_showmap_edges",
                    side_effect=([(7, 1)], [(7, 1), (11, 3)]),
            ) as showmap:
                initial = bridge.ingest_queue()
                repeated = bridge.ingest_queue()
                third = queue / "id:000002,orig:third"
                third.write_bytes(b"third")
                incremental = bridge.ingest_queue()
                bridge._queue_audit_interval = 2
                audited_skip = bridge.ingest_queue()
                audited = bridge.ingest_queue()

            self.assertTrue(initial.complete)
            self.assertEqual(initial.examined, 2)
            self.assertEqual(initial.ingested, 2)
            self.assertEqual(initial.globally_new_features, 1)
            self.assertEqual(repeated.examined, 0)
            self.assertEqual(incremental.ingested, 1)
            self.assertEqual(audited_skip.examined, 0)
            self.assertEqual(audited.examined, 0)
            self.assertEqual(incremental.globally_new_features, 2)
            self.assertEqual(coverage.feature_count, 3)
            self.assertEqual(showmap.call_count, 2)
            self.assertTrue(showmap.call_args.kwargs["require_all"])
            self.assertEqual(bridge.snapshot()["queue_scans"], 3)
            self.assertEqual(bridge.snapshot()["queue_scan_skips"], 2)

    def test_coverage_bitmap_zero_extends_across_native_map_sizes(self):
        helper = importlib.import_module("mpi_fuzzing_helper")
        coverage = helper.CoverageBitmap()

        self.assertEqual(coverage.merge_delta([(1, 1)]), 1)
        larger = bytearray(len(coverage.data) + 8)
        larger[1] = 1
        larger[-1] = 3
        self.assertEqual(coverage.count_delta(bytes(larger)), 2)
        self.assertEqual(coverage.merge_delta(bytes(larger)), 2)
        self.assertEqual(len(coverage.data), len(larger))

        # A shorter bitmap is the same index domain with an implicit zero
        # suffix; it must not erase the already observed high indices.
        shorter = bytes((1, 0))
        self.assertEqual(coverage.count_delta(shorter), 1)
        self.assertEqual(coverage.merge_delta(shorter), 1)
        self.assertEqual(coverage.data[-1], 3)
        self.assertEqual(coverage.feature_count, 4)

    def test_coverage_bitmap_counts_duplicate_sparse_rows_once(self):
        helper = importlib.import_module("mpi_fuzzing_helper")
        coverage = helper.CoverageBitmap()

        rows = [(7, 1), (7, 3), (7, 2), (9, 4), (9, 4)]
        self.assertEqual(coverage.count_delta(rows), 3)
        self.assertEqual(coverage.merge_delta(rows), 3)
        self.assertEqual(coverage.count_delta(rows), 0)

    def test_coverage_bitmap_ignores_zero_hit_sparse_rows(self):
        helper = importlib.import_module("mpi_fuzzing_helper")
        coverage = helper.CoverageBitmap()

        self.assertEqual(coverage.merge_delta([(7, 0), (9, 0)]), 0)
        self.assertEqual(coverage.feature_count, 0)
        self.assertEqual(coverage.edges, set())

    def test_coverage_bitmap_dense_merge_preserves_chunk_boundaries(self):
        helper = importlib.import_module("mpi_fuzzing_helper")
        coverage = helper.CoverageBitmap()
        chunk = helper._COVERAGE_BITMAP_CHUNK_BYTES
        first = bytearray(chunk * 2 + 9)
        first[chunk - 1] = 1
        first[chunk] = 2
        first[-1] = 4

        self.assertEqual(coverage.merge_delta(bytes(first)), 3)
        second = bytearray(first)
        second[chunk - 1] |= 8
        second[chunk] |= 4
        second[-1] |= 1
        self.assertEqual(coverage.count_delta(bytes(second)), 3)
        self.assertEqual(coverage.merge_delta(bytes(second)), 3)
        self.assertEqual(coverage.count_delta(bytes(second)), 0)
        self.assertEqual(coverage.feature_count, 6)

    def test_afl_coverage_bridge_retries_failed_entries(self):
        helper = importlib.import_module("mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            afl = root / "afl"
            queue = afl / "queue"
            queue.mkdir(parents=True)
            (afl / "fuzzer_stats").write_text(
                "command_line : /bin/true -- /bin/true\n",
                encoding="ascii",
            )
            seed = queue / "id:000000,orig:seed"
            seed.write_bytes(b"seed")
            config = helper.AflConfig(str(afl), max_input_bytes=64)
            coverage = helper.CoverageBitmap()
            bridge = helper.AflCoverageBridge(
                config, coverage, str(afl / ".triage_bitmap")
            )

            with mock.patch.object(
                    helper,
                    "corpus_showmap_edges",
                    side_effect=(None, None, [(19, 1)])), \
                    mock.patch.object(
                        config,
                        "run_showmap",
                        side_effect=(("error", None), ("success", [(19, 1)]))):
                failed = bridge.ingest_queue()
                recovered = bridge.retry_failed()

            self.assertFalse(failed.complete)
            self.assertEqual(failed.failed, 1)
            self.assertTrue(recovered.complete)
            self.assertEqual(recovered.ingested, 1)
            self.assertEqual(bridge.snapshot()["retry_entries"], 0)

    def test_afl_coverage_bridge_schedules_retry_before_queue_audit(self):
        helper = importlib.import_module("mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            afl = root / "afl"
            queue = afl / "queue"
            queue.mkdir(parents=True)
            (afl / "fuzzer_stats").write_text(
                "command_line : /bin/true -- /bin/true\n",
                encoding="ascii",
            )
            seed = queue / "id:000000,orig:seed"
            seed.write_bytes(b"seed")
            config = helper.AflConfig(str(afl), max_input_bytes=64)
            with mock.patch.dict(
                os.environ,
                {"SYMCC_AFL_COVERAGE_RETRY_BASE_SEC": "0"},
            ):
                bridge = helper.AflCoverageBridge(
                    config,
                    helper.CoverageBitmap(),
                    str(afl / ".triage_bitmap"),
                )

            with mock.patch.object(
                config,
                "run_showmap",
                side_effect=(("error", None), ("success", [(21, 1)])),
            ):
                self.assertEqual(bridge.schedule_queue(), 1)
                deadline = time.monotonic() + 2.0
                failed = helper.AflCoverageIngestResult()
                while failed.examined == 0 and time.monotonic() < deadline:
                    time.sleep(0.01)
                    failed = bridge.poll()
                self.assertEqual(failed.failed, 1)

                bridge._queue_audit_interval = 1000
                self.assertEqual(bridge.schedule_queue(), 0)
                self.assertEqual(bridge.schedule_failed(), 1)
                recovered = helper.AflCoverageIngestResult()
                while recovered.examined == 0 and time.monotonic() < deadline:
                    time.sleep(0.01)
                    recovered = bridge.poll()

            self.assertEqual(recovered.ingested, 1)
            self.assertEqual(bridge.snapshot()["retry_entries"], 0)
            bridge.close()

    def test_afl_coverage_retry_ledger_is_lossless_and_persistent(self):
        helper = importlib.import_module("mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            afl = root / "afl"
            queue = afl / "queue"
            queue.mkdir(parents=True)
            (afl / "fuzzer_stats").write_text(
                "command_line : /bin/true -- /bin/true\n",
                encoding="ascii",
            )
            paths = []
            for index in range(3):
                path = root / f"symcc-{index}"
                path.write_bytes(bytes((index,)))
                paths.append(str(path))
            config = helper.AflConfig(str(afl), max_input_bytes=64)
            bitmap = str(afl / ".triage_bitmap")
            environment = {
                "SYMCC_AFL_COVERAGE_RETRIES": "1",
                "SYMCC_AFL_COVERAGE_RETRY_BASE_SEC": "0",
            }
            with mock.patch.dict(os.environ, environment), mock.patch.object(
                helper, "corpus_showmap_edges", return_value=None
            ), mock.patch.object(
                config, "run_showmap", return_value=("error", None)
            ):
                bridge = helper.AflCoverageBridge(
                    config, helper.CoverageBitmap(), bitmap
                )
                failed = bridge.ingest(paths)
                self.assertEqual(failed.failed, 3)
                # RETRIES bounds each scheduling batch, never durable state.
                self.assertEqual(bridge.snapshot()["retry_entries"], 3)
                bridge.close()

            with mock.patch.dict(os.environ, environment), mock.patch.object(
                helper, "corpus_showmap_edges", return_value=None
            ), mock.patch.object(
                config,
                "run_showmap",
                side_effect=(("success", [(41, 1)]),) * 3,
            ):
                resumed = helper.AflCoverageBridge(
                    config, helper.CoverageBitmap(), bitmap
                )
                self.assertEqual(resumed.snapshot()["retry_entries"], 3)
                for _ in paths:
                    self.assertEqual(resumed.retry_failed().ingested, 1)
                self.assertEqual(resumed.snapshot()["retry_entries"], 0)
                resumed.close()

    def test_afl_coverage_missing_path_enters_durable_retry_ledger(self):
        helper = importlib.import_module("mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            afl = root / "afl"
            queue = afl / "queue"
            queue.mkdir(parents=True)
            (afl / "fuzzer_stats").write_text(
                "command_line : /bin/true -- /bin/true\n",
                encoding="ascii",
            )
            delayed = root / "delayed"
            config = helper.AflConfig(str(afl), max_input_bytes=64)
            with mock.patch.dict(
                os.environ,
                {"SYMCC_AFL_COVERAGE_RETRY_BASE_SEC": "0"},
            ):
                bridge = helper.AflCoverageBridge(
                    config,
                    helper.CoverageBitmap(),
                    str(afl / ".triage_bitmap"),
                )

            missing = bridge.ingest((str(delayed),))
            self.assertEqual(missing.examined, 1)
            self.assertEqual(missing.failed, 1)
            self.assertEqual(bridge.snapshot()["retry_entries"], 1)

            delayed.write_bytes(b"now-visible")
            with mock.patch.object(
                helper, "corpus_showmap_edges", return_value=None
            ), mock.patch.object(
                config, "run_showmap", return_value=("success", [(47, 1)])
            ):
                recovered = bridge.retry_failed()
            self.assertEqual(recovered.ingested, 1)
            self.assertEqual(bridge.snapshot()["retry_entries"], 0)
            bridge.close()

    def test_afl_coverage_retry_backlog_fills_all_available_slots(self):
        helper = importlib.import_module("mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            afl = root / "afl"
            queue = afl / "queue"
            queue.mkdir(parents=True)
            (afl / "fuzzer_stats").write_text(
                "command_line : /bin/true -- /bin/true\n",
                encoding="ascii",
            )
            retry_paths = []
            for index in range(8):
                path = root / f"retry-{index}"
                path.write_bytes(bytes((index,)))
                retry_paths.append(str(path))
            config = helper.AflConfig(str(afl), max_input_bytes=64)
            environment = {
                "SYMCC_AFL_COVERAGE_PENDING": "8",
                "SYMCC_AFL_COVERAGE_JOBS": "4",
                "SYMCC_AFL_COVERAGE_RETRY_BASE_SEC": "0",
            }
            with mock.patch.dict(os.environ, environment):
                bridge = helper.AflCoverageBridge(
                    config,
                    helper.CoverageBitmap(),
                    str(afl / ".triage_bitmap"),
                )
            release = threading.Event()

            def blocked_showmap(_path):
                release.wait(timeout=2.0)
                return "error", None

            try:
                for path in retry_paths:
                    bridge._note_retry(path)
                bridge._commit_index()
                with mock.patch.object(
                    bridge, "_run_showmap_async", side_effect=blocked_showmap
                ):
                    self.assertEqual(bridge.schedule_fair(), 8)
                    self.assertEqual(len(bridge._inflight), 8)
            finally:
                release.set()
                bridge.close()

    def test_afl_coverage_fair_scheduler_prioritizes_new_input_then_retry(self):
        helper = importlib.import_module("mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            afl = root / "afl"
            queue = afl / "queue"
            queue.mkdir(parents=True)
            (afl / "fuzzer_stats").write_text(
                "command_line : /bin/true -- /bin/true\n",
                encoding="ascii",
            )
            healthy = queue / "id:000000,orig:healthy"
            healthy.write_bytes(b"healthy")
            poison = root / "symcc-poison"
            poison.write_bytes(b"poison")
            config = helper.AflConfig(str(afl), max_input_bytes=64)
            environment = {
                "SYMCC_AFL_COVERAGE_PENDING": "1",
                "SYMCC_AFL_COVERAGE_JOBS": "1",
                "SYMCC_AFL_COVERAGE_RETRY_BASE_SEC": "0",
            }
            with mock.patch.dict(os.environ, environment):
                bridge = helper.AflCoverageBridge(
                    config,
                    helper.CoverageBitmap(),
                    str(afl / ".triage_bitmap"),
                )
            with mock.patch.object(
                helper, "corpus_showmap_edges", return_value=None
            ), mock.patch.object(
                config,
                "run_showmap",
                side_effect=(
                    ("error", None),
                    ("success", [(43, 1)]),
                    ("error", None),
                ),
            ):
                self.assertEqual(bridge.ingest((str(poison),)).failed, 1)
                self.assertEqual(bridge.schedule_fair(), 1)
                deadline = time.monotonic() + 2.0
                observed = helper.AflCoverageIngestResult()
                while observed.examined == 0 and time.monotonic() < deadline:
                    time.sleep(0.01)
                    observed = bridge.poll()
                self.assertEqual(observed.ingested, 1)
                self.assertTrue(bridge.is_synchronized(healthy))
                # A continuously growing AFL queue must not starve a durable
                # retry when only one showmap slot exists.
                (queue / "id:000001,orig:new").write_bytes(b"new")
                self.assertEqual(bridge.schedule_fair(), 1)
                self.assertIn(str(poison), bridge._inflight)
                bridge.close()

    def test_afl_coverage_bridge_runs_showmap_off_master_and_persists_index(self):
        helper = importlib.import_module("mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            afl = root / "afl"
            queue = afl / "queue"
            queue.mkdir(parents=True)
            (afl / "fuzzer_stats").write_text(
                "command_line : /bin/true -- /bin/true\n",
                encoding="ascii",
            )
            seed = queue / "id:000000,orig:seed"
            seed.write_bytes(b"seed")
            config = helper.AflConfig(str(afl), max_input_bytes=64)
            coverage = helper.CoverageBitmap()
            bitmap_path = str(afl / ".triage_bitmap")
            bridge = helper.AflCoverageBridge(
                config, coverage, bitmap_path
            )
            started = threading.Event()
            release = threading.Event()

            def delayed_showmap(_path, _bitmap):
                started.set()
                self.assertTrue(release.wait(timeout=2.0))
                return "success", [(23, 1)]

            with mock.patch.object(
                config, "run_showmap", side_effect=delayed_showmap
            ):
                self.assertEqual(bridge.schedule([str(seed)]), 1)
                self.assertTrue(started.wait(timeout=1.0))
                self.assertEqual(bridge.poll().examined, 0)
                self.assertEqual(bridge.snapshot()["async_pending"], 1)
                release.set()
                deadline = time.monotonic() + 2.0
                result = helper.AflCoverageIngestResult()
                while result.examined == 0 and time.monotonic() < deadline:
                    time.sleep(0.01)
                    result = bridge.poll()

            self.assertEqual(result.ingested, 1)
            self.assertEqual(coverage.feature_count, 1)
            self.assertFalse(hasattr(bridge, "_ingested"))

            own_queue = root / "symcc" / "queue"
            own_queue.mkdir(parents=True)
            own_seed = own_queue / "id:000000,src:000000"
            own_seed.write_bytes(b"symbolic")
            with mock.patch.object(
                helper, "corpus_showmap_edges", return_value=[(29, 2)]
            ):
                own_result = bridge.ingest([str(own_seed)])
            self.assertEqual(own_result.ingested, 1)
            self.assertEqual(coverage.feature_count, 2)
            bridge.close()

            restored_coverage = helper.CoverageBitmap()
            restored = helper.AflCoverageBridge(
                config, restored_coverage, bitmap_path
            )
            with mock.patch.object(config, "run_showmap") as showmap:
                replay = restored.ingest_queue()
                own_replay = restored.ingest([str(own_seed)])
            self.assertEqual(replay.examined, 0)
            self.assertEqual(own_replay.examined, 0)
            showmap.assert_not_called()
            self.assertEqual(restored.snapshot()["tracked_entries"], 2)
            self.assertEqual(restored_coverage.feature_count, 2)
            self.assertEqual(restored_coverage.data[23], 1)
            self.assertEqual(restored_coverage.data[29], 2)
            self.assertEqual(restored.snapshot()["restored_baseline_features"], 2)
            restored.close()

            # A legacy identity-only index has no bitmap evidence.  It must be
            # invalidated and replayed instead of suppressing this queue entry.
            index = helper.sqlite3.connect(
                bitmap_path + ".afl-coverage-index.sqlite3"
            )
            index.execute("DELETE FROM coverage_baseline")
            index.commit()
            index.close()
            legacy = helper.AflCoverageBridge(
                config, helper.CoverageBitmap(), bitmap_path
            )
            with mock.patch.object(
                helper, "corpus_showmap_edges", return_value=[(23, 1)]
            ) as showmap:
                replay = legacy.ingest_queue()
            self.assertEqual(replay.examined, 1)
            self.assertEqual(replay.ingested, 1)
            self.assertEqual(legacy.snapshot()["invalidated_indexes"], 1)
            showmap.assert_called_once()
            legacy.close()

    def test_afl_coverage_capacity_forces_immediate_queue_rescan(self):
        helper = importlib.import_module("mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            afl = root / "afl"
            queue = afl / "queue"
            queue.mkdir(parents=True)
            (afl / "fuzzer_stats").write_text(
                "command_line : /bin/true -- /bin/true\n",
                encoding="ascii",
            )
            first = queue / "id:000000,orig:first"
            second = queue / "id:000001,orig:second"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            config = helper.AflConfig(str(afl), max_input_bytes=64)
            with mock.patch.dict(os.environ, {
                "SYMCC_AFL_COVERAGE_PENDING": "1",
                "SYMCC_AFL_COVERAGE_JOBS": "1",
            }):
                bridge = helper.AflCoverageBridge(
                    config,
                    helper.CoverageBitmap(),
                    str(afl / ".triage_bitmap"),
                )
            with mock.patch.object(
                config,
                "run_showmap",
                side_effect=(("success", [(31, 1)]),
                             ("success", [(37, 1)])),
            ):
                self.assertEqual(bridge.schedule_queue(), 1)
                deadline = time.monotonic() + 2.0
                while (
                    bridge.poll().examined == 0
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
                # Directory metadata is unchanged.  A stale generation cache
                # would return zero here until the 1000-poll audit interval.
                bridge._queue_audit_interval = 1000
                self.assertEqual(bridge.schedule_queue(), 1)
                bridge.close()
            self.assertEqual(bridge.snapshot()["tracked_entries"], 2)

    def test_tracking_container_trim_preserves_low_watermark(self):
        helper = importlib.import_module("mpi_fuzzing_helper")
        mapping = {index: index for index in range(12)}
        values = set(range(12))

        self.assertEqual(
            helper._trim_tracking_container(mapping, 10, retain_ratio=0.8), 4
        )
        self.assertEqual(list(mapping), list(range(4, 12)))
        self.assertEqual(
            helper._trim_tracking_container(values, 10, retain_ratio=0.8), 4
        )
        self.assertEqual(len(values), 8)

    def test_shared_coverage_snapshot_is_atomic_bounded_and_monotonic(self):
        helper = importlib.import_module("mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "coverage.snapshot")
            first = bytes((1, 0, 0, 0))
            second = bytes((1, 2, 0, 0))
            coverage = helper.CoverageBitmap()
            version = [-1]

            self.assertTrue(helper._publish_coverage_snapshot(path, 1, first))
            self.assertEqual(helper._read_coverage_snapshot(path), (1, first))
            self.assertTrue(
                helper._refresh_worker_coverage_snapshot(
                    path, coverage, version
                )
            )
            self.assertEqual(version, [1])
            self.assertEqual(bytes(coverage.data), first)

            reads = []
            real_open = open

            class ObservedReader:
                def __init__(self, stream):
                    self.stream = stream

                def __enter__(self):
                    self.stream.__enter__()
                    return self

                def __exit__(self, *args):
                    return self.stream.__exit__(*args)

                def read(self, size=-1):
                    reads.append(size)
                    return self.stream.read(size)

            def observed_open(*args, **kwargs):
                return ObservedReader(real_open(*args, **kwargs))

            with mock.patch("builtins.open", side_effect=observed_open):
                self.assertEqual(
                    helper._read_coverage_snapshot(path, newer_than=1),
                    (1, None),
                )
            self.assertEqual(reads, [24])

            # A stale writer cannot roll the worker bitmap back or add bits.
            self.assertTrue(helper._publish_coverage_snapshot(path, 0, second))
            self.assertFalse(
                helper._refresh_worker_coverage_snapshot(
                    path, coverage, version
                )
            )
            self.assertEqual(bytes(coverage.data), first)

            self.assertTrue(helper._publish_coverage_snapshot(path, 2, second))
            self.assertTrue(
                helper._refresh_worker_coverage_snapshot(
                    path, coverage, version
                )
            )
            self.assertEqual(version, [2])
            self.assertEqual(bytes(coverage.data), second)

            Path(path).write_bytes(b"invalid")
            self.assertIsNone(helper._read_coverage_snapshot(path))

            self.assertFalse(helper._publish_coverage_snapshot(path, True, first))
            self.assertFalse(helper._publish_coverage_snapshot(path, -1, first))
            self.assertFalse(
                helper._publish_coverage_snapshot(path, 1 << 64, first)
            )
            self.assertFalse(
                helper._publish_coverage_snapshot(path, 3, bytearray(first))
            )

    def test_input_publish_rejects_replacement_and_cleans_temporary_state(self):
        runner = self.runner
        original = b"stable-before-owner-selection"
        replacement = b"replacement-after-owner-selection"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "seed"
            source.write_bytes(original)
            shared = root / "shared"
            shared.mkdir()
            snapshot = runner._input_file_snapshot(
                str(source), max_bytes=1024)
            self.assertIsNotNone(snapshot)

            replacement_path = root / "replacement"
            replacement_path.write_bytes(replacement)
            os.replace(replacement_path, source)
            with self.assertRaises(runner._SourceSnapshotChanged):
                runner._stream_publish_input_file(
                    str(source),
                    str(shared),
                    expected_hash=snapshot[0],
                    expected_identity=snapshot[2],
                    max_bytes=1024,
                )
            self.assertEqual(tuple(shared.iterdir()), ())

    def test_worker_input_copy_rejects_path_replacement_during_stream(self):
        runner = self.runner
        content = b"descriptor-remains-old"
        work_hash = hashlib.sha256(content).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / work_hash
            source.write_bytes(content)
            replacement = root / "replacement"
            replacement.write_bytes(b"new-path-object")
            destination = root / "private" / "input"
            destination.parent.mkdir()
            original_read = os.read
            replaced = False

            def replace_after_read(descriptor, size):
                nonlocal replaced
                chunk = original_read(descriptor, size)
                if chunk and not replaced:
                    replaced = True
                    os.replace(replacement, source)
                return chunk

            with mock.patch.object(
                    runner.os, "read", side_effect=replace_after_read):
                with self.assertRaises(runner._SourceSnapshotChanged):
                    runner._stream_copy_verified_input(
                        str(source),
                        str(destination),
                        expected_hash=work_hash,
                        max_bytes=1024,
                    )
            self.assertFalse(destination.exists())
            self.assertEqual(
                tuple(destination.parent.glob("input.tmp.*")), ())

    def test_public_corpus_accounting_intersects_external_provenance(self):
        runner = self.runner
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            external_content = b"canonical-external-object"
            external_hash = hashlib.sha256(external_content).hexdigest()
            (root / external_hash).write_bytes(external_content)
            generated_content = b"canonical-generated-object"
            generated_hash = hashlib.sha256(generated_content).hexdigest()
            (root / generated_hash).write_bytes(generated_content)
            phantom_hash = hashlib.sha256(
                b"observed-but-never-published-external").hexdigest()
            (root / "notes").write_bytes(b"not a corpus object")
            (root / ("a" * 64)).mkdir()
            outside = root.parent / f"outside-{root.name}"
            outside.write_bytes(b"outside")
            try:
                (root / ("b" * 64)).symlink_to(outside)
                with mock.patch.object(
                        runner.os, "listdir",
                        side_effect=AssertionError("listdir materialized")):
                    counts = runner._count_public_corpus_objects(
                        str(root), {external_hash, phantom_hash})
                self.assertEqual(counts.public, 2)
                self.assertEqual(counts.external, 1)
                self.assertEqual(counts.generated, 1)
                self.assertEqual(
                    counts.generated,
                    counts.public - len({external_hash}),
                )
                self.assertNotEqual(
                    counts.generated,
                    max(0, counts.public - len({external_hash, phantom_hash})),
                )
            finally:
                outside.unlink(missing_ok=True)

    def test_child_outputs_are_invisible_until_parent_fence_promotes_them(self):
        runner = self.runner
        content = b"staged-symbolic-child"
        child_hash = hashlib.sha256(content).hexdigest()
        staging_id = "9" * 32
        with tempfile.TemporaryDirectory() as tmp:
            state = str(Path(tmp) / "state")
            shared = str(Path(tmp) / "corpus")
            Path(shared).mkdir()
            stage = Path(runner._staging_directory(state, 7, staging_id))
            stage.mkdir(parents=True)
            runner._atomic_write(str(stage / child_hash), content)

            self.assertFalse((Path(shared) / child_hash).exists())
            self.assertTrue(runner._verify_staged_outputs(
                state, 7, staging_id, (child_hash,)))
            (stage / "unexpected").write_bytes(b"not-authorized")
            self.assertFalse(runner._verify_staged_outputs(
                state, 7, staging_id, (child_hash,)))
            (stage / "unexpected").unlink()

            with mock.patch.dict(
                os.environ,
                {"SYMCC_SHARED_CORPUS_FILE_MODE": "0644"},
            ):
                runner._promote_staged_outputs(
                    state, shared, 7, staging_id, (child_hash,))
            self.assertEqual(
                runner._file_sha256(str(Path(shared) / child_hash)),
                child_hash,
            )
            self.assertEqual(
                stat.S_IMODE((Path(shared) / child_hash).stat().st_mode),
                0o644,
            )
            runner._remove_staged_outputs(state, 7, staging_id)
            self.assertFalse(stage.exists())

    def test_shared_corpus_mode_is_strict_and_repairs_existing_objects(self):
        runner = self.runner
        content = b"existing corpus mode"
        work_hash = hashlib.sha256(content).hexdigest()
        staging_id = "5" * 32
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = str(root / "state")
            shared = root / "corpus"
            shared.mkdir()
            existing = shared / work_hash
            existing.write_bytes(content)
            existing.chmod(0o600)
            stage = Path(runner._staging_directory(state, 4, staging_id))
            stage.mkdir(parents=True)
            with mock.patch.dict(
                os.environ,
                {"SYMCC_SHARED_CORPUS_FILE_MODE": "0644"},
            ):
                runner._promote_staged_outputs(
                    state, str(shared), 4, staging_id, (work_hash,)
                )
            self.assertEqual(stat.S_IMODE(existing.stat().st_mode), 0o644)
            with mock.patch.dict(
                os.environ,
                {"SYMCC_SHARED_CORPUS_FILE_MODE": "0755"},
            ):
                with self.assertRaisesRegex(ValueError, "non-executable"):
                    runner._shared_corpus_file_mode()

    def test_staged_output_verification_rejects_before_unexpected_io(self):
        runner = self.runner
        expected_hash = hashlib.sha256(b"expected child").hexdigest()
        unexpected_content = b"validly named but unauthorized child"
        unexpected_hash = hashlib.sha256(unexpected_content).hexdigest()
        staging_id = "7" * 32
        with tempfile.TemporaryDirectory() as tmp:
            state = str(Path(tmp) / "state")
            stage = Path(runner._staging_directory(
                state, 9, staging_id))
            stage.mkdir(parents=True)
            (stage / unexpected_hash).write_bytes(unexpected_content)

            with mock.patch.object(runner, "_file_sha256") as digest:
                self.assertFalse(runner._verify_staged_outputs(
                    state, 9, staging_id, (expected_hash,)))
            digest.assert_not_called()

    def test_staged_and_public_symlinks_cannot_satisfy_a_manifest(self):
        runner = self.runner
        content = b"outside corpus object"
        work_hash = hashlib.sha256(content).hexdigest()
        staging_id = "6" * 32
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = str(root / "state")
            shared = root / "corpus"
            shared.mkdir()
            outside = root / "outside"
            outside.write_bytes(content)
            stage = Path(runner._staging_directory(
                state, 11, staging_id))
            stage.mkdir(parents=True)
            (stage / work_hash).symlink_to(outside)

            self.assertFalse(runner._verify_staged_outputs(
                state, 11, staging_id, (work_hash,)))
            (stage / work_hash).unlink()
            stage.rmdir()
            (shared / work_hash).symlink_to(outside)
            self.assertFalse(runner._verify_replayable_outputs(
                state, str(shared), 11, staging_id, (work_hash,)))
            with self.assertRaisesRegex(
                    ValueError, "existing corpus digest mismatch"):
                runner._promote_staged_outputs(
                    state, str(shared), 11, staging_id, (work_hash,))
            self.assertEqual(outside.read_bytes(), content)

    def test_staged_output_stream_closes_on_first_unexpected_name(self):
        runner = self.runner
        expected_hash = hashlib.sha256(b"expected stream child").hexdigest()
        unexpected_hash = hashlib.sha256(
            b"unexpected stream child").hexdigest()

        class UnexpectedEntry:
            name = unexpected_hash

        class EarlyExitScan:
            def __init__(self):
                self.closed = False
                self.yielded = 0

            def __enter__(self):
                return self

            def __exit__(self, _type, _value, _traceback):
                self.closed = True

            def __iter__(self):
                return self

            def __next__(self):
                self.yielded += 1
                if self.yielded == 1:
                    return UnexpectedEntry()
                raise AssertionError("verification read past the first reject")

        scan = EarlyExitScan()
        with mock.patch.object(
                runner.os, "scandir", return_value=scan), mock.patch.object(
                runner, "_file_sha256") as digest:
            self.assertIsNone(runner._verified_staged_output_hashes(
                "streamed-stage", {expected_hash}))
        self.assertTrue(scan.closed)
        self.assertEqual(scan.yielded, 1)
        digest.assert_not_called()

    def test_result_discovery_is_streamed_flat_and_hard_bounded(self):
        runner = self.runner
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "first").write_bytes(b"1234")
            (root / "first.hints").write_text("0:00:01\n")
            (root / "second").write_bytes(b"567")
            discovered = runner._discover_result_files(
                tmp, max_objects=2, max_bytes=7)
            self.assertEqual(
                {Path(path).name for path in discovered},
                {"first", "second"},
            )

            with self.assertRaises(runner._ResultBudgetExceeded) as count:
                runner._discover_result_files(
                    tmp, max_objects=1, max_bytes=7)
            self.assertEqual(count.exception.resource, "objects")
            self.assertEqual(count.exception.observed, 2)
            self.assertEqual(count.exception.limit, 1)

            with self.assertRaises(runner._ResultBudgetExceeded) as size:
                runner._discover_result_files(
                    tmp, max_objects=2, max_bytes=6)
            self.assertEqual(size.exception.resource, "bytes")
            self.assertGreater(size.exception.observed, 6)
            self.assertEqual(size.exception.limit, 6)

            (root / "alias").symlink_to(root / "first")
            with self.assertRaisesRegex(ValueError, "not a regular file"):
                runner._discover_result_files(
                    tmp, max_objects=3, max_bytes=7)

        class Entry:
            name = "entry"
            path = "entry"

            @staticmethod
            def stat(*, follow_symlinks):
                self.assertFalse(follow_symlinks)
                return os.stat_result((stat.S_IFREG, 0, 0, 0, 0, 0, 1,
                                       0, 0, 0))

        class BoundedScan:
            def __init__(self):
                self.closed = False
                self.yielded = 0

            def __enter__(self):
                return self

            def __exit__(self, _type, _value, _traceback):
                self.closed = True

            def __iter__(self):
                return self

            def __next__(self):
                self.yielded += 1
                if self.yielded <= 2:
                    return Entry()
                raise AssertionError("discovery read beyond the hard limit")

        scan = BoundedScan()
        with mock.patch.object(runner.os, "scandir", return_value=scan):
            with self.assertRaises(runner._ResultBudgetExceeded):
                runner._discover_result_files(
                    "bounded", max_objects=1, max_bytes=2)
        self.assertTrue(scan.closed)
        self.assertEqual(scan.yielded, 2)

    def test_dispatch_window_uses_target_budget_not_python_backstop(self):
        runner = self.runner
        self.assertTrue(runner._dispatch_window_open(5, 100, 1, 100))
        self.assertTrue(runner._dispatch_window_open(5, 100, 1, 103.9))
        self.assertFalse(runner._dispatch_window_open(5, 100, 1, 104))
        self.assertTrue(runner._dispatch_window_open(0, 100, 30, 10_000))
        self.assertFalse(runner._dispatch_window_open(
            float("nan"), 100, 1, 100))

    def test_worker_staging_streams_and_counts_duplicate_logical_bytes(self):
        runner = self.runner
        content = (b"bounded-stream-copy" * 1000) + b"tail"
        work_hash = hashlib.sha256(content).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            duplicate = root / "duplicate"
            source.write_bytes(content)
            duplicate.write_bytes(content)
            state = str(root / "state")
            requested = []
            real_read = os.read

            def observed_read(descriptor, size):
                requested.append(size)
                return real_read(descriptor, size)

            with mock.patch.object(runner, "_RESULT_STREAM_CHUNK_BYTES", 97), \
                    mock.patch.object(runner.os, "read", observed_read):
                hashes, staged_bytes, staging_id = \
                    runner._stage_worker_outputs(
                        (str(source), str(duplicate)),
                        state,
                        5,
                        max_objects=2,
                        max_bytes=len(content) * 2,
                    )

            self.assertEqual(hashes, (work_hash, work_hash))
            self.assertEqual(staged_bytes, len(content) * 2)
            self.assertTrue(requested)
            self.assertLessEqual(max(requested), 97)
            stage = Path(runner._staging_directory(state, 5, staging_id))
            self.assertEqual(
                {entry.name for entry in stage.iterdir()}, {work_hash})
            self.assertEqual(runner._file_sha256(str(stage / work_hash)),
                             work_hash)
            self.assertTrue(runner._verify_staged_outputs(
                state,
                5,
                staging_id,
                hashes,
                declared_bytes=len(content) * 2,
                max_objects=2,
                max_bytes=len(content) * 2,
            ))
            self.assertFalse(runner._verify_staged_outputs(
                state,
                5,
                staging_id,
                hashes,
                declared_bytes=len(content),
                max_objects=2,
                max_bytes=len(content) * 2,
            ))
            with self.assertRaises(runner._ResultBudgetExceeded) as size:
                runner._verify_staged_outputs(
                    state,
                    5,
                    staging_id,
                    hashes,
                    declared_bytes=len(content) * 2,
                    max_objects=2,
                    max_bytes=(len(content) * 2) - 1,
                )
            self.assertEqual(size.exception.payload(), {
                "resource": "bytes",
                "observed": len(content) * 2,
                "limit": (len(content) * 2) - 1,
            })

    def test_worker_staging_rejects_oversize_and_symlink_without_residue(self):
        runner = self.runner
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.write_bytes(b"12345")
            state = str(root / "state")
            with self.assertRaises(runner._ResultBudgetExceeded) as size:
                runner._stage_worker_outputs(
                    (str(source),),
                    state,
                    4,
                    max_objects=1,
                    max_bytes=4,
                )
            self.assertEqual(size.exception.payload(), {
                "resource": "bytes", "observed": 5, "limit": 4,
            })
            staging_root = Path(state) / "staging" / "4"
            self.assertEqual(
                tuple(staging_root.iterdir()) if staging_root.exists() else (),
                (),
            )

            alias = root / "alias"
            alias.symlink_to(source)
            with self.assertRaises(OSError):
                runner._stage_worker_outputs(
                    (str(alias),),
                    state,
                    4,
                    max_objects=1,
                    max_bytes=5,
                )
            self.assertEqual(
                tuple(staging_root.iterdir()) if staging_root.exists() else (),
                (),
            )

    def test_worker_staging_write_failure_closes_fds_and_removes_stage(self):
        runner = self.runner
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.write_bytes(b"stream-write-failure")
            state = str(root / "state")
            real_close = os.close
            with mock.patch.object(
                    runner.os, "write", side_effect=OSError("offline")), \
                    mock.patch.object(
                        runner.os, "close", wraps=real_close) as closed:
                with self.assertRaisesRegex(OSError, "offline"):
                    runner._stage_worker_outputs(
                        (str(source),),
                        state,
                        8,
                        max_objects=1,
                        max_bytes=1024,
                    )
            self.assertGreaterEqual(closed.call_count, 2)
            staging_root = Path(state) / "staging" / "8"
            self.assertEqual(
                tuple(staging_root.iterdir()) if staging_root.exists() else (),
                (),
            )

    def test_worker_budget_failure_retains_stage_identity_for_master_cleanup(self):
        runner = self.runner
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.write_bytes(b"oversize-with-cleanup-retry")
            state = str(root / "state")
            with mock.patch.object(
                    runner,
                    "_remove_staged_outputs",
                    side_effect=OSError("cleanup unavailable"),
            ) as cleanup:
                with self.assertRaises(
                        runner._ResultBudgetExceeded) as failure:
                    runner._stage_worker_outputs(
                        (str(source),),
                        state,
                        12,
                        max_objects=1,
                        max_bytes=4,
                    )
            staging_id = failure.exception.staging_id
            self.assertRegex(staging_id, r"^[0-9a-f]{32}$")
            cleanup.assert_called_once_with(state, 12, staging_id)
            stage = Path(runner._staging_directory(state, 12, staging_id))
            self.assertTrue(stage.is_dir())
            runner._remove_staged_outputs(state, 12, staging_id)
            self.assertFalse(stage.exists())

    def test_partial_child_promotion_is_replayable_and_idempotent(self):
        runner = self.runner
        contents = (b"first durable child", b"second durable child")
        hashes = tuple(hashlib.sha256(content).hexdigest()
                       for content in contents)
        staging_id = "8" * 32
        with tempfile.TemporaryDirectory() as tmp:
            state = str(Path(tmp) / "state")
            shared = str(Path(tmp) / "corpus")
            Path(shared).mkdir()
            stage = Path(runner._staging_directory(state, 6, staging_id))
            stage.mkdir(parents=True)
            for content, work_hash in zip(contents, hashes):
                runner._atomic_write(str(stage / work_hash), content)

            os.replace(stage / hashes[0], Path(shared) / hashes[0])
            self.assertTrue(runner._verify_replayable_outputs(
                state, shared, 6, staging_id, hashes))
            runner._promote_staged_outputs(
                state, shared, 6, staging_id, hashes)
            runner._promote_staged_outputs(
                state, shared, 6, staging_id, hashes)
            self.assertEqual(
                tuple(runner._file_sha256(str(Path(shared) / work_hash))
                      for work_hash in hashes),
                hashes,
            )

    def test_standalone_commit_manifest_and_epoch_are_canonical(self):
        runner = self.runner
        child = "a" * 64
        manifest = runner._standalone_commit_manifest(
            7, "b" * 32, (child, child), 2)
        self.assertEqual(manifest, {
            "schema": "symcc-standalone-result-commit-v1",
            "worker_global_rank": 7,
            "staging_id": "b" * 32,
            "hashes": [child],
            "num_generated": 2,
        })
        self.assertEqual(
            runner._normalize_standalone_commit_manifest(manifest),
            manifest,
        )
        malformed = dict(manifest or {})
        malformed["extra"] = True
        self.assertIsNone(
            runner._normalize_standalone_commit_manifest(malformed))
        self.assertIsNone(runner._standalone_commit_manifest(
            7, [], (), 0))

        self.assertEqual(
            runner._select_work_epoch("c" * 64), ("c" * 64, True))
        self.assertEqual(
            runner._select_work_epoch(
                "", random_bytes=lambda size: b"\x01" * size),
            ("01" * 32, False),
        )
        with self.assertRaisesRegex(ValueError, "64 lowercase hex"):
            runner._select_work_epoch("C" * 64)

        with tempfile.TemporaryDirectory() as tmp:
            runner._ensure_work_state_metadata(
                tmp, epoch="d" * 64, shard_count=64)
            runner._ensure_work_state_metadata(
                tmp, epoch="d" * 64, shard_count=64)
            with self.assertRaisesRegex(ValueError, "does not match"):
                runner._ensure_work_state_metadata(
                    tmp, epoch="d" * 64, shard_count=32)

        with tempfile.TemporaryDirectory() as tmp:
            state = importlib.import_module("distributed_state")
            with mock.patch.object(
                    state, "fsync_directory",
                    side_effect=OSError("metadata-directory-offline")):
                with self.assertRaisesRegex(
                        OSError, "metadata-directory-offline"):
                    runner._ensure_work_state_metadata(
                        tmp, epoch="e" * 64, shard_count=64)
            # link(2) may already be visible when directory fsync reports an
            # uncertain commit. A retry validates and reuses that exact epoch.
            runner._ensure_work_state_metadata(
                tmp, epoch="e" * 64, shard_count=64)

    def test_runtime_lock_configuration_manifest_is_consensus_gated(self):
        runner = self.runner
        epoch = hashlib.sha256(b"durable-renewal-configuration").hexdigest()

        def controller(jitter):
            value = runner.ClusterLockRenewalController(
                epoch=epoch,
                interval=60.0,
                timeout=5.0,
                completed_at=0.0,
                jitter_fraction=jitter,
            )
            return value

        with tempfile.TemporaryDirectory() as tmp:
            unsealed = controller(0.1)
            with self.assertRaisesRegex(RuntimeError, "not established"):
                runner._ensure_runtime_lock_configuration_manifest(
                    tmp, unsealed)
            self.assertFalse(
                (Path(tmp) / runner._RUNTIME_LOCK_CONFIGURATION_MANIFEST
                 ).exists())

            sealed = controller(0.1)
            sealed._record_configuration_consensus(
                sealed.configuration_fingerprint)
            expected = runner._ensure_runtime_lock_configuration_manifest(
                tmp, sealed)
            self.assertEqual(expected["epoch"], epoch)
            self.assertEqual(
                expected["fingerprint"], sealed.configuration_fingerprint)
            with mock.patch.object(runner, "durable_link") as publish:
                self.assertEqual(
                    runner._ensure_runtime_lock_configuration_manifest(
                        tmp, sealed),
                    expected,
                )
            publish.assert_not_called()

            drifted = controller(0.2)
            drifted._record_configuration_consensus(
                drifted.configuration_fingerprint)
            with mock.patch.object(runner, "durable_link") as publish:
                with self.assertRaisesRegex(
                        ValueError, "does not match consensus"):
                    runner._ensure_runtime_lock_configuration_manifest(
                        tmp, drifted)
            publish.assert_not_called()
            self.assertEqual(
                runner._ensure_runtime_lock_configuration_manifest(
                    tmp, sealed),
                expected,
            )
            self.assertEqual(tuple(Path(tmp).glob("*.tmp")), ())

    def test_runtime_lock_configuration_manifest_fails_closed_and_retries(self):
        runner = self.runner
        epoch = hashlib.sha256(b"renewal-manifest-durability").hexdigest()
        controller = runner.ClusterLockRenewalController(
            epoch=epoch,
            interval=60.0,
            timeout=5.0,
            completed_at=0.0,
            jitter_fraction=0.1,
        )
        controller._record_configuration_consensus(
            controller.configuration_fingerprint)

        with tempfile.TemporaryDirectory() as tmp:
            state = importlib.import_module("distributed_state")
            with mock.patch.object(
                    state, "fsync_directory",
                    side_effect=OSError("manifest-directory-offline")):
                with self.assertRaisesRegex(
                        OSError, "manifest-directory-offline"):
                    runner._ensure_runtime_lock_configuration_manifest(
                        tmp, controller)
            expected = runner._ensure_runtime_lock_configuration_manifest(
                tmp, controller)
            self.assertEqual(
                expected["fingerprint"], controller.configuration_fingerprint)

        with tempfile.TemporaryDirectory() as tmp:
            runner._ensure_runtime_lock_configuration_manifest(
                tmp, controller)
            path = Path(tmp) / runner._RUNTIME_LOCK_CONFIGURATION_MANIFEST
            with path.open("ab") as stream:
                stream.write(b"corrupt")
            with self.assertRaisesRegex(
                    ValueError, "does not match consensus"):
                runner._ensure_runtime_lock_configuration_manifest(
                    tmp, controller)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / runner._RUNTIME_LOCK_CONFIGURATION_MANIFEST
            path.mkdir()
            with self.assertRaisesRegex(ValueError, "unreadable"):
                runner._ensure_runtime_lock_configuration_manifest(
                    tmp, controller)

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "unexpected-target"
            target.write_text("not a manifest\n", encoding="ascii")
            path = Path(tmp) / runner._RUNTIME_LOCK_CONFIGURATION_MANIFEST
            path.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "unreadable"):
                runner._ensure_runtime_lock_configuration_manifest(
                    tmp, controller)

    def test_runtime_lock_configuration_manifest_has_one_race_winner(self):
        runner = self.runner
        epoch = hashlib.sha256(b"renewal-manifest-race").hexdigest()
        controllers = []
        for jitter in (0.1, 0.25):
            controller = runner.ClusterLockRenewalController(
                epoch=epoch,
                interval=60.0,
                timeout=5.0,
                completed_at=0.0,
                jitter_fraction=jitter,
            )
            controller._record_configuration_consensus(
                controller.configuration_fingerprint)
            controllers.append(controller)

        with tempfile.TemporaryDirectory() as tmp:
            barrier = threading.Barrier(2)
            outcomes = []

            def publish(controller):
                barrier.wait()
                try:
                    manifest = (
                        runner._ensure_runtime_lock_configuration_manifest(
                            tmp, controller)
                    )
                    outcomes.append(("published", manifest["fingerprint"]))
                except ValueError as error:
                    outcomes.append(("rejected", str(error)))

            threads = [
                threading.Thread(target=publish, args=(controller,))
                for controller in controllers
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5.0)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(
                sorted(outcome for outcome, _ in outcomes),
                ["published", "rejected"],
            )
            winner = next(value for outcome, value in outcomes
                          if outcome == "published")
            winning_controller = next(
                controller for controller in controllers
                if controller.configuration_fingerprint == winner
            )
            losing_controller = next(
                controller for controller in controllers
                if controller.configuration_fingerprint != winner
            )
            self.assertEqual(
                runner._ensure_runtime_lock_configuration_manifest(
                    tmp, winning_controller)["fingerprint"],
                winner,
            )
            with self.assertRaisesRegex(ValueError, "does not match consensus"):
                runner._ensure_runtime_lock_configuration_manifest(
                    tmp, losing_controller)

    def test_completed_work_state_cleanup_is_durable_and_scoped(self):
        runner = self.runner
        with tempfile.TemporaryDirectory() as tmp:
            shared = Path(tmp) / "shared"
            work = shared / (".standalone-work-" + "a" * 64)
            work.mkdir(parents=True)
            marker = work / "committed-state"
            marker.write_bytes(b"retire-me")
            retirement_id = "1" * 32
            retired = shared / runner._retired_work_state_name(
                "a" * 64, retirement_id)
            with mock.patch.object(
                    runner.os, "urandom",
                    return_value=bytes.fromhex(retirement_id)), mock.patch.object(
                    runner, "durable_rename_noreplace",
                    wraps=runner.durable_rename_noreplace
            ) as replace, mock.patch.object(
                    runner, "durable_rmtree", wraps=runner.durable_rmtree
            ) as remove:
                observed = runner._cleanup_completed_work_state(
                    str(work), str(shared), remove_shared_dir=False)
            self.assertFalse(work.exists())
            self.assertTrue(shared.is_dir())
            self.assertEqual(observed, str(retired))
            self.assertEqual(
                (retired / marker.name).read_bytes(), b"retire-me")
            replace.assert_called_once_with(str(work), str(retired))
            remove.assert_not_called()

        with tempfile.TemporaryDirectory() as tmp:
            shared = Path(tmp) / "shared"
            work = shared / (".standalone-work-" + "a" * 64)
            work.mkdir(parents=True)
            (work / "active-state").write_bytes(b"must-not-move")
            retirement_id = "1" * 32
            retired = shared / runner._retired_work_state_name(
                "a" * 64, retirement_id)
            retired.mkdir()
            (retired / "retired-state").write_bytes(b"must-not-clobber")
            with mock.patch.object(
                    runner.os, "urandom",
                    return_value=bytes.fromhex(retirement_id)):
                with self.assertRaises(FileExistsError):
                    runner._cleanup_completed_work_state(
                        str(work), str(shared), remove_shared_dir=False)
            self.assertEqual(
                (work / "active-state").read_bytes(), b"must-not-move")
            self.assertEqual(
                (retired / "retired-state").read_bytes(),
                b"must-not-clobber",
            )

        with tempfile.TemporaryDirectory() as tmp:
            shared = Path(tmp) / "shared"
            work = shared / (".standalone-work-" + "b" * 64)
            work.mkdir(parents=True)
            runner._cleanup_completed_work_state(
                str(work), str(shared), remove_shared_dir=True)
            self.assertFalse(shared.exists())

        with tempfile.TemporaryDirectory() as tmp:
            shared = Path(tmp) / "shared"
            outside = Path(tmp) / "outside"
            shared.mkdir()
            outside.mkdir()
            with self.assertRaisesRegex(ValueError, "direct child"):
                runner._cleanup_completed_work_state(
                    str(outside), str(shared), remove_shared_dir=False)

        for name in (
            "corpus",
            ".standalone-work-" + "c" * 63,
            ".standalone-work-" + "D" * 64,
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                shared = Path(tmp) / "shared"
                candidate = shared / name
                candidate.mkdir(parents=True)
                marker = candidate / "must-remain"
                marker.write_bytes(b"not-completed-work-state")
                with self.assertRaisesRegex(
                        ValueError, "invalid completed work-state identity"):
                    runner._cleanup_completed_work_state(
                        str(candidate), str(shared), remove_shared_dir=False)
                self.assertEqual(
                    marker.read_bytes(), b"not-completed-work-state")

        with tempfile.TemporaryDirectory() as tmp:
            shared = Path(tmp) / "shared"
            target = Path(tmp) / "outside-state"
            shared.mkdir()
            target.mkdir()
            work = shared / (".standalone-work-" + "e" * 64)
            work.symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "not a real directory"):
                runner._cleanup_completed_work_state(
                    str(work), str(shared), remove_shared_dir=False)
            self.assertTrue(target.is_dir())

    def test_retired_work_state_gc_is_bounded_and_prevalidated(self):
        runner = self.runner
        with tempfile.TemporaryDirectory() as tmp:
            shared = Path(tmp) / "shared"
            shared.mkdir()
            names = [
                runner._retired_work_state_name("a" * 64, "1" * 32),
                runner._retired_work_state_name("b" * 64, "2" * 32),
                runner._retired_work_state_name("c" * 64, "3" * 32),
            ]
            for name in names:
                directory = shared / name
                directory.mkdir()
                (directory / "state").write_bytes(name.encode("ascii"))
            unrelated = shared / "corpus"
            unrelated.mkdir()

            reclaimed = runner._reclaim_retired_work_states(
                str(shared), limit=1, lock_timeout=1.0)
            self.assertEqual(reclaimed.reclaimed_roots, (names[0],))
            self.assertIsNone(reclaimed.partial_root)
            self.assertEqual(reclaimed.removed_entries, 2)
            self.assertEqual(reclaimed.stop_reason, "root-limit")
            self.assertEqual(reclaimed.scanned_entries, 5)
            self.assertEqual(reclaimed.candidate_roots, 3)
            self.assertFalse((shared / names[0]).exists())
            self.assertTrue((shared / names[1]).is_dir())
            self.assertTrue((shared / names[2]).is_dir())
            self.assertTrue(unrelated.is_dir())
            self.assertTrue(
                (shared / runner._RETIRED_WORK_STATE_GC_LOCK).is_file())

            disabled = runner._reclaim_retired_work_states(
                str(shared), limit=0, lock_timeout=1.0)
            self.assertEqual(disabled.reclaimed_roots, ())
            self.assertEqual(disabled.removed_entries, 0)
            self.assertEqual(disabled.stop_reason, "disabled")
            self.assertEqual(disabled.scanned_entries, 0)
            self.assertEqual(disabled.candidate_roots, 0)
            remainder = runner._reclaim_retired_work_states(
                str(shared), limit=10, lock_timeout=1.0)
            self.assertEqual(remainder.reclaimed_roots, tuple(names[1:]))
            self.assertEqual(remainder.removed_entries, 4)
            self.assertEqual(remainder.stop_reason, "complete")
            self.assertEqual(remainder.scanned_entries, 4)
            self.assertEqual(remainder.candidate_roots, 2)
            self.assertTrue(unrelated.is_dir())

        for malformed_type in ("malformed", "symlink"):
            with self.subTest(malformed_type=malformed_type), \
                    tempfile.TemporaryDirectory() as tmp:
                shared = Path(tmp) / "shared"
                shared.mkdir()
                valid_name = runner._retired_work_state_name(
                    "d" * 64, "4" * 32)
                valid = shared / valid_name
                valid.mkdir()
                (valid / "must-remain").write_bytes(b"prevalidation")
                if malformed_type == "malformed":
                    malformed = shared / (
                        runner._RETIRED_WORK_STATE_PREFIX + "broken")
                    malformed.mkdir()
                    expected = "malformed reserved"
                else:
                    target = Path(tmp) / "outside-retired"
                    target.mkdir()
                    malformed = shared / runner._retired_work_state_name(
                        "e" * 64, "5" * 32)
                    malformed.symlink_to(target, target_is_directory=True)
                    expected = "not a real directory"
                with self.assertRaisesRegex(ValueError, expected):
                    runner._reclaim_retired_work_states(
                        str(shared), limit=10, lock_timeout=1.0)
                self.assertEqual(
                    (valid / "must-remain").read_bytes(), b"prevalidation")

    def test_retired_work_state_gc_streams_a_bounded_lexical_top_k(self):
        runner = self.runner
        with tempfile.TemporaryDirectory() as tmp:
            shared = Path(tmp) / "shared"
            shared.mkdir()
            names = [
                runner._retired_work_state_name(
                    f"{index:064x}", f"{index:032x}")
                for index in range(33)
            ]
            for name in reversed(names):
                (shared / name).mkdir()

            peak_retained = 0
            real_push = runner.heapq.heappush
            real_replace = runner.heapq.heapreplace

            def tracked_push(heap, item):
                nonlocal peak_retained
                result = real_push(heap, item)
                peak_retained = max(peak_retained, len(heap))
                return result

            def tracked_replace(heap, item):
                nonlocal peak_retained
                result = real_replace(heap, item)
                peak_retained = max(peak_retained, len(heap))
                return result

            with mock.patch.object(
                    runner.heapq, "heappush", side_effect=tracked_push), \
                    mock.patch.object(
                        runner.heapq,
                        "heapreplace",
                        side_effect=tracked_replace,
                    ):
                result = runner._reclaim_retired_work_states(
                    str(shared),
                    limit=4,
                    lock_timeout=1.0,
                    entry_budget=16,
                    time_budget=1.0,
                )

            self.assertEqual(result.reclaimed_roots, tuple(names[:4]))
            self.assertEqual(result.removed_entries, 4)
            self.assertEqual(result.stop_reason, "root-limit")
            self.assertEqual(result.scanned_entries, 34)
            self.assertEqual(result.candidate_roots, 33)
            self.assertEqual(peak_retained, 4)
            self.assertTrue(all(not (shared / name).exists()
                                for name in names[:4]))
            self.assertTrue(all((shared / name).is_dir()
                                for name in names[4:]))

    def test_retired_work_state_gc_resumes_with_a_hard_entry_budget(self):
        runner = self.runner
        with tempfile.TemporaryDirectory() as tmp:
            shared = Path(tmp) / "shared"
            shared.mkdir()
            name = runner._retired_work_state_name("a" * 64, "7" * 32)
            retired = shared / name
            leaf = retired / "one" / "two"
            leaf.mkdir(parents=True)
            for index in range(5):
                (leaf / f"state-{index}").write_bytes(b"retired")

            steps = []
            while retired.exists():
                result = runner._reclaim_retired_work_states(
                    str(shared),
                    limit=1,
                    lock_timeout=1.0,
                    entry_budget=2,
                    time_budget=1.0,
                )
                steps.append(result)

            self.assertEqual(
                [result.removed_entries for result in steps],
                [2, 2, 2, 2],
            )
            self.assertEqual(
                [result.reclaimed_roots for result in steps],
                [(), (), (), (name,)],
            )
            self.assertEqual(
                [result.partial_root for result in steps],
                [name, name, name, None],
            )
            self.assertEqual(
                [result.stop_reason for result in steps],
                ["entry-budget", "entry-budget", "entry-budget", "complete"],
            )

    def test_retirement_noreplace_probe_is_strict_and_self_cleaning(self):
        runner = self.runner
        with tempfile.TemporaryDirectory() as tmp:
            shared = Path(tmp) / "shared"
            shared.mkdir()
            runner._probe_retirement_noreplace(str(shared))
            self.assertEqual(tuple(shared.iterdir()), ())

        with tempfile.TemporaryDirectory() as tmp:
            shared = Path(tmp) / "shared"
            shared.mkdir()
            with mock.patch.object(
                    runner, "durable_rename_noreplace",
                    side_effect=OSError("primitive unavailable")):
                with self.assertRaisesRegex(OSError, "primitive unavailable"):
                    runner._probe_retirement_noreplace(str(shared))
            self.assertEqual(tuple(shared.iterdir()), ())

        with tempfile.TemporaryDirectory() as tmp:
            shared = Path(tmp) / "shared"
            shared.mkdir()
            with mock.patch.object(
                    runner, "fsync_directory",
                    side_effect=OSError("probe durability unavailable")):
                with self.assertRaisesRegex(
                        OSError, "probe durability unavailable"):
                    runner._probe_retirement_noreplace(str(shared))
            self.assertEqual(tuple(shared.iterdir()), ())

        with tempfile.TemporaryDirectory() as tmp:
            shared = Path(tmp) / "shared"
            shared.mkdir()

            def clobbering_rename(source, destination):
                os.replace(source, destination)

            with mock.patch.object(
                    runner, "durable_rename_noreplace",
                    side_effect=clobbering_rename):
                with self.assertRaisesRegex(
                        OSError, "did not reject an existing destination"):
                    runner._probe_retirement_noreplace(str(shared))
            self.assertEqual(tuple(shared.iterdir()), ())

    def test_visible_retirement_with_failed_fsync_is_reclaimable(self):
        runner = self.runner
        with tempfile.TemporaryDirectory() as tmp:
            shared = Path(tmp) / "shared"
            work = shared / (".standalone-work-" + "f" * 64)
            work.mkdir(parents=True)
            (work / "state").write_bytes(b"committed")
            retirement_id = "6" * 32
            retired_name = runner._retired_work_state_name(
                "f" * 64, retirement_id)
            retired = shared / retired_name

            def visible_then_uncertain(source, destination):
                os.replace(source, destination)
                raise OSError("injected directory fsync failure")

            with mock.patch.object(
                    runner.os, "urandom",
                    return_value=bytes.fromhex(retirement_id)), mock.patch.object(
                    runner, "durable_rename_noreplace",
                    side_effect=visible_then_uncertain):
                with self.assertRaisesRegex(OSError, "fsync failure"):
                    runner._cleanup_completed_work_state(
                        str(work), str(shared), remove_shared_dir=False)

            self.assertFalse(work.exists())
            self.assertEqual(
                (retired / "state").read_bytes(), b"committed")
            reclaimed = runner._reclaim_retired_work_states(
                str(shared), limit=1, lock_timeout=1.0)
            self.assertEqual(reclaimed.reclaimed_roots, (retired_name,))
            self.assertFalse(retired.exists())

    def test_retired_gc_budget_environment_is_strict(self):
        runner = self.runner
        name = "SYMCC_RETIRED_WORK_STATE_GC_LIMIT"
        with mock.patch.dict(os.environ, {name: "7"}):
            self.assertEqual(runner._environment_integer(name, 1, 0, 10), 7)
        for malformed in ("", "1.5", "-1", "11"):
            with self.subTest(malformed=malformed), mock.patch.dict(
                    os.environ, {name: malformed}):
                with self.assertRaisesRegex(ValueError, name):
                    runner._environment_integer(name, 1, 0, 10)

        entry_name = "SYMCC_RETIRED_WORK_STATE_GC_ENTRY_BUDGET"
        with mock.patch.dict(os.environ, {entry_name: "257"}):
            self.assertEqual(
                runner._environment_integer(entry_name, 4096, 1, 1000), 257)
        for malformed in ("", "1.5", "0", "1001"):
            with self.subTest(malformed=malformed), mock.patch.dict(
                    os.environ, {entry_name: malformed}):
                with self.assertRaisesRegex(ValueError, entry_name):
                    runner._environment_integer(entry_name, 4096, 1, 1000)

        time_name = "SYMCC_RETIRED_WORK_STATE_GC_TIME_BUDGET_SECONDS"
        with mock.patch.dict(os.environ, {time_name: "0.125"}):
            self.assertEqual(
                runner._environment_strict_float(
                    time_name, 0.05, 0.001, 1.0),
                0.125,
            )
        for malformed in ("", "nan", "inf", "0", "1.001"):
            with self.subTest(malformed=malformed), mock.patch.dict(
                    os.environ, {time_name: malformed}):
                with self.assertRaisesRegex(ValueError, time_name):
                    runner._environment_strict_float(
                        time_name, 0.05, 0.001, 1.0)

        for name, default, minimum, maximum in (
            (
                "SYMCC_STANDALONE_RESULT_MAX_OBJECTS",
                runner._DEFAULT_RESULT_MAX_OBJECTS,
                1,
                runner._MAX_RESULT_MAX_OBJECTS,
            ),
            (
                "SYMCC_STANDALONE_RESULT_MAX_BYTES",
                runner._DEFAULT_RESULT_MAX_BYTES,
                1,
                runner._MAX_RESULT_MAX_BYTES,
            ),
            (
                "SYMCC_STANDALONE_INPUT_MAX_BYTES",
                runner._DEFAULT_INPUT_MAX_BYTES,
                1,
                runner._MAX_INPUT_MAX_BYTES,
            ),
        ):
            with self.subTest(name=name), mock.patch.dict(
                    os.environ, {name: str(minimum)}):
                self.assertEqual(
                    runner._environment_integer(
                        name, default, minimum, maximum),
                    minimum,
                )
            for malformed in ("", "0", str(maximum + 1), "1.5"):
                with self.subTest(name=name, malformed=malformed), \
                        mock.patch.dict(os.environ, {name: malformed}):
                    with self.assertRaisesRegex(ValueError, name):
                        runner._environment_integer(
                            name, default, minimum, maximum)

        with mock.patch.dict(os.environ, {
            "SYMCC_STANDALONE_RESULT_MAX_OBJECTS": "3",
            "SYMCC_STANDALONE_RESULT_MAX_BYTES": "11",
            "SYMCC_STANDALONE_INPUT_MAX_BYTES": "10",
        }, clear=False):
            with self.assertRaisesRegex(
                    ValueError, "RESULT_MAX_BYTES must not exceed"):
                runner._standalone_admission_budgets()

    def test_worker_result_payload_is_strict(self):
        valid_hash = "a" * 64
        self.assertEqual(
            self.runner._worker_result_payload({
                "new_hashes": [valid_hash],
                "num_generated": 1,
                "staged_bytes": 17,
                "input_hash": "b" * 64,
                "staging_id": "d" * 32,
            }, expected_input_hash="b" * 64),
            ((valid_hash,), 1, 17),
        )
        self.assertEqual(
            self.runner._worker_result_payload({
                "new_hashes": [],
                "num_generated": 0,
                "staged_bytes": 0,
                "staging_id": "",
            }),
            ((), 0, 0),
        )
        self.assertIsNone(
            self.runner._worker_result_payload({
                "new_hashes": [valid_hash],
                "num_generated": 1,
                "staged_bytes": 17,
                "input_hash": "c" * 64,
                "staging_id": "d" * 32,
            }, expected_input_hash="b" * 64),
        )
        self.assertEqual(
            self.runner._worker_result_payload({
                "new_hashes": [valid_hash],
                "num_generated": 1,
                "staged_bytes": 17,
                "staging_id": "d" * 32,
            }),
            ((valid_hash,), 1, 17),
        )
        for malformed in (
            None,
            {},
            {"new_hashes": ["not-a-hash"], "num_generated": 1},
            {
                "new_hashes": [valid_hash],
                "num_generated": True,
                "staged_bytes": 17,
                "staging_id": "d" * 32,
            },
            {
                "new_hashes": [valid_hash],
                "num_generated": 0,
                "staged_bytes": 17,
                "staging_id": "d" * 32,
            },
            {
                "new_hashes": [valid_hash],
                "num_generated": 1,
                "staging_id": "d" * 32,
            },
            {
                "new_hashes": [valid_hash],
                "num_generated": 1,
                "staged_bytes": 17,
                "staging_id": "d" * 32,
                "protocol_error": [],
            },
        ):
            with self.subTest(malformed=malformed):
                self.assertIsNone(
                    self.runner._worker_result_payload(malformed))

        with self.assertRaises(self.runner._ResultBudgetExceeded) as count:
            self.runner._worker_result_payload({
                "new_hashes": [valid_hash, valid_hash],
                "num_generated": 2,
                "staged_bytes": 2,
                "staging_id": "d" * 32,
            }, max_objects=1, max_bytes=10)
        self.assertEqual(count.exception.payload(), {
            "resource": "objects", "observed": 2, "limit": 1,
        })
        with self.assertRaises(self.runner._ResultBudgetExceeded) as size:
            self.runner._worker_result_payload({
                "new_hashes": [valid_hash],
                "num_generated": 1,
                "staged_bytes": 11,
                "staging_id": "d" * 32,
            }, max_objects=1, max_bytes=10)
        self.assertEqual(size.exception.payload(), {
            "resource": "bytes", "observed": 11, "limit": 10,
        })

        violation = self.runner._worker_result_budget_violation({
            "new_hashes": [],
            "num_generated": 0,
            "staged_bytes": 0,
            "input_hash": "b" * 64,
            "staging_id": "e" * 32,
            "protocol_error": "result-budget-exceeded",
            "result_budget": {
                "resource": "bytes", "observed": 11, "limit": 10,
            },
        }, expected_input_hash="b" * 64, max_objects=1, max_bytes=10)
        self.assertIsInstance(violation, self.runner._ResultBudgetExceeded)
        self.assertEqual(str(violation), (
            "standalone result bytes budget exceeded: observed=11, limit=10"
        ))

        input_violation = self.runner._worker_input_budget_violation({
            "new_hashes": [],
            "num_generated": 0,
            "staged_bytes": 0,
            "input_hash": "b" * 64,
            "staging_id": "",
            "protocol_error": "input-budget-exceeded",
            "input_budget": {"observed": 12, "limit": 10},
        }, expected_input_hash="b" * 64, max_bytes=10)
        self.assertIsInstance(
            input_violation, self.runner._InputBudgetExceeded)
        self.assertEqual(input_violation.payload(), {
            "observed": 12, "limit": 10,
        })

    def test_idle_timeout_uses_elapsed_time_not_rounded_poll_count(self):
        remaining = self.runner._idle_time_remaining
        self.assertEqual(remaining(10.0, 1.0, 10.0), 1.0)
        self.assertAlmostEqual(remaining(10.0, 1.0, 10.25), 0.75)
        self.assertEqual(remaining(10.0, 1.0, 11.0), 0.0)
        self.assertEqual(remaining(10.0, -1.0, 10.0), 0.0)
        self.assertEqual(remaining(10.0, 1.0, float("nan")), 0.0)

    def test_rendezvous_work_owner_is_deterministic_and_minimally_remaps(self):
        owner = self.runner._rendezvous_work_owner
        work_hashes = tuple(
            hashlib.sha256(f"seed-{index}".encode("ascii")).hexdigest()
            for index in range(600)
        )
        original = {work_hash: owner(work_hash, (0, 1, 2))
                    for work_hash in work_hashes}
        repeated = {work_hash: owner(work_hash, (2, 0, 1, 1))
                    for work_hash in work_hashes}
        expanded = {work_hash: owner(work_hash, (0, 1, 2, 3))
                    for work_hash in work_hashes}

        self.assertEqual(original, repeated)
        counts = {rank: tuple(original.values()).count(rank)
                  for rank in (0, 1, 2)}
        self.assertTrue(all(150 <= count <= 250 for count in counts.values()))
        for work_hash, previous in original.items():
            if expanded[work_hash] != previous:
                self.assertEqual(expanded[work_hash], 3)
        with self.assertRaises(ValueError):
            owner("not-a-hash", (0, 1))

    def test_shared_work_coordinator_fences_global_content_ownership(self):
        runner = self.runner
        epoch = "e" * 64
        work_hash = hashlib.sha256(b"one shared seed").hexdigest()
        owner_rank = runner._rendezvous_work_owner(work_hash, (0, 1))
        other_rank = 1 - owner_rank
        with tempfile.TemporaryDirectory() as tmp:
            coordinators = {
                rank: runner._SharedWorkCoordinator(
                    tmp,
                    rank=rank,
                    master_ranks=(0, 1),
                    epoch=epoch,
                    shard_count=4,
                    lease_ttl=10.0,
                    lock_ttl=2.0,
                    lock_acquire_timeout=1.0,
                )
                for rank in (0, 1)
            }
            owner = coordinators[owner_rank]
            other = coordinators[other_rank]
            commit = runner._standalone_commit_manifest(2, "", (), 3)
            self.assertIsNotNone(commit)

            token = owner.claim_owned(work_hash, origin="initial")
            self.assertTrue(token)
            self.assertIsNone(other.claim_owned(work_hash, origin="initial"))
            self.assertIsNone(other.reclaim(work_hash, origin="initial"))
            self.assertFalse(other.begin_commit(work_hash, token, commit))
            self.assertTrue(owner.begin_commit(work_hash, token, commit))
            self.assertEqual(other.recover_committing(), (
                (work_hash, token, commit),
            ))
            self.assertEqual(
                other.finish_commit(work_hash, token), "completed")
            self.assertEqual(
                owner.finish_commit(work_hash, token), "already")
            self.assertNotIn(work_hash, owner.tokens)
            self.assertIsNone(owner.claim_owned(work_hash, origin="initial"))
            self.assertEqual(
                runner._durable_completed_work_statistics(coordinators[0]),
                {
                    "generated": 3,
                    "analyzed": 1,
                    "by_master": {owner_rank: 1},
                },
            )

            abandon_hash = next(
                candidate
                for index in range(1000)
                if owner.designated_owner(candidate := hashlib.sha256(
                    f"abandon-{index}".encode("ascii")).hexdigest()) ==
                owner_rank
            )
            abandoned_token = owner.claim_owned(
                abandon_hash, origin="generated")
            self.assertTrue(abandoned_token)
            self.assertFalse(owner.abandon(abandon_hash, "wrong-token"))
            self.assertTrue(owner.abandon(abandon_hash, abandoned_token))
            self.assertNotIn(abandon_hash, owner.tokens)
            self.assertTrue(owner.claim_owned(
                abandon_hash, origin="generated"))

    def test_shared_work_coordinator_probes_before_epoch_publication(self):
        runner = self.runner
        epoch = "e" * 64
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
                runner,
                "probe_shared_state_filesystem",
                side_effect=RuntimeError("incompatible shared filesystem"),
        ):
            with self.assertRaisesRegex(
                    RuntimeError, "incompatible shared filesystem"):
                runner._SharedWorkCoordinator(
                    tmp,
                    rank=0,
                    master_ranks=(0,),
                    epoch=epoch,
                    shard_count=4,
                    lease_ttl=10.0,
                    lock_ttl=2.0,
                    lock_acquire_timeout=1.0,
                    verify_filesystem=True,
                )

            self.assertFalse(os.path.exists(os.path.join(tmp, "state.json")))

    def test_shared_work_coordinator_reuses_exact_prequalified_capability(self):
        runner = self.runner
        epoch = "e" * 64
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "state")
            publication = os.path.join(tmp, "corpus")
            capability = runner.probe_shared_state_filesystem(
                state,
                timeout=2.0,
                publication_root=publication,
            )
            with mock.patch.object(
                    runner,
                    "probe_shared_state_filesystem",
                    side_effect=AssertionError("preflight must not be repeated")):
                coordinator = runner._SharedWorkCoordinator(
                    state,
                    rank=0,
                    master_ranks=(0,),
                    epoch=epoch,
                    shard_count=4,
                    lease_ttl=10.0,
                    lock_ttl=2.0,
                    lock_acquire_timeout=1.0,
                    verify_filesystem=True,
                    publication_root=publication,
                    filesystem_capabilities=capability,
                )

            self.assertIs(coordinator.filesystem_capabilities, capability)
            self.assertTrue(os.path.isfile(os.path.join(state, "state.json")))

            with self.assertRaisesRegex(ValueError, "do not satisfy"):
                runner._SharedWorkCoordinator(
                    os.path.join(tmp, "different-state"),
                    rank=0,
                    master_ranks=(0,),
                    epoch=epoch,
                    shard_count=4,
                    lease_ttl=10.0,
                    lock_ttl=2.0,
                    lock_acquire_timeout=1.0,
                    publication_root=publication,
                    filesystem_capabilities=capability,
                )

    def test_shared_work_coordinator_batches_heartbeats_and_tracks_failures(self):
        runner = self.runner
        epoch = "a" * 64
        work_ids = (
            "00000000" + "a" * 56,
            "00000004" + "b" * 56,
        )
        with tempfile.TemporaryDirectory() as tmp:
            coordinator = runner._SharedWorkCoordinator(
                tmp,
                rank=0,
                master_ranks=(0,),
                epoch=epoch,
                shard_count=4,
                lease_ttl=10.0,
                lock_ttl=2.0,
                lock_acquire_timeout=1.0,
            )
            for work_hash in work_ids:
                self.assertTrue(coordinator.claim_owned(
                    work_hash, origin="initial"))
            coordinator.tokens[work_ids[1]] = "stale-token"

            with mock.patch.object(
                    coordinator.table, "heartbeat",
                    side_effect=AssertionError("scalar heartbeat used")):
                self.assertEqual(coordinator.heartbeat_all(), (work_ids[1],))

            self.assertEqual(coordinator.heartbeat_batches, 1)
            self.assertEqual(coordinator.heartbeat_renewals, 1)
            self.assertEqual(coordinator.heartbeat_directory_syncs, 1)
            self.assertEqual(coordinator.heartbeat_failures, 0)
            self.assertEqual(set(coordinator.tokens), {work_ids[0]})

            with mock.patch.object(
                    coordinator.table, "heartbeat_many",
                    side_effect=OSError("heartbeat storage offline")):
                with self.assertRaisesRegex(
                        OSError, "heartbeat storage offline"):
                    coordinator.heartbeat_all()

            self.assertEqual(coordinator.heartbeat_batches, 1)
            self.assertEqual(coordinator.heartbeat_failures, 1)
            self.assertEqual(set(coordinator.tokens), {work_ids[0]})

            with mock.patch.object(
                    coordinator.table, "heartbeat_many",
                    side_effect=LookupError("heartbeat adapter failed")):
                with self.assertRaisesRegex(
                        LookupError, "heartbeat adapter failed"):
                    coordinator.heartbeat_all()

            self.assertEqual(coordinator.heartbeat_batches, 1)
            self.assertEqual(coordinator.heartbeat_failures, 2)
            self.assertEqual(set(coordinator.tokens), {work_ids[0]})

            malformed = runner.LeaseHeartbeatBatch(
                (work_ids[0],), (), "invalid-sync-count")
            with mock.patch.object(
                    coordinator.table, "heartbeat_many",
                    return_value=malformed):
                with self.assertRaisesRegex(
                        TypeError, "invalid work lease heartbeat batch"):
                    coordinator.heartbeat_all()

            self.assertEqual(coordinator.heartbeat_batches, 1)
            self.assertEqual(coordinator.heartbeat_renewals, 1)
            self.assertEqual(coordinator.heartbeat_directory_syncs, 1)
            self.assertEqual(coordinator.heartbeat_failures, 3)
            self.assertEqual(set(coordinator.tokens), {work_ids[0]})

    def test_shared_work_coordinator_force_recovers_future_lease_once(self):
        runner = self.runner
        epoch = "f" * 64
        work_hash = hashlib.sha256(b"recover me").hexdigest()
        payload = {
            "schema": runner._SharedWorkCoordinator._SCHEMA,
            "hash": work_hash,
            "origin": "generated",
        }
        with tempfile.TemporaryDirectory() as tmp:
            first = runner._SharedWorkCoordinator(
                tmp,
                rank=0,
                master_ranks=(0, 1),
                epoch=epoch,
                shard_count=2,
                lease_ttl=1.0,
                lock_ttl=2.0,
                lock_acquire_timeout=1.0,
            )
            second = runner._SharedWorkCoordinator(
                tmp,
                rank=1,
                master_ranks=(0, 1),
                epoch=epoch,
                shard_count=2,
                lease_ttl=1.0,
                lock_ttl=2.0,
                lock_acquire_timeout=1.0,
            )
            future = time.time() + 3600.0
            stale = first.table.claim(
                work_hash, payload, owner=first.owner, now=future)
            self.assertTrue(stale)

            self.assertEqual(second.recover_expired(), ())
            self.assertEqual(
                second.recover_expired(lease_ttl=0.0),
                ((work_hash, "generated"),),
            )
            token = second.tokens[work_hash]
            commit = runner._standalone_commit_manifest(3, "", (), 0)
            self.assertIsNotNone(commit)
            self.assertEqual(first.recover_expired(), ())
            self.assertFalse(first.table.complete(work_hash, stale or ""))
            self.assertTrue(second.begin_commit(work_hash, token, commit))
            self.assertTrue(second.complete(work_hash, token))

    def test_shared_work_recovery_fails_closed_on_invalid_persisted_state(self):
        runner = self.runner
        epoch = "d" * 64
        record_hash = hashlib.sha256(b"authoritative-record").hexdigest()
        redirected_hash = hashlib.sha256(b"redirected-payload").hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            first = runner._SharedWorkCoordinator(
                tmp,
                rank=0,
                master_ranks=(0, 1),
                epoch=epoch,
                shard_count=2,
                lease_ttl=1.0,
                lock_ttl=2.0,
                lock_acquire_timeout=1.0,
            )
            second = runner._SharedWorkCoordinator(
                tmp,
                rank=1,
                master_ranks=(0, 1),
                epoch=epoch,
                shard_count=2,
                lease_ttl=1.0,
                lock_ttl=2.0,
                lock_acquire_timeout=1.0,
            )
            token = first.table.claim(record_hash, {
                "schema": runner._SharedWorkCoordinator._SCHEMA,
                "hash": redirected_hash,
                "origin": "generated",
            }, owner=first.owner, now=1.0)
            self.assertTrue(token)

            with self.assertRaisesRegex(
                    ValueError, "invalid standalone leased payload"):
                second.recover_expired()
            self.assertEqual(second.tokens, {})

            commit_hash = hashlib.sha256(
                b"malformed commit manifest").hexdigest()
            commit_payload = {
                "schema": runner._SharedWorkCoordinator._SCHEMA,
                "hash": commit_hash,
                "origin": "generated",
            }
            commit_token = first.table.claim(
                commit_hash,
                commit_payload,
                owner=first.owner,
                now=1.0,
            )
            self.assertTrue(first.table.begin_commit(
                commit_hash,
                commit_token or "",
                commit={"schema": "not-canonical"},
                now=2.0,
            ))
            with self.assertRaisesRegex(
                    ValueError, "invalid standalone work payload"):
                second.recover_committing()

            first.table._write_record(record_hash, {
                "schema": 1,
                "id": record_hash,
                "status": "done",
                "payload": {
                    "schema": runner._SharedWorkCoordinator._SCHEMA,
                    "hash": record_hash,
                    "origin": "generated",
                },
                "owner": first.owner,
                "token": token,
                "updated": 3.0,
            })
            with self.assertRaisesRegex(
                    ValueError, "invalid standalone commit manifest"):
                second.recover_committing()

    def test_master_quiescence_requires_stable_status_vote_and_commit_ack(self):
        runner = self.runner
        gate = runner._MasterQuiescenceGate((1, 2))
        for peer in (1, 2):
            status = runner._master_status_payload(peer, 1, True)
            self.assertEqual(
                gate.observe_status(peer, status, now=10.0), "current")

        self.assertFalse(gate.should_probe(
            True, now=10.0, idle_window=0.5, freshness=2.0))
        self.assertTrue(gate.should_probe(
            True, now=10.5, idle_window=0.5, freshness=2.0))
        probe = gate.begin_probe()
        token = probe["probe_token"]
        for peer in (1, 2):
            self.assertEqual(gate.observe_reply(peer, {
                "schema": "symcc-master-quiescence-reply-v1",
                "rank": peer,
                "probe_token": token,
                "idle": True,
            }), "current")
        self.assertTrue(gate.ready_to_commit(
            True, now=10.6, freshness=2.0))
        commit = gate.commit_message()
        self.assertEqual(commit["probe_token"], token)
        self.assertEqual(gate.observe_status(
            1, runner._master_status_payload(1, 2, False), now=10.7),
            "commit-conflict",
        )
        self.assertEqual(gate.probe_token, token)
        self.assertFalse(gate.should_probe(
            True, now=100.0, idle_window=0.0, freshness=0.1))
        self.assertFalse(gate.commit_acknowledged())

        for peer in (1, 2):
            self.assertEqual(gate.observe_commit_ack(peer, {
                "schema": "symcc-master-quiescence-ack-v1",
                "rank": peer,
                "probe_token": token,
                "committed": True,
            }), "current")
        self.assertTrue(gate.commit_acknowledged())

    def test_busy_quiescence_vote_emits_exact_abort(self):
        runner = self.runner
        gate = runner._MasterQuiescenceGate((1,))
        status = runner._master_status_payload(1, 1, True)
        self.assertEqual(gate.observe_status(1, status, now=1.0), "current")
        self.assertTrue(gate.should_probe(
            True, now=1.0, idle_window=0.0, freshness=1.0))
        token = gate.begin_probe()["probe_token"]
        self.assertEqual(gate.observe_reply(1, {
            "schema": "symcc-master-quiescence-reply-v1",
            "rank": 1,
            "probe_token": token,
            "idle": False,
        }), "busy")
        self.assertEqual(gate.take_abort_message(), {
            "schema": "symcc-master-quiescence-abort-v1",
            "probe_token": token,
        })
        self.assertIsNone(gate.take_abort_message())

    def test_shutdown_consumes_late_result_before_exact_ack(self):
        lifecycle = self.lifecycle

        class Request:
            def Test(self):
                return True

        class Comm:
            def __init__(self):
                self.queues = {
                    (1, lifecycle.TAG_RESULT): [{"num_generated": 3}],
                    (1, lifecycle.TAG_READY): [{"rank": 1}],
                }

            def iprobe(self, *, source, tag):
                return bool(self.queues.get((source, tag)))

            def recv(self, *, source, tag):
                return self.queues[(source, tag)].pop(0)

            def isend(self, message, *, dest, tag):
                self.queues.setdefault((dest, lifecycle.TAG_STOP_ACK), []).append({
                    "schema": "symcc-shutdown-ack-v1",
                    "rank": dest,
                    "shutdown_token": message["shutdown_token"],
                })
                return Request()

        observed = []
        outcome = lifecycle._cooperative_shutdown_workers(
            Comm(),
            (1,),
            grace=1.0,
            result_callback=lambda worker, message: (
                observed.append((worker, message)) or True
            ),
        )

        self.assertTrue(outcome["clean"])
        self.assertEqual(outcome["drained_results"], 1)
        self.assertEqual(outcome["result_errors"], ())
        self.assertEqual(observed, [(1, {"num_generated": 3})])

    def test_shutdown_communication_error_is_not_clean(self):
        lifecycle = self.lifecycle

        class Request:
            def Test(self):
                return True

        class Comm:
            def __init__(self):
                self.failed_once = False
                self.queues = {}

            def iprobe(self, *, source, tag):
                if not self.failed_once:
                    self.failed_once = True
                    raise RuntimeError("transient MPI failure")
                return bool(self.queues.get((source, tag)))

            def recv(self, *, source, tag):
                return self.queues[(source, tag)].pop(0)

            def isend(self, message, *, dest, tag):
                self.queues.setdefault((dest, lifecycle.TAG_STOP_ACK), []).append({
                    "schema": "symcc-shutdown-ack-v1",
                    "rank": dest,
                    "shutdown_token": message["shutdown_token"],
                })
                return Request()

        outcome = lifecycle._cooperative_shutdown_workers(
            Comm(),
            (1,),
            initial_ready=(1,),
            grace=1.0,
        )

        self.assertFalse(outcome["clean"])
        self.assertEqual(outcome["acknowledged"], (1,))
        self.assertEqual(outcome["pending"], ())
        self.assertEqual(outcome["communication_errors"], (1,))

    def test_root_stats_exchange_aggregates_and_exactly_acknowledges(self):
        runner = self.runner

        class Request:
            def Test(self):
                return True

        class Comm:
            def __init__(self):
                self.queues = {
                    (1, runner.TAG_MASTER_STATS): [
                        runner._master_stats_payload(4, 2, 3, "a" * 64)
                    ],
                    (2, runner.TAG_MASTER_STATS): [
                        runner._master_stats_payload(6, 1, 5, "b" * 64)
                    ],
                }
                self.sent = []

            def iprobe(self, *, source, tag):
                return bool(self.queues.get((source, tag)))

            def recv(self, *, source, tag):
                return self.queues[(source, tag)].pop(0)

            def isend(self, message, *, dest, tag):
                self.sent.append((dest, tag, message))
                return Request()

        pending = [Request()]
        outcome = runner._bounded_master_stats_exchange(
            Comm(),
            rank=0,
            is_root=True,
            peer_masters=(1, 2),
            generated=10,
            interesting=3,
            analyzed=7,
            pending_sends=pending,
            timeout=1.0,
        )

        self.assertTrue(outcome["clean"])
        self.assertEqual(outcome["received"], (1, 2))
        self.assertEqual(outcome["pending"], ())
        self.assertEqual(outcome["generated"], 20)
        self.assertEqual(outcome["interesting"], 6)
        self.assertEqual(outcome["analyzed"], 15)
        self.assertEqual(
            {rank: values["analyzed"]
             for rank, values in outcome["by_master"].items()},
            {0: 7, 1: 3, 2: 5},
        )
        self.assertEqual(pending, [])

    def test_stats_exchange_is_fail_closed_and_bounded(self):
        runner = self.runner

        class Request:
            def Test(self):
                return True

        class Comm:
            def __init__(self):
                valid = runner._master_stats_payload(4, 2, 3, "c" * 64)
                self.queues = {
                    (1, runner.TAG_MASTER_STATS): [valid],
                    (2, runner.TAG_MASTER_STATS): [{
                        "schema": "symcc-master-stats-v1",
                        "stats_token": "d" * 64,
                        "generated": True,
                        "interesting": 1,
                        "analyzed": 1,
                    }],
                }

            def iprobe(self, *, source, tag):
                return bool(self.queues.get((source, tag)))

            def recv(self, *, source, tag):
                return self.queues[(source, tag)].pop(0)

            def isend(self, message, *, dest, tag):
                return Request()

        clock = [0.0]

        def monotonic():
            return clock[0]

        def sleep(delay):
            clock[0] += delay

        outcome = runner._bounded_master_stats_exchange(
            Comm(),
            rank=0,
            is_root=True,
            peer_masters=(1, 2),
            generated=1,
            interesting=1,
            analyzed=1,
            pending_sends=[],
            timeout=0.025,
            monotonic=monotonic,
            sleep=sleep,
        )

        self.assertFalse(outcome["clean"])
        self.assertEqual(outcome["received"], (1,))
        self.assertEqual(outcome["pending"], (2,))
        self.assertEqual(outcome["quarantined"], 1)
        self.assertAlmostEqual(outcome["elapsed"], 0.025)

    def test_submaster_waits_for_exact_root_ack(self):
        runner = self.runner

        class Request:
            def Test(self):
                return True

        class Comm:
            def __init__(self):
                self.queues = {
                    (2, runner.TAG_MASTER_STATUS): [
                        runner._master_status_payload(2, 1, True)
                    ],
                }

            def iprobe(self, *, source, tag):
                return bool(self.queues.get((source, tag)))

            def recv(self, *, source, tag):
                return self.queues[(source, tag)].pop(0)

            def isend(self, message, *, dest, tag):
                self.assertions(message, dest, tag)
                return Request()

            def assertions(self, message, dest, tag):
                if tag != runner.TAG_MASTER_STATS:
                    return
                assert dest == 0
                self.queues.setdefault(
                    (0, runner.TAG_MASTER_STATS_ACK), []).extend([
                        {
                            "schema": "symcc-master-stats-ack-v1",
                            "rank": 1,
                            "stats_token": "e" * 64,
                        },
                        {
                            "schema": "symcc-master-stats-ack-v1",
                            "rank": 1,
                            "stats_token": message["stats_token"],
                        },
                    ])

        comm = Comm()
        outcome = runner._bounded_master_stats_exchange(
            comm,
            rank=1,
            is_root=False,
            peer_masters=(0, 2),
            generated=9,
            interesting=4,
            analyzed=7,
            pending_sends=[],
            timeout=1.0,
        )

        self.assertTrue(outcome["clean"])
        self.assertEqual(outcome["quarantined"], 1)
        self.assertEqual(outcome["pending"], ())
        self.assertEqual(
            comm.queues[(2, runner.TAG_MASTER_STATUS)], [])


if __name__ == "__main__":
    unittest.main()
