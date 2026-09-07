# RUN: python3 %s

import importlib
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "util"))


class AflProfileOrchestrationTests(unittest.TestCase):
    def test_afl_artifact_ids_remain_correct_beyond_six_digits(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        self.assertEqual(helper._afl_artifact_id("id:999999,orig:seed"), 999999)
        self.assertEqual(
            helper._afl_artifact_id("id:1000000,src:999999"),
            1000000,
        )
        self.assertEqual(
            helper._afl_source_id("id:1000000,orig:seed"),
            "1000000",
        )
        self.assertIsNone(helper._afl_artifact_id("id:4294967296,orig:seed"))
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "id:999999,orig:a").write_bytes(b"a")
            Path(tmp, "id:1000000,orig:b").write_bytes(b"b")
            Path(tmp, "not-a-queue-entry").write_bytes(b"c")
            self.assertEqual(helper._next_afl_artifact_id(tmp), 1000001)

    def test_target_group_excludes_skips_and_preserves_primary_order(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        self.assertEqual(
            helper._target_group(
                77,
                ((88, "sample"), (77, "solve"), (99, "skip")),
            ),
            (77, 88),
        )
        self.assertEqual(
            helper._target_group(0, ((88, "solve"), (99, "skip"))),
            (88,),
        )
        for invalid in (
            True,
            float("nan"),
            float("inf"),
            -1,
            1 << 64,
            object(),
        ):
            with self.subTest(invalid=invalid):
                self.assertEqual(helper._normalize_branch_id(invalid), 0)
        self.assertEqual(helper._normalize_branch_id("77"), 77)
        self.assertEqual(
            helper._normalize_s2f_actions(((float("inf"), "solve"), (88, "sample"))),
            ((88, "sample"),),
        )
        self.assertEqual(
            helper._work_item_parts(("seed", None, float("inf"))),
            ("seed", None, 0, (), (), None),
        )
        self.assertEqual(
            helper._work_item_from_lease_payload(
                {"path": "seed", "target_branch": float("inf")}
            ),
            ("seed", None, 0),
        )

    def test_target_contract_cannot_be_rewritten_by_later_hint(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        message = {
            "target_branch": 999,
            "s2f_actions": ((999, "solve"), (1000, "sample")),
            "strategy": 6,
        }
        actions = ((77, "solve"), (88, "sample"), (99, "skip"))

        group = helper._enforce_target_contract(message, 77, actions)

        self.assertEqual(group, (77, 88))
        self.assertEqual(message["target_branch"], 77)
        self.assertEqual(message["s2f_actions"], actions)
        self.assertEqual(message["strategy"], 6)

    def test_shared_lease_heartbeat_tracks_the_shortest_ttl(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        self.assertAlmostEqual(helper._lease_heartbeat_interval(120.0, 30.0), 10.0)
        self.assertAlmostEqual(helper._lease_heartbeat_interval(1.0), 1.0 / 3.0)
        self.assertEqual(helper._lease_heartbeat_interval(300.0, 600.0), 30.0)

    def test_shared_heartbeat_isolates_storage_failures_per_lease(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        work_table = mock.Mock()
        work_table.heartbeat.side_effect = [OSError("offline"), True]
        target_table = mock.Mock()
        target_table.heartbeat_group.side_effect = [OSError("offline"), False, True]

        stats = helper._heartbeat_fenced_leases(
            work_table,
            {1: "work-a", 2: "work-b"},
            {1: "fence-a", 2: "fence-b"},
            target_table,
            {1: ((11, 22), "target-a")},
            {(33,): "target-b", (44,): "target-c"},
        )

        self.assertEqual(
            stats,
            {
                "work_ok": 1,
                "work_failed": 1,
                "target_ok": 1,
                "target_failed": 2,
            },
        )
        self.assertEqual(work_table.heartbeat.call_count, 2)
        self.assertEqual(target_table.heartbeat_group.call_count, 3)

    def test_shared_heartbeat_uses_one_work_lease_batch(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")

        class BatchWorkTable:
            def __init__(self):
                self.calls = []

            def heartbeat_many(self, leases):
                self.calls.append(dict(leases))
                return type(
                    "Result",
                    (),
                    {
                        "renewed": ("work-a",),
                        "lost": ("work-b",),
                    },
                )()

        work_table = BatchWorkTable()
        stats = helper._heartbeat_fenced_leases(
            work_table,
            {1: "work-a", 2: "work-b"},
            {1: "fence-a", 2: "fence-b"},
            None,
            {},
            {},
        )

        self.assertEqual(
            work_table.calls,
            [
                {
                    "work-a": "fence-a",
                    "work-b": "fence-b",
                }
            ],
        )
        self.assertEqual(
            stats,
            {
                "work_ok": 1,
                "work_failed": 1,
                "target_ok": 0,
                "target_failed": 0,
            },
        )

    def test_dispatch_transaction_uses_phase_specific_reverse_rollback(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        events = []
        transaction = helper._DispatchReservationTransaction(7, "1" * 64)
        transaction.defer(
            "first",
            lambda: events.append("first-unsent"),
            lambda: events.append("first-executed"),
        )
        transaction.defer(
            "broken",
            lambda: (_ for _ in ()).throw(OSError("cleanup failed")),
        )
        transaction.defer(
            "last",
            lambda: events.append("last-unsent"),
            lambda: events.append("last-executed"),
        )
        transaction.mark_dispatched()

        result = transaction.rollback()

        self.assertEqual(events, ["last-executed", "first-executed"])
        self.assertEqual(result["attempted"], 3)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["failures"], ["broken"])
        self.assertEqual(transaction.rollback()["attempted"], 0)

        unsent = helper._DispatchReservationTransaction(8, "2" * 64)
        unsent.defer(
            "phase",
            lambda: events.append("phase-unsent"),
            lambda: events.append("phase-executed"),
        )
        self.assertEqual(unsent.rollback()["failed"], 0)
        self.assertEqual(events[-1], "phase-unsent")

        committed = helper._DispatchReservationTransaction(9, "3" * 64)
        committed.defer("ignored", lambda: events.append("unexpected"))
        committed.mark_dispatched()
        committed.commit()
        self.assertEqual(committed.rollback()["attempted"], 0)
        self.assertNotIn("unexpected", events)

    def test_dispatch_registry_rollback_handles_invariant_violation(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        events = []
        preparing = {}
        active = {}
        committing = {}
        first = helper._DispatchReservationTransaction(7, "4" * 64)
        first.defer("preparing", lambda: events.append("preparing"))
        second = helper._DispatchReservationTransaction(7, "5" * 64)
        second.defer("active", lambda: events.append("active"))
        second.mark_dispatched()
        preparing[7] = first
        active[7] = second

        result = helper._rollback_dispatch_registries(7, preparing, active, committing)

        self.assertEqual(events, ["active", "preparing"])
        self.assertEqual(result["transactions"], 2)
        self.assertEqual(result["attempted"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertEqual((preparing, active, committing), ({}, {}, {}))

    def test_dispatch_tokens_fence_reused_worker_generations(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        epoch = "a" * 64
        first = helper._make_dispatch_token(epoch, 7, 1)
        second = helper._make_dispatch_token(epoch, 7, 2)
        other_worker = helper._make_dispatch_token(epoch, 8, 1)

        self.assertEqual(len(first), 64)
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, other_worker)
        self.assertEqual(
            helper._dispatch_result_status(first, {"dispatch_token": first}),
            "current",
        )
        self.assertEqual(
            helper._dispatch_result_status(second, {"dispatch_token": first}),
            "stale",
        )

    def test_dispatch_result_validation_is_fail_closed(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        current = helper._make_dispatch_token("b" * 64, 3, 9)

        self.assertEqual(
            helper._dispatch_result_status("", {"dispatch_token": current}),
            "unowned",
        )
        self.assertEqual(helper._dispatch_result_status(current, None), "malformed")
        self.assertEqual(helper._dispatch_result_status(current, {}), "missing")
        self.assertEqual(
            helper._dispatch_result_status(current, {"dispatch_token": "not-a-token"}),
            "malformed",
        )
        self.assertEqual(
            helper._dispatch_result_status(current, {"dispatch_token": [current]}),
            "malformed",
        )
        with self.assertRaisesRegex(ValueError, "coordinates"):
            helper._make_dispatch_token("bad", 3, 9)
        with self.assertRaisesRegex(ValueError, "transaction token"):
            helper._DispatchReservationTransaction(3, "")

    def test_worker_result_sender_overwrites_reported_dispatch_identity(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        current = helper._make_dispatch_token("c" * 64, 4, 2)
        comm = mock.Mock()

        returned = helper._send_dispatch_result(
            comm,
            {"new_tests": [], "dispatch_token": "d" * 64},
            current,
        )

        self.assertEqual(returned, current)
        comm.send.assert_called_once_with(
            {"new_tests": [], "dispatch_token": current},
            dest=0,
            tag=helper.TAG_RESULT,
        )
        with self.assertRaisesRegex(RuntimeError, "invalid dispatch token"):
            helper._send_dispatch_result(comm, {}, "")

    def test_master_admits_worker_result_before_consuming_dispatch_state(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        valid = {
            "new_tests": [
                {
                    "content": b"A",
                    "bitmap": [(7, 1)],
                    "hints": [(0, 0, 65)],
                }
            ],
            "total_generated": 1,
            "retcode": 0,
            "elapsed": 0.25,
            "killed": False,
            "proposal_content": b"B",
            "proposal_bitmap": [(8, 2)],
            "strategy": 0,
            "executor": "exact",
            "engine": "symcc",
            "s2f_actions": [[9, "solve"]],
            "parameter_overrides": {"SYMCC_FAST_SOLVE": "1"},
            "schedule_prefix": [0, 1],
            "schedule_trace": "1 0 lock a\n",
            "timeout_sites": [11],
            "telemetry": helper.asdict(
                helper.SolverTelemetry.from_observation(
                    None,
                    engine="symcc",
                    input_bytes=1,
                    generated=1,
                    elapsed=0.25,
                    return_code=0,
                    killed=False,
                    solver_algorithm="z3",
                )
            ),
        }
        limits = {
            "max_objects": 2,
            "max_bytes": 4,
            "max_object_bytes": 2,
            "max_hints": 2,
            "max_timeout_sites": 2,
            "max_schedule_trace_bytes": 32,
        }
        self.assertIs(
            helper._validate_hybrid_worker_result(valid, **limits),
            valid,
        )
        terminal = {
            **valid,
            "new_tests": [{
                "content": b"A",
                "terminal_status": "crash",
                "terminal_detail": 11,
            }],
        }
        self.assertIs(
            helper._validate_hybrid_worker_result(terminal, **limits), terminal
        )

        invalid_results = (
            {**valid, "new_tests": "not-a-list"},
            {**valid, "new_tests": [{"content": b"AAA"}]},
            {**valid, "new_tests": [{"content": "A"}]},
            {
                **valid,
                "new_tests": [{"content": b"A", "bitmap": [(7, 1), (7, 2)]}],
            },
            {
                **valid,
                "new_tests": [{"content": b"A", "bitmap": ((7, 1),)}],
            },
            {
                **valid,
                "new_tests": [{"content": b"A", "bitmap": [(1 << 23, 1)]}],
            },
            {
                **valid,
                "new_tests": [{"content": b"A", "hints": [(0, 0, 256)]}],
            },
            {**valid, "total_generated": float("inf")},
            {**valid, "total_generated": 3},
            {**valid, "elapsed": float("nan")},
            {**valid, "killed": 1},
            {
                **valid,
                "new_tests": [{"content": b"A", "unexpected": 1}],
            },
            {
                **valid,
                "new_tests": [{
                    "content": b"A",
                    "bitmap": [(7, 1)],
                    "terminal_status": "crash",
                    "terminal_detail": 11,
                }],
            },
            {
                **valid,
                "new_tests": [{
                    "content": b"A",
                    "terminal_status": "timeout",
                    "terminal_detail": 256,
                }],
            },
            {**valid, "proposal_content": None},
            {**valid, "strategy": float("nan")},
            {**valid, "s2f_actions": [[9, "solve"], [9, "skip"]]},
            {**valid, "schedule_prefix": [True]},
            {**valid, "schedule_trace": "x" * 33},
            {**valid, "timeout_sites": [11, 11]},
            {
                **valid,
                "parameter_overrides": {"LD_PRELOAD": "/tmp/injected"},
            },
            {
                **valid,
                "telemetry": {**valid["telemetry"], "capabilities": ()},
            },
        )
        for invalid in invalid_results:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    helper._validate_hybrid_worker_result(invalid, **limits)

        aggregate_coverage_overflow = {
            **valid,
            "new_tests": [
                {"content": b"A", "bitmap": [(7, 1)]},
                {"content": b"B", "bitmap": [(8, 1)]},
            ],
            "total_generated": 2,
        }
        with mock.patch.object(helper.StreamingShowmap, "MAX_EDGES", 1):
            with self.assertRaisesRegex(ValueError, "aggregate coverage"):
                helper._validate_hybrid_worker_result(
                    aggregate_coverage_overflow, **limits
                )

            proposal_coverage_overflow = {
                **valid,
                "new_tests": [
                    {"content": b"A", "bitmap": [(7, 1)]},
                ],
                "proposal_content": b"B",
                "proposal_bitmap": [(8, 1)],
            }
            with self.assertRaisesRegex(ValueError, "aggregate coverage"):
                helper._validate_hybrid_worker_result(
                    proposal_coverage_overflow, **limits
                )

        overflow = {
            "new_tests": [],
            "total_generated": 3,
            "retcode": -1,
            "elapsed": 0.0,
            "killed": False,
            "result_budget_error": {
                "resource": "objects",
                "observed": 3,
                "limit": 2,
                "objects": 3,
            },
        }
        self.assertIs(
            helper._validate_hybrid_worker_result(overflow, **limits),
            overflow,
        )

        checkpoint = "e" * 64
        continuation = {
            "new_tests": [],
            "total_generated": 1,
            "retcode": 0,
            "elapsed": 0.1,
            "killed": False,
            "continuation_generated": 0,
            "continuation_id": "f" * 64,
            "continuation_frontier": [checkpoint],
        }
        self.assertIs(
            helper._validate_hybrid_worker_result(continuation, **limits),
            continuation,
        )
        with self.assertRaisesRegex(ValueError, "continuation count"):
            helper._validate_hybrid_worker_result(
                {**continuation, "total_generated": 0}, **limits
            )

        token = helper._make_dispatch_token("d" * 64, 4, 3)
        gate = helper._DispatchGenerationGate()
        self.assertEqual(
            gate.observe_result(4, token, {"dispatch_token": token}),
            "current",
        )
        self.assertEqual(gate.observe_result(4, token, {}), "missing")
        self.assertEqual(
            gate.observe_ready(
                4,
                token,
                {"completed_dispatch_token": token},
            ),
            "current",
        )
        self.assertTrue(gate.recoverable(4, token))

    def test_master_result_admission_service_is_bounded_and_ordered(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        limits = {
            "max_objects": 2,
            "max_bytes": 8,
            "max_object_bytes": 4,
            "max_hints": 2,
            "max_timeout_sites": 2,
            "max_schedule_trace_bytes": 32,
        }
        with self.assertRaisesRegex(ValueError, "capacity"):
            helper._HybridResultAdmissionService(
                max_workers=2, capacity=1, **limits
            )

        service = helper._HybridResultAdmissionService(
            max_workers=2, capacity=2, **limits
        )
        first_dispatch = object()
        second_dispatch = object()
        valid = {"new_tests": [], "total_generated": 0}
        invalid = {"new_tests": "invalid"}
        admitted = service.validate_many([
            (7, first_dispatch, valid),
            (3, second_dispatch, invalid),
        ])
        self.assertEqual([row[0] for row in admitted], [7, 3])
        self.assertIs(admitted[0][1], first_dispatch)
        self.assertIs(admitted[0][2], valid)
        self.assertIsNone(admitted[0][3])
        self.assertIs(admitted[1][1], second_dispatch)
        self.assertIsNone(admitted[1][2])
        self.assertIsInstance(admitted[1][3], ValueError)
        self.assertEqual(service.snapshot()["submitted"], 2)
        self.assertEqual(service.snapshot()["invalid"], 1)
        self.assertEqual(service.snapshot()["maximum_batch"], 2)
        admission_metrics = service.snapshot()
        self.assertEqual(
            admission_metrics["validation_seconds"],
            admission_metrics["validation_parallel_wall_seconds"],
        )
        self.assertGreaterEqual(
            admission_metrics["validation_service_seconds"],
            admission_metrics["validation_parallel_wall_seconds"],
        )
        self.assertGreaterEqual(
            admission_metrics["validation_response_seconds"],
            admission_metrics["validation_service_seconds"],
        )
        with self.assertRaisesRegex(ValueError, "exceeds capacity"):
            service.validate_many([
                (1, None, valid), (2, None, valid), (3, None, valid),
            ])
        service.close()
        service.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            service.validate_many([(1, None, valid)])

    def test_master_result_admission_pipeline_does_not_block_master_loop(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        limits = {
            "max_objects": 2,
            "max_bytes": 8,
            "max_object_bytes": 4,
            "max_hints": 2,
            "max_timeout_sites": 2,
            "max_schedule_trace_bytes": 32,
        }
        entered = threading.Event()
        release = threading.Event()
        original = helper._validate_hybrid_worker_result

        def delayed(result, **kwargs):
            entered.set()
            release.wait(timeout=2.0)
            return original(result, **kwargs)

        with mock.patch.object(
            helper, "_validate_hybrid_worker_result", side_effect=delayed,
        ):
            service = helper._HybridResultAdmissionService(
                max_workers=1, capacity=2, **limits
            )
            dispatch = object()
            service.submit_many([(
                7, dispatch, {"new_tests": [], "total_generated": 0},
            )])
            self.assertTrue(entered.wait(timeout=1.0))
            started = time.monotonic()
            self.assertEqual(service.collect_ready(), [])
            self.assertLess(time.monotonic() - started, 0.1)
            self.assertEqual(service.available_capacity, 1)
            release.set()
            deadline = time.monotonic() + 1.0
            while not service.has_ready() and time.monotonic() < deadline:
                time.sleep(0.001)
            admitted = service.collect_ready()
            self.assertEqual(len(admitted), 1)
            self.assertEqual(admitted[0][:2], (7, dispatch))
            self.assertIsNone(admitted[0][3])
            service.close()

    def test_master_result_admission_collects_ready_tail_without_hol_blocking(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        limits = {
            "max_objects": 2,
            "max_bytes": 8,
            "max_object_bytes": 4,
            "max_hints": 2,
            "max_timeout_sites": 2,
            "max_schedule_trace_bytes": 32,
        }
        slow = {"new_tests": [], "total_generated": 0}
        fast = {"new_tests": [], "total_generated": 0}
        slow_started = threading.Event()
        release_slow = threading.Event()
        original = helper._validate_hybrid_worker_result

        def delayed(result, **kwargs):
            if result is slow:
                slow_started.set()
                release_slow.wait(timeout=2.0)
            return original(result, **kwargs)

        with mock.patch.object(
            helper, "_validate_hybrid_worker_result", side_effect=delayed,
        ):
            service = helper._HybridResultAdmissionService(
                max_workers=2, capacity=2, **limits
            )
            slow_dispatch = object()
            fast_dispatch = object()
            service.submit_many([
                (1, slow_dispatch, slow),
                (2, fast_dispatch, fast),
            ])
            self.assertTrue(slow_started.wait(timeout=1.0))
            deadline = time.monotonic() + 1.0
            admitted = []
            while not admitted and time.monotonic() < deadline:
                admitted = service.collect_ready()
                if not admitted:
                    time.sleep(0.001)
            self.assertEqual(len(admitted), 1)
            self.assertEqual(admitted[0][:2], (2, fast_dispatch))
            self.assertEqual(service.available_capacity, 1)
            release_slow.set()
            remaining = service.collect_ready(wait=True)
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0][:2], (1, slow_dispatch))
            service.close()

    def test_master_result_receive_capacity_counts_quarantined_messages(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")

        class Status:
            source = -1

            def Get_source(self):
                return self.source

        class Comm:
            def __init__(self):
                self.messages = [
                    (rank, {"result_status": "stale"}) for rank in range(10)
                ]
                self.received = 0

            def iprobe(self, **_kwargs):
                return bool(self.messages)

            def recv(self, *, status, **_kwargs):
                rank, message = self.messages.pop(0)
                status.source = rank
                self.received += 1
                return message

        class Gate:
            @staticmethod
            def observe_result(_worker, _expected, message):
                return message["result_status"]

        comm = Comm()
        current, quarantined, received = helper._receive_hybrid_result_batch(
            comm,
            Gate(),
            {},
            set(),
            3,
            status_factory=Status,
        )
        self.assertEqual(current, [])
        self.assertEqual(received, 3)
        self.assertEqual(comm.received, 3)
        self.assertEqual(len(comm.messages), 7)
        self.assertEqual(quarantined, [(0, "stale"), (1, "stale"), (2, "stale")])
        with self.assertRaisesRegex(ValueError, "capacity"):
            helper._receive_hybrid_result_batch(
                comm, Gate(), {}, set(), 0, status_factory=Status
            )

    def test_timeout_site_artifact_is_bounded_and_canonical(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "timeout_sites")
            with open(path, "w", encoding="ascii") as stream:
                stream.write("11 0x16\n")
            self.assertEqual(
                helper._load_timeout_sites(path, max_sites=2, max_bytes=16),
                [11, 22],
            )

            for content, limits in (
                ("11 11\n", {"max_sites": 2, "max_bytes": 16}),
                ("11 nope\n", {"max_sites": 2, "max_bytes": 16}),
                ("11 22 33\n", {"max_sites": 2, "max_bytes": 16}),
                ("1" * 17, {"max_sites": 2, "max_bytes": 16}),
            ):
                with self.subTest(content=content):
                    with open(path, "w", encoding="ascii") as stream:
                        stream.write(content)
                    with self.assertRaises(ValueError):
                        helper._load_timeout_sites(path, **limits)

    def test_quarantined_result_stats_are_reasoned_and_logged(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        stats = helper.Stats()
        stats.quarantine_result("stale")
        stats.quarantine_result("stale")
        stats.quarantine_result("malformed")
        stream = io.StringIO()

        stats.log(stream)

        self.assertEqual(stats.quarantined_results, 3)
        self.assertEqual(
            stats.quarantined_result_reasons,
            {"stale": 2, "malformed": 1},
        )
        self.assertIn(
            "Quarantined worker results: 3 (malformed=1,stale=2)",
            stream.getvalue(),
        )

    def test_ready_generation_gate_joins_both_message_orders(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        current = helper._make_dispatch_token("e" * 64, 5, 3)
        stale = helper._make_dispatch_token("e" * 64, 5, 2)
        ready = {"completed_dispatch_token": current}

        gate = helper._DispatchGenerationGate()
        self.assertEqual(gate.observe_ready(5, current, ready), "current")
        self.assertEqual(gate.observe_result(5, current, {}), "missing")
        self.assertTrue(gate.recoverable(5, current))

        gate.retire(5)
        self.assertEqual(
            gate.observe_result(5, current, {"dispatch_token": "bad"}),
            "malformed",
        )
        self.assertEqual(gate.observe_ready(5, current, ready), "current")
        self.assertTrue(gate.recoverable(5, current))

        self.assertEqual(
            gate.observe_result(5, current, {"dispatch_token": current}),
            "current",
        )
        self.assertFalse(gate.recoverable(5, current))
        gate.retire(5)
        self.assertEqual(
            gate.observe_result(5, current, {"dispatch_token": stale}),
            "stale",
        )
        self.assertEqual(gate.observe_ready(5, current, ready), "current")
        self.assertFalse(gate.recoverable(5, current))

    def test_ready_generation_validation_rejects_stale_and_malformed(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        current = helper._make_dispatch_token("f" * 64, 6, 4)
        stale = helper._make_dispatch_token("f" * 64, 6, 3)

        self.assertEqual(helper._ready_generation_status("", {}), "idle")
        self.assertEqual(
            helper._ready_generation_status("", {"completed_dispatch_token": stale}),
            "idle",
        )
        self.assertEqual(helper._ready_generation_status(current, {}), "missing")
        self.assertEqual(
            helper._ready_generation_status(
                current, {"completed_dispatch_token": stale}
            ),
            "stale",
        )
        self.assertEqual(
            helper._ready_generation_status(
                current, {"completed_dispatch_token": [current]}
            ),
            "malformed",
        )
        self.assertEqual(helper._ready_generation_status(current, None), "malformed")
        self.assertEqual(helper._ready_bitmap_version(-1, 7), -1)
        self.assertEqual(helper._ready_bitmap_version("7", 7), 7)
        self.assertEqual(helper._ready_bitmap_version(8, 7), -1)
        self.assertEqual(helper._ready_bitmap_version(-2, 7), -1)
        self.assertEqual(helper._ready_bitmap_version(True, 7), -1)
        self.assertEqual(helper._ready_bitmap_version([], 7), -1)

    def test_owned_dispatch_rollback_is_generation_atomic(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        current = helper._make_dispatch_token("1" * 64, 7, 8)
        stale = helper._make_dispatch_token("1" * 64, 7, 7)
        events = []
        transaction = helper._DispatchReservationTransaction(7, current)
        transaction.defer(
            "resource",
            lambda: events.append("before"),
            lambda: events.append("after"),
        )
        transaction.mark_dispatched()
        transactions = {7: transaction}
        items = {7: ("seed", None, 0)}
        workers = {7: "seed"}
        leases = {7: "lease"}

        self.assertIsNone(
            helper._rollback_owned_dispatch(
                7, stale, transactions, items, workers, leases
            )
        )
        self.assertEqual(
            (set(transactions), set(items), set(workers), set(leases)),
            ({7}, {7}, {7}, {7}),
        )

        recovered = helper._rollback_owned_dispatch(
            7, current, transactions, items, workers, leases
        )

        self.assertIsNotNone(recovered)
        item, rollback = recovered
        self.assertEqual(item, ("seed", None, 0))
        self.assertEqual(events, ["after"])
        self.assertEqual(rollback["failed"], 0)
        self.assertEqual((transactions, items, workers, leases), ({}, {}, {}, {}))

    def test_dispatch_watchdog_expires_only_active_exact_generations(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        first_token = helper._make_dispatch_token("2" * 64, 2, 1)
        second_token = helper._make_dispatch_token("2" * 64, 3, 2)
        first = helper._DispatchReservationTransaction(2, first_token)
        second = helper._DispatchReservationTransaction(3, second_token)
        first.mark_dispatched(now=10.0)

        self.assertFalse(first.expired(now=39.999, timeout=30.0))
        self.assertTrue(first.expired(now=40.0, timeout=30.0))
        self.assertFalse(first.expired(now=100.0, timeout=0.0))
        self.assertEqual(
            helper._expired_dispatches({3: second, 2: first}, now=40.0, timeout=30.0),
            ((2, first_token),),
        )
        self.assertEqual(
            helper._expired_dispatches({2: first}, now=float("nan"), timeout=30.0),
            (),
        )
        self.assertEqual(
            helper._expired_dispatches({2: first}, now=40.0, timeout=float("inf")),
            (),
        )
        with self.assertRaisesRegex(ValueError, "timestamp must be finite"):
            second.mark_dispatched(now=float("inf"))
        first.rollback()
        self.assertFalse(first.expired(now=100.0, timeout=30.0))
        self.assertEqual(helper._dispatch_watchdog_timeout("0", 120), 0.0)
        self.assertEqual(helper._dispatch_watchdog_timeout("30", 120), 30.0)
        self.assertEqual(helper._dispatch_watchdog_timeout("-1", 120), 0.0)
        self.assertEqual(helper._dispatch_watchdog_timeout("1000000", 120), 86400.0)
        self.assertEqual(helper._dispatch_watchdog_timeout("nan", 120), 120.0)
        self.assertEqual(helper._dispatch_watchdog_timeout("invalid", 120), 120.0)

    def test_bounded_finite_float_rejects_nonfinite_lock_timeouts(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        parse = helper._bounded_finite_float
        options = {"default": 60.0, "minimum": 0.001, "maximum": 3600.0}

        self.assertEqual(parse("0.25", **options), 0.25)
        self.assertEqual(parse("0", **options), 0.001)
        self.assertEqual(parse("9999", **options), 3600.0)
        self.assertEqual(parse("nan", **options), 60.0)
        self.assertEqual(parse("inf", **options), 60.0)
        self.assertEqual(parse("invalid", **options), 60.0)

    def test_timed_out_rank_requires_exact_generation_ready_to_recover(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        retired = helper._make_dispatch_token("3" * 64, 8, 4)
        stale = helper._make_dispatch_token("3" * 64, 8, 3)
        gate = helper._RetiredDispatchGate()

        gate.park(8, retired)
        self.assertTrue(gate.is_parked(8))
        self.assertEqual(gate.observe_ready(8, {}), "missing")
        self.assertEqual(
            gate.observe_ready(8, {"completed_dispatch_token": stale}),
            "stale",
        )
        self.assertTrue(gate.is_parked(8))
        self.assertEqual(
            gate.observe_ready(8, {"completed_dispatch_token": retired}),
            "recovered",
        )
        self.assertFalse(gate.is_parked(8))
        self.assertEqual(gate.observe_ready(8, {}), "unowned")
        with self.assertRaisesRegex(ValueError, "retired dispatch token"):
            gate.park(8, "")

    def test_dispatch_recovery_retries_then_persists_without_loss(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        item = ("/tmp/seed", "1-3", 19, ((19, "solve"),), (2,))
        with tempfile.TemporaryDirectory() as tmpdir:
            journal = helper.WorkLeaseJournal(os.path.join(tmpdir, "deferred.jsonl"))
            queue = []
            attempts = {}

            first = helper._enqueue_dispatch_recovery(
                item, queue, 0, attempts, 1, journal, worker=4
            )
            second = helper._enqueue_dispatch_recovery(
                item, queue, 0, attempts, 1, journal, worker=5
            )

            self.assertEqual(first["disposition"], "requeued")
            self.assertEqual(second["disposition"], "deferred")
            self.assertEqual(queue, [item])
            self.assertEqual(first["recovery_id"], second["recovery_id"])
            self.assertIn(second["recovery_id"], journal.leases)

            failed_journal = mock.Mock()
            failed_journal.leases = {}
            failed_journal.lease.side_effect = OSError("disk full")
            fallback_queue = []
            fallback = helper._enqueue_dispatch_recovery(
                item,
                fallback_queue,
                0,
                {first["recovery_id"]: 1},
                1,
                failed_journal,
                worker=6,
            )
            self.assertEqual(fallback["disposition"], "requeued-unpersisted")
            self.assertEqual(fallback_queue, [item])

    def test_shutdown_gate_requires_ready_and_exact_ack(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        epoch = "4" * 64
        gate = helper._ShutdownGenerationGate((2, 3), epoch)

        self.assertIsNone(gate.stop_message(2))
        self.assertEqual(gate.observe_ready(9), "unowned")
        self.assertEqual(gate.observe_ready(2), "ready")
        stop = gate.stop_message(2)
        self.assertEqual(stop["schema"], "symcc-shutdown-v1")
        self.assertEqual(stop["rank"], 2)
        self.assertEqual(helper._shutdown_stop_token(stop, 2), stop["shutdown_token"])
        self.assertEqual(helper._shutdown_stop_token(stop, 3), "")
        self.assertIsNone(gate.stop_message(2))

        self.assertEqual(gate.observe_ack(3, {}), "unowned")
        self.assertEqual(gate.observe_ack(2, {}), "malformed")
        self.assertEqual(
            gate.observe_ack(2, {"schema": "symcc-shutdown-ack-v1"}),
            "missing",
        )
        self.assertEqual(
            gate.observe_ack(
                2,
                {
                    "schema": "symcc-shutdown-ack-v1",
                    "rank": 2,
                    "shutdown_token": "5" * 64,
                },
            ),
            "stale",
        )
        self.assertEqual(
            gate.observe_ack(
                2,
                {
                    "schema": "symcc-shutdown-ack-v1",
                    "rank": True,
                    "shutdown_token": stop["shutdown_token"],
                },
            ),
            "malformed",
        )
        self.assertEqual(
            gate.observe_ack(
                2,
                {
                    "schema": "symcc-shutdown-ack-v1",
                    "rank": 2,
                    "shutdown_token": stop["shutdown_token"],
                },
            ),
            "current",
        )
        self.assertEqual(gate.pending, (3,))

        comm = mock.Mock()
        helper._send_shutdown_ack(comm, 2, stop["shutdown_token"])
        comm.send.assert_called_once_with(
            {
                "schema": "symcc-shutdown-ack-v1",
                "rank": 2,
                "shutdown_token": stop["shutdown_token"],
            },
            dest=0,
            tag=helper.TAG_STOP_ACK,
        )
        with self.assertRaisesRegex(RuntimeError, "shutdown token"):
            helper._send_shutdown_ack(comm, 2, "")
        self.assertEqual(helper._shutdown_stop_token(None), "")
        self.assertEqual(
            helper._shutdown_stop_token(
                {"schema": "wrong", "shutdown_token": "5" * 64}
            ),
            "",
        )

    def test_cooperative_shutdown_is_bounded_and_reports_silent_rank(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")

        class Request:
            def Test(self):
                return True

        class Comm:
            def __init__(self):
                self.queues = {
                    (2, helper.TAG_READY): [{"rank": 2}],
                }
                self.sent = []

            def iprobe(self, *, source, tag):
                return bool(self.queues.get((source, tag)))

            def recv(self, *, source, tag):
                return self.queues[(source, tag)].pop(0)

            def isend(self, message, *, dest, tag):
                self.sent.append((dest, tag, message))
                self.queues.setdefault((dest, helper.TAG_STOP_ACK), []).append(
                    {
                        "schema": "symcc-shutdown-ack-v1",
                        "rank": dest,
                        "shutdown_token": message["shutdown_token"],
                    }
                )
                return Request()

        clock = [10.0]

        def monotonic():
            return clock[0]

        def sleep(delay):
            clock[0] += delay

        comm = Comm()
        outcome = helper._cooperative_shutdown_workers(
            comm,
            (1, 2, 3),
            initial_ready=(1, 99),
            grace=0.03,
            monotonic=monotonic,
            sleep=sleep,
        )

        self.assertFalse(outcome["clean"])
        self.assertEqual(outcome["acknowledged"], (1, 2))
        self.assertEqual(outcome["sent"], (1, 2))
        self.assertEqual(outcome["pending"], (3,))
        self.assertAlmostEqual(outcome["elapsed"], 0.03)
        self.assertEqual(
            [(dest, tag) for dest, tag, _message in comm.sent],
            [(1, helper.TAG_STOP), (2, helper.TAG_STOP)],
        )

    def test_bounded_mpi_barrier_never_waits_past_deadline(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")

        class Request:
            def __init__(self, completion):
                self.completion = list(completion)

            def Test(self):
                if self.completion:
                    return self.completion.pop(0)
                return False

        class Comm:
            def __init__(self, completion):
                self.request = Request(completion)

            def Ibarrier(self):
                return self.request

        def run(completion, timeout):
            clock = [0.0]

            def monotonic():
                return clock[0]

            def sleep(delay):
                clock[0] += delay

            result = helper._bounded_mpi_barrier(
                Comm(completion),
                timeout,
                monotonic=monotonic,
                sleep=sleep,
            )
            return result, clock[0]

        completed, completed_at = run((False, False, True), 1.0)
        timed_out, timed_out_at = run((False,), 0.025)

        self.assertTrue(completed)
        self.assertLess(completed_at, 1.0)
        self.assertFalse(timed_out)
        self.assertAlmostEqual(timed_out_at, 0.025)
        self.assertEqual(helper._bounded_mpi_timeout("0", 30), 0.0)
        self.assertEqual(helper._bounded_mpi_timeout("45", 30), 45.0)
        self.assertEqual(helper._bounded_mpi_timeout("-1", 30), 0.0)
        self.assertEqual(helper._bounded_mpi_timeout("10000", 30), 3600.0)
        self.assertEqual(helper._bounded_mpi_timeout("nan", 30), 30.0)
        self.assertEqual(helper._bounded_mpi_timeout("invalid", 30), 30.0)

    def test_recovery_payload_round_trips_without_transport_token(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        item = (
            "/tmp/seed",
            "2-5",
            77,
            ((77, "solve"), (88, "sample")),
            (3, 1, 4),
        )

        payload = helper._work_item_recovery_payload(item)
        restored = helper._work_item_from_lease_payload(payload)

        self.assertNotIn("dispatch_token", payload)
        self.assertEqual(
            helper._work_item_parts(restored),
            helper._work_item_parts(item),
        )
        self.assertEqual(
            helper.WorkLeaseJournal.work_id(payload),
            helper.WorkLeaseJournal.work_id(dict(reversed(list(payload.items())))),
        )
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
            "path_condition_root": "a" * 64,
            "target_branch": 77,
        }
        missing_path = "/definitely/missing/symcc-seed"
        continuation_payload = helper._work_item_recovery_payload(
            (
                missing_path,
                "2-5",
                77,
                (),
                (),
                descriptor,
            )
        )

        self.assertIsNone(
            helper._recoverable_work_item_from_lease_payload(
                {"path": missing_path, "target_branch": 77},
            )
        )
        recovered_continuation = helper._recoverable_work_item_from_lease_payload(
            continuation_payload
        )
        self.assertIsNotNone(recovered_continuation)
        self.assertIsNotNone(helper._work_item_parts(recovered_continuation)[5])

    def test_protocol_deferred_journal_survives_restart(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "protocol-deferred.jsonl")
            item = ("/tmp/seed", "4-7", 31, ((31, "solve"),), (2, 1))
            payload = helper._work_item_recovery_payload(item)
            recovery_id = helper.WorkLeaseJournal.work_id(payload)
            journal = helper.WorkLeaseJournal(path, lease_ttl=60.0)

            self.assertTrue(journal.lease(recovery_id, payload, worker=9, now=100.0))
            restarted = helper.WorkLeaseJournal(path, lease_ttl=60.0)
            recovered = restarted.recover_expired(lease_ttl=0.0, now=101.0)

            self.assertEqual(recovered, [payload])
            self.assertEqual(restarted.leases, {})
            self.assertEqual(
                helper._work_item_parts(
                    helper._work_item_from_lease_payload(recovered[0])
                ),
                helper._work_item_parts(item),
            )

    def test_ready_quarantine_and_protocol_recovery_stats_are_logged(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        stats = helper.Stats()
        stats.quarantine_ready("stale")
        stats.quarantine_ready("malformed")
        stats.requeued_dispatches = 2
        stats.deferred_dispatches = 1
        stats.watchdog_timeouts = 3
        stats.watchdog_worker_recoveries = 2
        stream = io.StringIO()

        stats.log(stream)

        logged = stream.getvalue()
        self.assertIn(
            "Quarantined worker ready messages: 2 (malformed=1,stale=1)",
            logged,
        )
        self.assertIn("Protocol-requeued dispatches: 2", logged)
        self.assertIn("Protocol-deferred dispatches: 1", logged)
        self.assertIn("Dispatch watchdog timeouts: 3", logged)
        self.assertIn("Dispatch watchdog worker recoveries: 2", logged)

    def test_master_state_task_identity_overrides_worker_report(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        active = "a" * 64
        reported = "b" * 64

        self.assertEqual(
            helper._authoritative_state_task(active, reported),
            (active, True),
        )
        self.assertEqual(
            helper._authoritative_state_task(active, active),
            (active, False),
        )
        self.assertEqual(
            helper._authoritative_state_task("", reported),
            (reported, False),
        )

    def test_solver_component_override_is_strict(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        self.assertEqual(helper._configured_solver_component("exact"), "exact")
        self.assertEqual(helper._configured_solver_component(" DIVERSE "), "diverse")
        self.assertEqual(helper._configured_solver_component("unsupported"), "learned")
        self.assertEqual(helper._configured_solver_component(None), "learned")

    def test_executable_profile_context_hashes_exact_command(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "profile-target"
            content = b"exact instrumented executable"
            target.write_bytes(content)
            target.chmod(0o755)
            expected = hashlib.sha256(content).hexdigest()

            self.assertEqual(helper._command_executable_sha256([str(target)]), expected)
            with mock.patch.dict(os.environ, {"PATH": tmp}):
                self.assertEqual(
                    helper._command_executable_sha256([target.name]), expected
                )
            self.assertEqual(helper._command_executable_sha256(["missing-target"]), "")
            self.assertEqual(helper._command_executable_sha256([]), "")

            first = json.loads(
                helper._self_config_program_key([str(target), "--mode", "a"])
            )
            second = json.loads(
                helper._self_config_program_key([str(target), "--mode", "b"])
            )
            self.assertEqual(first["schema"], "symcc-self-config-program-key-v1")
            self.assertEqual(first["executable_sha256"], expected)
            self.assertNotEqual(first["argv"], second["argv"])

    def test_streaming_showmap_restarts_and_retries_current_input(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.write_bytes(b"binary")

            def process(payload):
                proc = mock.Mock()
                proc.stdin = io.BytesIO()
                proc.stdout = io.BytesIO(payload)
                proc.wait.return_value = 0
                return proc

            dead = process(b"")
            response = (
                b"\x00\x00"
                + struct.pack("<I", 1)
                + struct.pack("<IB", 17, 3)
                + struct.pack("<I", 0)
                + struct.pack("<I", 0)
            )
            recovered = process(response)
            with mock.patch.object(
                helper.subprocess, "Popen", side_effect=[dead, recovered]
            ) as popen:
                oracle = helper.StreamingShowmap("/bin/afl-showmap", [str(target)])
                try:
                    self.assertEqual(oracle.input_mode, "stdin")
                    self.assertEqual(oracle.get_edges(b"input"), [(17, 3)])
                    self.assertEqual(oracle._restart_count, 1)
                    self.assertEqual(popen.call_count, 2)
                finally:
                    oracle.close()

            with self.assertRaisesRegex(ValueError, "requires a stdin"):
                helper.StreamingShowmap("/bin/afl-showmap", [str(target), "@@"])

    def test_streaming_showmap_exposes_crash_status_and_auxiliary_bytes(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.write_bytes(b"binary")
            raw_status = (11 << 8) | 2
            response = (
                struct.pack("<H", raw_status)
                + struct.pack("<I", 2)
                + struct.pack("<IB", 17, 3)
                + struct.pack("<IB", 91, 1)
                + struct.pack("<I", 3)
                + b"out"
                + struct.pack("<I", 3)
                + b"err"
            )
            process = mock.Mock()
            process.stdin = io.BytesIO()
            process.stdout = io.BytesIO(response)
            process.wait.return_value = 0
            with mock.patch.object(helper.subprocess, "Popen", return_value=process):
                oracle = helper.StreamingShowmap("/bin/afl-showmap", [str(target)])
                try:
                    result = oracle.get_result(b"input")
                    self.assertIsNotNone(result)
                    self.assertEqual(result.status, "crash")
                    self.assertEqual(result.status_detail, 11)
                    self.assertEqual(result.edges, ((17, 3), (91, 1)))
                    self.assertEqual(result.stdout, b"out")
                    self.assertEqual(result.stderr, b"err")
                finally:
                    oracle.close()

    def test_streaming_showmap_bounds_corrupt_auxiliary_lengths(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.write_bytes(b"binary")
            oversized = helper.StreamingShowmap.MAX_AUXILIARY_BYTES + 1
            response = (
                struct.pack("<H", 0)
                + struct.pack("<I", 0)
                + struct.pack("<I", oversized)
            )

            def process():
                candidate = mock.Mock()
                candidate.stdin = io.BytesIO()
                candidate.stdout = io.BytesIO(response)
                candidate.wait.return_value = 0
                return candidate

            with mock.patch.object(
                helper.subprocess,
                "Popen",
                side_effect=[process(), process(), process(), process()],
            ) as popen:
                oracle = helper.StreamingShowmap("/bin/afl-showmap", [str(target)])
                try:
                    self.assertIsNone(oracle.get_result(b"input"))
                    self.assertEqual(popen.call_count, 2)
                    self.assertEqual(oracle.restart_count, 1)
                    # A call that starts with a dead session may restart once,
                    # but it must not consume a second restart on failure.
                    self.assertIsNone(oracle.get_result(b"input"))
                    self.assertEqual(popen.call_count, 3)
                    self.assertEqual(oracle.restart_count, 2)
                finally:
                    oracle.close()

    def test_streaming_showmap_rejects_oversized_input_without_restart(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.write_bytes(b"binary")
            process = mock.Mock()
            process.stdin = io.BytesIO()
            process.stdout = io.BytesIO()
            process.wait.return_value = 0
            with mock.patch.object(
                helper.subprocess, "Popen", return_value=process
            ) as popen:
                oracle = helper.StreamingShowmap("/bin/afl-showmap", [str(target)])
                try:
                    content = b"x" * (oracle.MAX_INPUT_BYTES + 1)
                    self.assertIsNone(oracle.get_result(content))
                    self.assertEqual(popen.call_count, 1)
                    self.assertEqual(oracle.restart_count, 0)
                finally:
                    oracle.close()

    def test_streaming_showmap_rejects_invalid_sparse_edges(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        encoded_cases = (
            struct.pack("<IB", 17, 0),
            struct.pack("<IB", helper.StreamingShowmap.MAX_MAP_SIZE, 1),
            struct.pack("<IB", 17, 1) + struct.pack("<IB", 17, 2),
        )
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.write_bytes(b"binary")
            for encoded in encoded_cases:
                with self.subTest(encoded=encoded):
                    edge_count = len(encoded) // struct.calcsize("<IB")
                    response = (
                        struct.pack("<H", 0)
                        + struct.pack("<I", edge_count)
                        + encoded
                        + struct.pack("<I", 0)
                        + struct.pack("<I", 0)
                    )

                    def process():
                        candidate = mock.Mock()
                        candidate.stdin = io.BytesIO()
                        candidate.stdout = io.BytesIO(response)
                        candidate.wait.return_value = 0
                        return candidate

                    with mock.patch.object(
                        helper.subprocess,
                        "Popen",
                        side_effect=[process(), process()],
                    ):
                        oracle = helper.StreamingShowmap(
                            "/bin/afl-showmap", [str(target)]
                        )
                        try:
                            self.assertIsNone(oracle.get_result(b"input"))
                        finally:
                            oracle.close()

    def test_one_shot_showmap_isolated_fallback(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            observed = []

            def run(cmd, **kwargs):
                observed.append(kwargs["input"])
                output = Path(cmd[cmd.index("-o") + 1])
                output.write_text("17:3\n91:1\n", encoding="ascii")
                return mock.Mock(returncode=0)

            with mock.patch.object(helper.subprocess, "run", side_effect=run):
                edges = helper.one_shot_showmap_edges(
                    "/bin/afl-showmap", ["/bin/target"], b"candidate", tmp
                )

            self.assertEqual(edges, [(17, 3), (91, 1)])
            self.assertEqual(observed, [b"candidate"])

    def test_one_shot_showmap_rejects_abnormal_or_invalid_results(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        cases = ((2, "17:1\n"), (0, ""), (0, "17:0\n"))
        with tempfile.TemporaryDirectory() as tmp:
            for returncode, map_content in cases:
                with self.subTest(returncode=returncode, map_content=map_content):

                    def run(command, **_kwargs):
                        output = Path(command[command.index("-o") + 1])
                        output.write_text(map_content, encoding="ascii")
                        return mock.Mock(returncode=returncode)

                    with mock.patch.object(helper.subprocess, "run", side_effect=run):
                        self.assertIsNone(
                            helper.one_shot_showmap_edges(
                                "/bin/afl-showmap",
                                ["/bin/target"],
                                b"candidate",
                                tmp,
                            )
                        )

    def test_batch_showmap_uses_stdin_and_refuses_file_placeholder(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate = root / "candidate"
            candidate.write_bytes(b"payload")
            commands = []

            def run(command, **_kwargs):
                commands.append(command)
                mapdir = Path(command[command.index("-o") + 1])
                (mapdir / candidate.name).write_text("17:3\n", encoding="ascii")
                return mock.Mock(returncode=0)

            with mock.patch.object(helper.subprocess, "run", side_effect=run):
                stdin_result = helper.batch_showmap_edges(
                    "/bin/afl-showmap",
                    ["/bin/stdin-target"],
                    [str(candidate)],
                    tmp,
                    wall_timeout_seconds=0.25,
                )
                file_result = helper.batch_showmap_edges(
                    "/bin/afl-showmap",
                    ["/bin/file-target", "@@"],
                    [str(candidate)],
                    tmp,
                )

            self.assertEqual(stdin_result[str(candidate)], [(17, 3)])
            self.assertEqual(file_result, {})
            self.assertEqual(commands[0][-1], "/bin/stdin-target")
            self.assertEqual(len(commands), 1)

    def test_showmap_wall_budget_caps_external_process_timeout(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            candidate = Path(tmp) / "candidate"
            candidate.write_bytes(b"payload")
            observed = []

            def run(command, **kwargs):
                observed.append(kwargs["timeout"])
                output = Path(command[command.index("-o") + 1])
                if output.is_dir():
                    (output / candidate.name).write_text(
                        "17:1\n", encoding="ascii"
                    )
                else:
                    output.write_text("17:1\n", encoding="ascii")
                return mock.Mock(returncode=0)

            with mock.patch.object(helper.subprocess, "run", side_effect=run):
                helper.batch_showmap_edges(
                    "/bin/afl-showmap",
                    ["/bin/target"],
                    [str(candidate)],
                    tmp,
                    wall_timeout_seconds=0.25,
                )
                helper.one_shot_showmap_edges(
                    "/bin/afl-showmap",
                    ["/bin/target"],
                    b"payload",
                    tmp,
                    wall_timeout_seconds=0.5,
                )

            self.assertEqual(observed, [0.25, 0.5])

    def test_one_shot_showmap_stages_file_placeholder_input(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            observed = []

            def run(command, **kwargs):
                observed.append((command, kwargs))
                staged = Path(command[-1])
                self.assertEqual(staged.read_bytes(), b"candidate")
                output = Path(command[command.index("-o") + 1])
                output.write_text("17:3\n", encoding="ascii")
                return mock.Mock(returncode=0)

            with mock.patch.object(helper.subprocess, "run", side_effect=run):
                result = helper.one_shot_showmap_edges(
                    "/bin/afl-showmap",
                    ["/bin/file-target", "@@"],
                    b"candidate",
                    tmp,
                )

            self.assertEqual(result, [(17, 3)])
            self.assertIs(observed[0][1]["stdin"], helper.subprocess.DEVNULL)

    def test_batch_showmap_rejects_failed_or_invalid_batches(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            candidate = Path(tmp) / "candidate"
            candidate.write_bytes(b"payload")
            for returncode, map_content in ((2, "17:1\n"), (0, "17:0\n")):
                with self.subTest(returncode=returncode, map_content=map_content):

                    def run(command, **_kwargs):
                        mapdir = Path(command[command.index("-o") + 1])
                        (mapdir / candidate.name).write_text(
                            map_content, encoding="ascii"
                        )
                        return mock.Mock(returncode=returncode)

                    with mock.patch.object(helper.subprocess, "run", side_effect=run):
                        self.assertEqual(
                            helper.batch_showmap_edges(
                                "/bin/afl-showmap",
                                ["/bin/target"],
                                [str(candidate)],
                                tmp,
                            ),
                            {},
                        )

    def test_auto_profile_mode(self):
        bench = importlib.import_module("benchmark.run_benchmark")
        self.assertEqual(
            bench.resolve_afl_profile_mode("auto", True, True, False), "full"
        )
        self.assertEqual(
            bench.resolve_afl_profile_mode("auto", True, False, False), "basic"
        )
        self.assertEqual(
            bench.resolve_afl_profile_mode("auto", False, False, False), "off"
        )
        self.assertEqual(bench.resolve_afl_profile_mode("off", True, True, True), "off")

    def test_data_coverage_runtime_build_uses_thread_link_flags(self):
        bench = importlib.import_module("benchmark.run_benchmark")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            source = root / "util" / "afl_data_coverage_rt.c"
            source.parent.mkdir(parents=True)
            source.write_text("int data_coverage_runtime;\n", encoding="ascii")
            output = Path(tmp) / "output"
            output.mkdir()
            library = output / "libafl_data_coverage_rt.so"
            library.write_bytes(b"library")
            completed = mock.Mock(returncode=0)

            with (
                mock.patch.object(bench, "SYMCC_ROOT", root),
                mock.patch.dict(os.environ, {"CC": "/usr/bin/cc"}),
                mock.patch.object(
                    bench.subprocess, "run", return_value=completed
                ) as run,
            ):
                built = bench.build_afl_data_coverage_runtime(str(output))

            self.assertEqual(built, str(library))
            command = run.call_args.args[0]
            self.assertIn("-pthread", command)
            self.assertLess(command.index("-ldl"), command.index("-pthread"))

    def test_showmap_parser_distinguishes_edge_universe_from_map_capacity(self):
        bench = importlib.import_module("benchmark.run_benchmark")
        complete = bench.parse_afl_showmap_coverage(
            "A coverage of 37 edges were achieved out of 211 existing (17.54%)"
        )
        self.assertEqual(complete, {
            "edge_cov": 17.54,
            "edges_found": 37,
            "edges_total": 211,
            "measure_ok": True,
            "edge_count_ok": True,
            "coverage_denominator_kind": "existing_edges",
            "coverage_map_size": 0,
        })

        tuple_only = bench.parse_afl_showmap_coverage(
            "Captured 37 tuples (map size 65536, highest value 8, total values 91)"
        )
        self.assertEqual(tuple_only, {
            "edge_cov": 0.0,
            "edges_found": 37,
            "edges_total": 0,
            "measure_ok": False,
            "edge_count_ok": True,
            "coverage_denominator_kind": "unavailable",
            "coverage_map_size": 65536,
        })
        self.assertFalse(
            bench.parse_afl_showmap_coverage("forkserver failed")["edge_count_ok"]
        )

    def test_corpus_metrics_ignore_staging_and_deduplicate_instances(self):
        bench = importlib.import_module("benchmark.run_benchmark")
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first"
            second = Path(tmp) / "second"
            first.mkdir()
            second.mkdir()
            (first / "id:000000").write_bytes(b"shared")
            (first / ".symcc-publish-x.tmp").write_bytes(b"partial")
            (second / "id:000000").write_bytes(b"shared")
            (second / "id:000001").write_bytes(b"unique")

            self.assertEqual(bench.count_output_files(str(first)), 1)
            self.assertEqual(
                len(bench.get_unique_hashes_many([str(first), str(second)])),
                2,
            )

    def test_link_or_copy_tolerates_live_queue_race(self):
        bench = importlib.import_module("benchmark.run_benchmark")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / "combined"
            self.assertFalse(
                bench._link_or_copy(str(root / "already-rotated"), str(destination))
            )
            self.assertFalse(destination.exists())

    def test_afl_only_uses_equal_core_instances_and_execution_metrics(self):
        bench = importlib.import_module("benchmark.run_benchmark")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / "target"
            binary.write_bytes(b"binary")
            seed_dir = root / "seeds"
            seed_dir.mkdir()
            (seed_dir / "seed").write_bytes(b"seed")
            work = root / "work"
            work.mkdir()
            processes = []

            def popen(cmd, **_kwargs):
                marker = "-M" if "-M" in cmd else "-S"
                name = cmd[cmd.index(marker) + 1]
                instance = work / "afl_out" / name
                queue = instance / "queue"
                queue.mkdir(parents=True)
                (queue / "id:000000").write_bytes(b"shared")
                (queue / "id:000001").write_bytes(name.encode("ascii"))
                (queue / ".symcc-publish-live.tmp").write_bytes(b"partial")
                index = int(name[-2:])
                (instance / "fuzzer_stats").write_text(
                    f"execs_done : {index * 10}\n"
                    f"execs_per_sec : {index * 2}\n"
                    "corpus_count : 2\n"
                    "edges_found : 3\n"
                    "total_edges : 10\n"
                    "bitmap_cvg : 30.00%\n",
                    encoding="ascii",
                )
                proc = mock.Mock()
                proc.pid = 1000 + index
                proc.returncode = 0
                proc.poll.return_value = None
                proc.wait.return_value = 0
                processes.append(proc)
                return proc

            with (
                mock.patch.object(bench.subprocess, "Popen", side_effect=popen),
                mock.patch.object(bench.os, "killpg"),
            ):
                result = bench.run_afl_only(
                    str(binary),
                    "target",
                    str(seed_dir),
                    0,
                    str(work),
                    instances=2,
                    afl_profile_mode="off",
                )

            self.assertEqual(len(processes), 2)
            self.assertEqual(result["afl_instances"], 2)
            self.assertEqual(result["generated"], 30)
            self.assertEqual(result["generated_kind"], "afl-executions")
            self.assertEqual(result["afl_executions"], 30)
            self.assertEqual(result["afl_execs_done"], 30)
            self.assertEqual(result["afl_retained_files"], 4)
            self.assertEqual(result["unique"], 3)
            self.assertFalse(
                any(
                    "symcc-publish" in path.name
                    for path in Path(result["output_dir"]).iterdir()
                )
            )

    def test_public_afl_variant_discovery(self):
        bench = importlib.import_module("benchmark.run_benchmark")
        old_public_dir = bench.PUBLIC_DIR
        with tempfile.TemporaryDirectory() as tmp:
            public_dir = Path(tmp) / "public"
            bin_dir = public_dir / "bin"
            for suite in [
                "google-fts-afl",
                "google-fts-afl-laf",
                "google-fts-afl-ctx",
                "google-fts-afl-ngram4",
                "google-fts-afl-laf-ctx",
            ]:
                target_dir = bin_dir / suite
                target_dir.mkdir(parents=True, exist_ok=True)
                binary = target_dir / "png_read_fuzzer"
                binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                os.chmod(binary, 0o755)
            try:
                bench.PUBLIC_DIR = public_dir
                variants = bench.discover_public_afl_variants()
            finally:
                bench.PUBLIC_DIR = old_public_dir

        self.assertEqual(
            set(variants["gfts-png_read_fuzzer"]),
            {"default", "laf", "ctx", "ngram4", "laf_ctx"},
        )

    def test_nested_timeouts_respect_campaign_budget(self):
        bench = importlib.import_module("benchmark.run_benchmark")
        for budget in (1, 2, 10, 60, 300):
            wall, per_exec, idle = bench._mpi_timeout_budget(budget)
            self.assertGreaterEqual(wall, 1)
            self.assertLessEqual(wall, budget)
            self.assertLessEqual(per_exec, wall)
            self.assertLessEqual(idle, wall)
        self.assertEqual(bench._mpi_timeout_budget(2), (2, 1, 1))

    def test_research_metadata_and_auc_are_attached(self):
        bench = importlib.import_module("benchmark.run_benchmark")
        rows = [
            {
                "target": "parser",
                "mode": "mpi",
                "np": 4,
                "round": 1,
                "wall_time": 10.0,
                "generated": 1,
                "unique": 1,
                "num_workers": 3,
            }
        ]
        series = [
            {
                "target": "parser",
                "mode": "mpi",
                "np": 4,
                "round": 1,
                "timeseries": [
                    {"timestamp_sec": 0, "edges_found": 10},
                    {"timestamp_sec": 10, "edges_found": 30},
                ],
            }
        ]
        with mock.patch.dict(
            os.environ,
            {
                "SYMCC_EXPERIMENT_ID": "experiment",
                "SYMCC_RUN_ID": "run",
                "SYMCC_PAIR_ID": "pair",
                "SYMCC_RESEARCH_PHASE": "confirmatory",
                "SYMCC_RESEARCH_CONFIGURATION": "full",
                "SYMCC_RANDOM_SEED": "9",
                "SYMCC_CPU_BUDGET_SECONDS": "40",
                "SYMCC_CPU_CORES": "6",
            },
            clear=False,
        ):
            bench.annotate_research_results(rows, series)
        self.assertEqual(rows[0]["configuration"], "full")
        self.assertEqual(rows[0]["run_id"], "run")
        self.assertEqual(rows[0]["allocated_cpu_cores"], 6)
        self.assertEqual(rows[0]["coverage_auc"], 20)
        self.assertEqual(series[0]["run_id"], "run")

    def test_campaign_budget_exhaustion_is_not_a_failed_run(self):
        bench = importlib.import_module("benchmark.run_benchmark")
        outcome = bench.benchmark_outcome(
            {
                "timed_out": True,
                "retcode": -15,
            }
        )
        self.assertEqual(outcome["status"], "success")
        self.assertTrue(outcome["budget_exhausted"])
        self.assertEqual(
            bench.benchmark_matrix_exit_code([
                {"mode": "seed", "status": "success"},
                {"mode": "mpi", "status": "success"},
            ]),
            0,
        )
        self.assertEqual(
            bench.benchmark_matrix_exit_code([
                {"mode": "seed", "status": "success"},
                {"mode": "mpi", "status": "failed"},
            ]),
            3,
        )

    def test_protocol_cell_rejects_an_inner_benchmark_matrix(self):
        bench = importlib.import_module("benchmark.run_benchmark")
        rows = [
            {"target": "parser", "mode": "mpi", "np": 2, "round": 1},
            {"target": "parser", "mode": "mpi", "np": 4, "round": 1},
        ]
        with mock.patch.dict(
            os.environ,
            {
                "SYMCC_RUN_ID": "outer-run",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(ValueError, "exactly one"):
                bench.annotate_research_results(rows, [])

    def test_timeseries_stop_records_final_corpus(self):
        bench = importlib.import_module("benchmark.run_benchmark")
        stopped = threading.Event()
        stopped.set()
        samples = [
            {
                "measure_ok": True,
                "edge_cov": 1.0,
                "edges_found": 10,
                "edges_total": 100,
                "total_cases": 1,
            },
            {
                "measure_ok": True,
                "edge_cov": 2.0,
                "edges_found": 20,
                "edges_total": 100,
                "total_cases": 2,
            },
        ]
        with mock.patch.object(bench, "measure_coverage_afl", side_effect=samples):
            series = bench.measure_coverage_timeseries_afl(
                "afl-target", "corpus", interval=30, max_duration=60, stop_event=stopped
            )
        self.assertEqual(len(series), 2)
        self.assertEqual(series[-1]["edges_found"], 20)

        running = threading.Event()
        started = time.monotonic()
        with mock.patch.object(bench, "measure_coverage_afl", side_effect=samples):
            bounded = bench.measure_coverage_timeseries_afl(
                "afl-target",
                "corpus",
                interval=30,
                max_duration=0.01,
                stop_event=running,
            )
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(len(bounded), 2)

        completed = threading.Event()
        timer = threading.Timer(0.03, completed.set)
        timer.start()

        def replay_after_completion(*_args, **_kwargs):
            self.assertTrue(completed.is_set())
            return samples[0]

        try:
            with mock.patch.object(
                bench, "measure_coverage_afl", side_effect=replay_after_completion
            ):
                deferred = bench.measure_coverage_timeseries_afl(
                    "afl-target",
                    "corpus",
                    interval=30,
                    max_duration=0.01,
                    stop_event=completed,
                    wait_for_stop_before_replay=True,
                )
        finally:
            timer.cancel()
            timer.join(timeout=1.0)
            self.assertFalse(timer.is_alive())
        self.assertEqual(len(deferred), 2)

    def test_timeseries_join_rejects_a_still_running_benchmark(self):
        bench = importlib.import_module("benchmark.run_benchmark")

        class StuckThread:
            joined_with = None

            def join(self, timeout):
                self.joined_with = timeout

            @staticmethod
            def is_alive():
                return True

        thread = StuckThread()
        with self.assertRaisesRegex(TimeoutError, "cleanup budget"):
            bench._join_benchmark_thread(thread, 7.5)
        self.assertEqual(thread.joined_with, 7.5)

    def test_live_corpus_union_is_deduplicated_and_routed(self):
        bench = importlib.import_module("benchmark.run_benchmark")
        with tempfile.TemporaryDirectory() as tmp:
            seed_dir = Path(tmp) / "seeds"
            queue_dir = Path(tmp) / "queue"
            seed_dir.mkdir()
            queue_dir.mkdir()
            (seed_dir / "seed").write_bytes(b"same")
            (queue_dir / "duplicate").write_bytes(b"same")
            (queue_dir / "new").write_bytes(b"different")

            def inspect_snapshot(binary, corpus, **kwargs):
                self.assertEqual(binary, "afl-target")
                self.assertEqual(kwargs["extra_args"], ["-d"])
                contents = {path.read_bytes() for path in Path(corpus).iterdir()}
                self.assertEqual(contents, {b"same", b"different"})
                return {
                    "measure_ok": True,
                    "edge_cov": 2.0,
                    "edges_found": 2,
                    "edges_total": 100,
                    "total_cases": 2,
                }

            with mock.patch.object(
                bench, "measure_coverage_afl", side_effect=inspect_snapshot
            ):
                measured = bench.measure_coverage_corpora_afl(
                    "afl-target",
                    lambda: [seed_dir, queue_dir],
                    extra_args=["-d"],
                )
            self.assertEqual(measured["total_cases"], 2)

            output_dir = Path(tmp) / "serial_output"

            def runner(work_dir):
                Path(work_dir, "serial_output").mkdir()
                return {"output_dir": str(output_dir), "retcode": 0}

            samples = [{"timestamp_sec": 0.0, "edges_found": 1}]
            with mock.patch.object(
                bench, "measure_coverage_timeseries_afl", return_value=samples
            ) as sampler:
                result, series = bench.run_with_timeseries(
                    runner,
                    {"work_dir": tmp},
                    "afl-target",
                    corpus_source=lambda: [seed_dir, output_dir],
                    extra_args=["-d"],
                    timeout=1,
                )
            self.assertEqual(result["retcode"], 0)
            self.assertEqual(series, samples)
            self.assertEqual(sampler.call_args.kwargs["extra_args"], ["-d"])

    def test_hybrid_sync_configuration_and_foreign_queue_deduplication(self):
        bench = importlib.import_module("benchmark.run_benchmark")
        environment = {"AFL_NO_SYNC": "1"}
        self.assertEqual(bench._configure_hybrid_afl_sync(environment), 1)
        self.assertEqual(environment["AFL_SYNC_TIME"], "1")
        self.assertEqual(environment["AFL_FINAL_SYNC"], "1")
        self.assertNotIn("AFL_NO_SYNC", environment)

        environment = {"AFL_SYNC_TIME": "7"}
        self.assertEqual(bench._configure_hybrid_afl_sync(environment), 7)
        environment = {"AFL_SYNC_TIME": "invalid"}
        self.assertEqual(bench._configure_hybrid_afl_sync(environment), 1)

        with tempfile.TemporaryDirectory() as tmp:
            queue = Path(tmp) / "foreign"
            queue.mkdir()
            alias = Path(tmp) / "alias"
            alias.symlink_to(queue, target_is_directory=True)
            queues = bench._existing_foreign_queues(
                [
                    str(queue),
                    str(alias),
                    str(Path(tmp) / "missing"),
                    "",
                ]
            )
            self.assertEqual(queues, [str(queue.resolve())])

    def test_native_peer_cursor_and_import_attribution(self):
        bench = importlib.import_module("benchmark.run_benchmark")
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp) / "fuzzer01"
            synced = instance / ".synced"
            queue = instance / "queue"
            synced.mkdir(parents=True)
            queue.mkdir()
            (synced / "symcc01").write_bytes((18).to_bytes(4, byteorder=sys.byteorder))
            (queue / "id:000001,sync:symcc01,src:000007,+cov").write_bytes(b"accepted")
            (queue / "id:000002,sync:fuzzer02,src:000003,+cov").write_bytes(b"other")

            self.assertEqual(bench._read_afl_sync_cursor(str(instance), "symcc01"), 18)
            self.assertEqual(bench._count_afl_sync_imports(str(queue), "symcc01"), 1)
            (synced / "symcc01").write_bytes(b"bad")
            self.assertIsNone(bench._read_afl_sync_cursor(str(instance), "symcc01"))

    def test_peer_sync_drain_waits_for_frozen_tail_with_bounded_clock(self):
        bench = importlib.import_module("benchmark.run_benchmark")

        class LiveProcess:
            @staticmethod
            def poll():
                return None

        clock = [0.0]
        cursors = iter((None, 1, 3))

        def sleep(duration):
            clock[0] += duration

        cursor, complete, elapsed = bench._wait_for_afl_peer_sync(
            "/campaign/fuzzer01",
            "symcc01",
            3,
            LiveProcess(),
            1.0,
            cursor_reader=lambda _instance, _peer: next(cursors),
            monotonic=lambda: clock[0],
            sleep=sleep,
        )

        self.assertEqual(cursor, 3)
        self.assertTrue(complete)
        self.assertAlmostEqual(elapsed, 0.1)

    def test_peer_sync_drain_treats_empty_peer_as_complete(self):
        bench = importlib.import_module("benchmark.run_benchmark")
        cursor, complete, elapsed = bench._wait_for_afl_peer_sync(
            "/missing",
            "symcc01",
            0,
            None,
            5.0,
            cursor_reader=lambda _instance, _peer: None,
            monotonic=lambda: 10.0,
        )
        self.assertIsNone(cursor)
        self.assertTrue(complete)
        self.assertEqual(elapsed, 0.0)

    def test_batch_triage_publishes_to_independent_afl_foreign_queue(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "symcc01" / "queue"
            crashes = root / "symcc01" / "crashes"
            hangs = root / "symcc01" / "hangs"
            foreign = root / "symcc_foreign"
            for directory in (queue, crashes, hangs, foreign):
                directory.mkdir(parents=True)
            source = root / "id:000123,orig:seed"
            source.write_bytes(b"seed")

            stats = helper.Stats()
            feedback = []
            staged_names = []
            real_replace = os.replace

            def audited_replace(src, dst):
                staged_names.append(Path(src).name)
                return real_replace(src, dst)

            with mock.patch.object(helper.os, "replace", side_effect=audited_replace):
                changed = helper._batch_triage(
                    [
                        (
                            1,
                            str(source),
                            [{"content": b"candidate", "bitmap": [(17, 1)]}],
                            0,
                            0.01,
                            False,
                            "exact",
                            None,
                            (),
                            "",
                            {},
                        )
                    ],
                    stats,
                    helper.CoverageBitmap(),
                    object(),
                    str(queue),
                    str(crashes),
                    str(hangs),
                    str(foreign),
                    None,
                    str(root / "symcc01"),
                    str(root / ".triage_bitmap"),
                    feedback,
                    [0],
                )

            self.assertTrue(changed)
            self.assertEqual(stats.interesting_count, 1)
            self.assertEqual(len(feedback), 1)
            self.assertEqual(
                [path.read_bytes() for path in queue.iterdir()],
                [b"candidate"],
            )
            self.assertEqual(
                [path.read_bytes() for path in foreign.iterdir()],
                [b"candidate"],
            )
            self.assertEqual(
                [path.name for path in foreign.iterdir()],
                ["id:000000,src:000123"],
            )
            self.assertTrue(staged_names)
            self.assertTrue(
                all(name.startswith(".symcc-publish-") for name in staged_names)
            )
            self.assertFalse((root / "fuzzer01" / "queue").exists())

    def test_batch_triage_reports_authoritative_agentic_coverage_delta(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "queue"
            crashes = root / "crashes"
            hangs = root / "hangs"
            for directory in (queue, crashes, hangs):
                directory.mkdir()
            source = root / "id:000123,orig:seed"
            source.write_bytes(b"seed")
            observations = []
            common = (
                str(source),
                0,
                0.01,
                False,
                0,
                None,
                (),
                "",
                {},
            )
            changed = helper._batch_triage(
                [
                    (1, common[0], [{"content": b"first", "bitmap": [(17, 1)]}], *common[1:]),
                    (2, common[0], [{"content": b"second", "bitmap": [(17, 1)]}], *common[1:]),
                ],
                helper.Stats(),
                helper.CoverageBitmap(),
                object(),
                str(queue),
                str(crashes),
                str(hangs),
                None,
                None,
                str(root),
                str(root / ".triage_bitmap"),
                [],
                [0],
                agentic_observation_callback=lambda rank, delta, cases: (
                    observations.append((rank, delta, cases))
                ),
            )
            self.assertTrue(changed)
            self.assertEqual(observations, [(1, 1, 1), (2, 0, 0)])

    def test_batch_triage_does_not_claim_coverage_before_publish(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "queue"
            crashes = root / "crashes"
            hangs = root / "hangs"
            for directory in (queue, crashes, hangs):
                directory.mkdir()
            source = root / "id:000123,orig:seed"
            source.write_bytes(b"seed")
            claims = []

            with mock.patch.object(
                helper, "_atomic_publish", side_effect=OSError("disk full")
            ):
                changed = helper._batch_triage(
                    [
                        (
                            1,
                            str(source),
                            [
                                {
                                    "content": b"candidate",
                                    "bitmap": [(17, 1)],
                                }
                            ],
                            0,
                            0.01,
                            False,
                            "exact",
                            None,
                            (),
                            "",
                            {},
                        )
                    ],
                    helper.Stats(),
                    helper.CoverageBitmap(),
                    object(),
                    str(queue),
                    str(crashes),
                    str(hangs),
                    None,
                    None,
                    str(root),
                    str(root / ".triage_bitmap"),
                    [],
                    [0],
                    coverage_claim_callback=lambda bitmap: (
                        claims.append(bitmap) or (1, True)
                    ),
                )

            self.assertFalse(changed)
            self.assertEqual(claims, [])
            self.assertEqual(list(queue.iterdir()), [])

    def test_batch_triage_keeps_gap_free_slot_after_lost_claim(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "queue"
            crashes = root / "crashes"
            hangs = root / "hangs"
            for directory in (queue, crashes, hangs):
                directory.mkdir()
            source = root / "id:000123,orig:seed"
            source.write_bytes(b"seed")

            queue_id = [0]
            stats = helper.Stats()
            changed = helper._batch_triage(
                [
                    (
                        1,
                        str(source),
                        [
                            {
                                "content": b"candidate",
                                "bitmap": [(17, 1)],
                            }
                        ],
                        0,
                        0.01,
                        False,
                        "exact",
                        None,
                        (),
                        "",
                        {},
                    )
                ],
                stats,
                helper.CoverageBitmap(),
                object(),
                str(queue),
                str(crashes),
                str(hangs),
                None,
                None,
                str(root),
                str(root / ".triage_bitmap"),
                [],
                queue_id,
                coverage_claim_callback=lambda _bitmap: (0, False),
            )

            self.assertFalse(changed)
            self.assertEqual(queue_id, [1])
            self.assertEqual(stats.interesting_count, 0)
            self.assertEqual(
                [path.read_bytes() for path in queue.iterdir()],
                [b"candidate"],
            )

    def test_batch_triage_group_commits_local_queue_directory_once(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "queue"
            crashes = root / "crashes"
            hangs = root / "hangs"
            for directory in (queue, crashes, hangs):
                directory.mkdir()
            source = root / "id:000123,orig:seed"
            source.write_bytes(b"seed")

            sync_counts = {"file": 0, "directory": 0}
            real_fsync = os.fsync

            def audited_fsync(descriptor):
                kind = (
                    "directory"
                    if stat.S_ISDIR(os.fstat(descriptor).st_mode)
                    else "file"
                )
                sync_counts[kind] += 1
                return real_fsync(descriptor)

            with mock.patch.object(helper.os, "fsync", side_effect=audited_fsync):
                changed = helper._batch_triage(
                    [
                        (
                            1,
                            str(source),
                            [
                                {"content": b"first", "bitmap": [(17, 1)]},
                                {"content": b"second", "bitmap": [(23, 1)]},
                            ],
                            0,
                            0.01,
                            False,
                            "exact",
                            None,
                            (),
                            "",
                            {},
                        )
                    ],
                    helper.Stats(),
                    helper.CoverageBitmap(),
                    object(),
                    str(queue),
                    str(crashes),
                    str(hangs),
                    None,
                    None,
                    str(root),
                    str(root / ".triage_bitmap"),
                    [],
                    [0],
                )

            self.assertTrue(changed)
            self.assertEqual(sync_counts, {"file": 2, "directory": 1})
            self.assertEqual(len(list(queue.iterdir())), 2)

    def test_batch_triage_claims_one_owner_shard_transaction_per_result(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        distributed = importlib.import_module("util.distributed_state")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "queue"
            crashes = root / "crashes"
            hangs = root / "hangs"
            for directory in (queue, crashes, hangs):
                directory.mkdir()
            source = root / "id:000123,orig:seed"
            source.write_bytes(b"seed")
            coverage = helper.CoverageBitmap()
            gossip = distributed.CoverageOwnerShardGossip(
                str(root / "coverage-owner"),
                shard_count=4,
                coordinator_id="master",
            )
            stats = helper.Stats()
            queue_id = [0]
            analyzed = set()

            def claim_batch(bitmaps):
                self.assertEqual(list(queue.iterdir()), [])
                return helper._claim_coverage_transactions(
                    coverage, gossip, bitmaps
                )

            changed = helper._batch_triage(
                [
                    (
                        1,
                        str(source),
                        [
                            {"content": b"first", "bitmap": [(1, 1)]},
                            {"content": b"second", "bitmap": [(5, 1)]},
                        ],
                        0,
                        0.01,
                        False,
                        "exact",
                        None,
                        (),
                        "",
                        {},
                    )
                ],
                stats,
                coverage,
                object(),
                str(queue),
                str(crashes),
                str(hangs),
                None,
                None,
                str(root),
                str(root / ".triage_bitmap"),
                [],
                queue_id,
                analyzed_hashes_ref=analyzed,
                coverage_claim_callback=lambda bitmap: (
                    helper._claim_coverage_transaction(
                        coverage, gossip, bitmap
                    )
                ),
                coverage_claim_many_callback=claim_batch,
            )

            self.assertTrue(changed)
            self.assertEqual(queue_id, [2])
            self.assertEqual(stats.interesting_count, 2)
            self.assertEqual(len(list(queue.iterdir())), 2)
            self.assertEqual(
                analyzed,
                {
                    hashlib.sha256(b"first").hexdigest(),
                    hashlib.sha256(b"second").hexdigest(),
                },
            )
            snapshot = gossip.snapshot()
            self.assertEqual(snapshot["claims"], 2)
            self.assertEqual(snapshot["claim_batches"], 1)
            self.assertEqual(snapshot["claim_shard_writes"], 1)

    def test_batch_triage_claim_crash_keeps_queue_hidden_and_recoverable(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "queue"
            crashes = root / "crashes"
            hangs = root / "hangs"
            for directory in (queue, crashes, hangs):
                directory.mkdir()
            source = root / "id:000001,orig:seed"
            source.write_bytes(b"seed")

            def interrupted_claim(_bitmaps):
                self.assertEqual(list(queue.iterdir()), [])
                raise RuntimeError("injected owner reply loss")

            with self.assertRaisesRegex(RuntimeError, "owner reply loss"):
                helper._batch_triage(
                    [(
                        1,
                        str(source),
                        [{"content": b"recoverable", "bitmap": [(7, 1)]}],
                        0,
                        0.01,
                        False,
                        "exact",
                        None,
                        (),
                        "",
                        {},
                    )],
                    helper.Stats(),
                    helper.CoverageBitmap(),
                    object(),
                    str(queue),
                    str(crashes),
                    str(hangs),
                    None,
                    None,
                    str(root),
                    str(root / ".triage_bitmap"),
                    [],
                    [0],
                    coverage_claim_many_callback=interrupted_claim,
                )

            self.assertEqual(list(queue.iterdir()), [])
            recovered = helper.CoverageQueueTransactionStore(
                str(root), str(queue)
            ).recover(0)
            self.assertEqual(recovered.conservatively_recovered, 1)
            self.assertEqual(
                (queue / "id:000000,src:000001").read_bytes(),
                b"recoverable",
            )

    def test_committed_queue_id_survives_later_batch_callback_failure(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "queue"
            crashes = root / "crashes"
            hangs = root / "hangs"
            for directory in (queue, crashes, hangs):
                directory.mkdir()
            source = root / "id:000001,orig:seed"
            source.write_bytes(b"seed")
            queue_id = [0]

            def fail_observation(*_args):
                raise RuntimeError("injected post-commit callback failure")

            with self.assertRaisesRegex(RuntimeError, "post-commit callback"):
                helper._batch_triage(
                    [(
                        1,
                        str(source),
                        [{"content": b"committed", "bitmap": [(7, 1)]}],
                        0,
                        0.01,
                        False,
                        "exact",
                        None,
                        (),
                        "",
                        {},
                    )],
                    helper.Stats(),
                    helper.CoverageBitmap(),
                    object(),
                    str(queue),
                    str(crashes),
                    str(hangs),
                    None,
                    None,
                    str(root),
                    str(root / ".triage_bitmap"),
                    [],
                    queue_id,
                    observation_callback=fail_observation,
                    coverage_claim_many_callback=lambda _bitmaps: ([1], True),
                )

            self.assertEqual(queue_id, [1])
            self.assertEqual(
                (queue / "id:000000,src:000001").read_bytes(),
                b"committed",
            )

    def test_lost_global_claim_converges_local_bitmap_immediately(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")

        class LostRaceGossip:
            def claim(self, _bitmap):
                return 0

            def pull_recent_claims(self):
                return []

        coverage = helper.CoverageBitmap()
        claimed, changed = helper._claim_coverage_transaction(
            coverage, LostRaceGossip(), [(17, 3)]
        )

        self.assertEqual(claimed, 0)
        self.assertTrue(changed)
        self.assertEqual(coverage.count_delta([(17, 3)]), 0)

    def test_lost_batch_claim_is_removed_without_symbolic_rediscovery(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "queue"
            crashes = root / "crashes"
            hangs = root / "hangs"
            for directory in (queue, crashes, hangs):
                directory.mkdir()
            source = root / "id:000001,orig:seed"
            source.write_bytes(b"seed")
            child = b"globally-redundant-child"
            analyzed = set()

            changed = helper._batch_triage(
                [(1, str(source), [{"content": child, "bitmap": [(7, 1)]}],
                  0, 0.01, False, "exact", None, (), "", {})],
                helper.Stats(),
                helper.CoverageBitmap(),
                object(),
                str(queue),
                str(crashes),
                str(hangs),
                None,
                None,
                str(root),
                str(root / ".triage_bitmap"),
                [],
                [0],
                analyzed_hashes_ref=analyzed,
                coverage_claim_many_callback=lambda bitmaps: (
                    [0 for _ in bitmaps], False
                ),
            )

            self.assertFalse(changed)
            self.assertEqual(len(list(queue.iterdir())), 0)
            self.assertEqual(analyzed, {hashlib.sha256(child).hexdigest()})

    def test_batch_claim_compacts_winners_to_gap_free_queue_ids(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "queue"
            crashes = root / "crashes"
            hangs = root / "hangs"
            for directory in (queue, crashes, hangs):
                directory.mkdir()
            source = root / "id:000001,orig:seed"
            source.write_bytes(b"seed")
            queue_id = [0]
            feedback = []

            changed = helper._batch_triage(
                [(
                    1,
                    str(source),
                    [
                        {"content": b"lost", "bitmap": [(7, 1)]},
                        {"content": b"winner", "bitmap": [(9, 1)]},
                    ],
                    0,
                    0.01,
                    False,
                    "exact",
                    None,
                    (),
                    "",
                    {},
                )],
                helper.Stats(),
                helper.CoverageBitmap(),
                object(),
                str(queue),
                str(crashes),
                str(hangs),
                None,
                None,
                str(root),
                str(root / ".triage_bitmap"),
                feedback,
                queue_id,
                coverage_claim_many_callback=lambda bitmaps: ([0, 1], True),
            )

            self.assertTrue(changed)
            self.assertEqual(queue_id, [1])
            entries = list(queue.iterdir())
            self.assertEqual(len(entries), 1)
            self.assertTrue(entries[0].name.startswith("id:000000,"))
            self.assertEqual(entries[0].read_bytes(), b"winner")
            self.assertEqual(feedback, [(str(entries[0]), 1)])

    def test_queue_ids_remain_contiguous_across_hangs_and_crashes(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "symcc01" / "queue"
            crashes = root / "symcc01" / "crashes"
            hangs = root / "symcc01" / "hangs"
            for directory in (queue, crashes, hangs):
                directory.mkdir(parents=True)
            source = root / "id:000123,orig:seed"
            source.write_bytes(b"seed")
            signaled_source = root / "id:000124,orig:abort"
            signaled_source.write_bytes(b"signal-seed")
            queue_ids = [0]
            crash_ids = [0]
            hang_ids = [0]
            analyzed = set()
            persisted = []

            changed = helper._batch_triage(
                [
                    (1, str(source), [], 137, 0.01, True, "exact", None, (), "", {}),
                    (2, str(source), [], 139, 0.01, False, "exact", None, (), "", {}),
                    (
                        4,
                        str(signaled_source),
                        [],
                        -6,
                        0.01,
                        False,
                        "exact",
                        None,
                        (),
                        "",
                        {},
                    ),
                    (
                            3,
                            str(source),
                            [
                                {"content": b"candidate", "bitmap": [(17, 1)]},
                                {
                                    "content": b"child-crash",
                                    "terminal_status": "crash",
                                    "terminal_detail": 11,
                                },
                                {
                                    "content": b"child-timeout",
                                    "terminal_status": "timeout",
                                    "terminal_detail": 9,
                                },
                            ],
                        0,
                        0.01,
                        False,
                        "exact",
                        None,
                        (),
                        "",
                        {},
                    ),
                ],
                helper.Stats(),
                helper.CoverageBitmap(),
                object(),
                str(queue),
                str(crashes),
                str(hangs),
                None,
                None,
                str(root / "symcc01"),
                str(root / ".triage_bitmap"),
                [],
                queue_ids,
                crash_id_ref=crash_ids,
                hang_id_ref=hang_ids,
                analyzed_hashes_ref=analyzed,
                analyzed_hash_callback=persisted.append,
            )

            self.assertTrue(changed)
            self.assertEqual(
                [path.name for path in queue.iterdir()], ["id:000000,src:000123"]
            )
            self.assertEqual(
                sorted(path.read_bytes() for path in crashes.iterdir()),
                [b"child-crash", b"seed", b"signal-seed"],
            )
            self.assertEqual(
                sorted(path.read_bytes() for path in hangs.iterdir()),
                [b"child-timeout", b"seed"],
            )
            self.assertEqual((queue_ids, crash_ids, hang_ids), ([1], [3], [2]))
            digests = {
                hashlib.sha256(content).hexdigest()
                for content in (b"candidate", b"child-crash", b"child-timeout")
            }
            self.assertEqual(analyzed, digests)
            self.assertEqual(set(persisted), digests)

    def test_terminal_artifact_id_exhaustion_is_explicit(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "queue"
            crashes = root / "crashes"
            hangs = root / "hangs"
            for directory in (queue, crashes, hangs):
                directory.mkdir()
            source = root / "id:000001,orig:seed"
            source.write_bytes(b"seed")

            for label, retcode, killed in (
                ("hang", 137, True),
                ("crash", 139, False),
            ):
                with self.subTest(label=label), self.assertRaisesRegex(
                    RuntimeError, rf"AFL {label} ID space is exhausted"
                ):
                    helper._batch_triage(
                        [(1, str(source), [], retcode, 0.01, killed,
                          0, None, (), "", {})],
                        helper.Stats(),
                        helper.CoverageBitmap(),
                        object(),
                        str(queue),
                        str(crashes),
                        str(hangs),
                        None,
                        None,
                        str(root),
                        str(root / ".bitmap"),
                        [],
                        [0],
                        crash_id_ref=[helper._MAX_AFL_ARTIFACT_ID + 1],
                        hang_id_ref=[helper._MAX_AFL_ARTIFACT_ID + 1],
                    )

    def test_terminal_digest_cache_avoids_per_batch_directory_scans(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "queue"
            crashes = root / "crashes"
            hangs = root / "hangs"
            for directory in (queue, crashes, hangs):
                directory.mkdir()
            source = root / "id:000001,orig:seed"
            source.write_bytes(b"seed")
            crash_cache = set()
            hang_cache = set()
            crash_ids = [0]
            result = [(
                1,
                str(source),
                [{
                    "content": b"terminal",
                    "terminal_status": "crash",
                    "terminal_detail": 11,
                }],
                0,
                0.01,
                False,
                0,
                None,
                (),
                "",
                {},
            )]
            with mock.patch.object(
                helper.os,
                "listdir",
                side_effect=AssertionError("terminal cache must avoid scans"),
            ):
                for _ in range(2):
                    helper._batch_triage(
                        result,
                        helper.Stats(),
                        helper.CoverageBitmap(),
                        object(),
                        str(queue),
                        str(crashes),
                        str(hangs),
                        None,
                        None,
                        str(root),
                        str(root / ".bitmap"),
                        [],
                        [0],
                        crash_id_ref=crash_ids,
                        hang_id_ref=[0],
                        crash_digests_ref=crash_cache,
                        hang_digests_ref=hang_cache,
                    )
            digest = hashlib.sha256(b"terminal").hexdigest()
            self.assertEqual(crash_cache, {digest})
            self.assertEqual(len(list(crashes.iterdir())), 1)
            self.assertEqual(crash_ids, [1])

    def test_parse_args_rejects_an_afl_instance_own_queue_as_sync_dir(self):
        helper = importlib.import_module("util.mpi_fuzzing_helper")
        with tempfile.TemporaryDirectory() as tmp:
            own_queue = Path(tmp) / "fuzzer01" / "queue"
            argv = [
                "mpi_fuzzing_helper.py",
                "-a",
                "fuzzer01",
                "-o",
                tmp,
                "-n",
                "symcc01",
                "--afl-sync-dir",
                str(own_queue),
                "--",
                "/bin/true",
            ]
            with mock.patch.object(sys, "argv", argv):
                with self.assertRaises(SystemExit):
                    helper.parse_args()

            symcc_queue = Path(tmp) / "symcc01" / "queue"
            argv[argv.index(str(own_queue))] = str(symcc_queue)
            with mock.patch.object(sys, "argv", argv):
                with self.assertRaises(SystemExit):
                    helper.parse_args()

            alias = Path(tmp) / "queue-alias"
            alias.symlink_to(own_queue)
            argv[argv.index(str(symcc_queue))] = str(alias)
            with mock.patch.object(sys, "argv", argv):
                with self.assertRaises(SystemExit):
                    helper.parse_args()


if __name__ == "__main__":
    unittest.main()
