# RUN: python3 %s

import json
import gc
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import warnings
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from distributed_state import LiveStateStore  # noqa: E402
from live_continuation import (  # noqa: E402
    LIVE_PERSISTENT_EXECUTION_SCHEMA,
    LiveContinuationExecutor,
)
from live_state_frontier import (  # noqa: E402
    PersistentLiveStateFrontier,
)
from live_state_search import LiveStateSearchPolicy  # noqa: E402


def branching_program():
    return {
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
                            "dst": "condition",
                            "left": {"var": "byte"},
                            "right": {"const": 65, "bits": 8},
                            "bits": 1,
                        },
                        {
                            "op": "branch",
                            "condition": {"var": "condition"},
                            "true": "yes",
                            "false": "no",
                            "site": 7001,
                        },
                    ],
                    "yes": [{"op": "halt", "value": 1}],
                    "no": [{"op": "halt", "value": 2}],
                },
            },
        },
    }


class PersistentLiveContinuationTests(unittest.TestCase):
    def test_path_cover_configuration_and_scores_survive_worker_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            store_root = os.path.join(temporary, "store")
            frontier_root = os.path.join(temporary, "frontier")
            with mock.patch.dict(os.environ, {
                "SYMCC_LIVE_SEARCH": "path-cover",
                "SYMCC_LIVE_MPC_COVERS": "2",
                "SYMCC_LIVE_MPC_FUNCTION_NODES": "32",
            }):
                executor = LiveContinuationExecutor(LiveStateStore(store_root))
                root = executor.create(branching_program(), input_bytes=b"A")
                first = executor.resume_persistent(
                    root,
                    frontier_root,
                    owner="first",
                    max_claims=1,
                    max_steps_per_claim=3,
                    max_states_per_claim=1,
                )
                self.assertEqual(first["frontier"]["ready"], 2)
                expected_graph_telemetry = {
                    "enabled": True,
                    "functions_total": 1,
                    "functions_admitted": 1,
                    "functions_skipped_oversized": 0,
                    "components": 3,
                    "covers": 2,
                    "decision_tokens": 2,
                    "ambiguous_decision_tokens": 0,
                }
                self.assertEqual(
                    first["state_search"]["path_cover_graph"],
                    expected_graph_telemetry,
                )
                self.assertEqual(
                    first["executions"][0]["result"]["state_search"]
                    ["path_cover_graph"],
                    expected_graph_telemetry,
                )
                snapshot = PersistentLiveStateFrontier(frontier_root).snapshot()
                self.assertEqual(
                    snapshot.search["schema"],
                    "symcc-live-state-search-snapshot-v5",
                )
                self.assertEqual(snapshot.search["path_cover_max_covers"], 2)
                candidates, graph = executor._persistent_candidates(snapshot, 8)
                features = [
                    executor._search_features(candidate, graph)
                    for candidate in candidates
                ]
                self.assertTrue(all(
                    feature.path_cover_score is not None for feature in features
                ))
                executor._search.observe_location(("main:yes",))
                context = graph.path_cover_coverage_context(
                    executor._search.covered_locations,
                )
                features = [
                    executor._search_features(candidate, graph, context)
                    for candidate in candidates
                ]
                selected = executor._search.select_index(features)
                self.assertEqual(candidates[selected].frames[-1].block, "no")
                executor.close()

            with mock.patch.dict(os.environ, {
                "SYMCC_LIVE_SEARCH": "path-cover",
                "SYMCC_LIVE_MPC_COVERS": "8",
                "SYMCC_LIVE_MPC_FUNCTION_NODES": "4096",
            }):
                restarted = LiveContinuationExecutor(LiveStateStore(store_root))
                result = restarted.resume_persistent(
                    root,
                    frontier_root,
                    owner="second",
                    max_claims=4,
                    max_steps_per_claim=2,
                    max_states_per_claim=1,
                )
                restarted.close()
                self.assertEqual(result["frontier"]["ready"], 0)
                final = PersistentLiveStateFrontier(frontier_root).snapshot()
                self.assertEqual(final.search["path_cover_max_covers"], 2)
                self.assertEqual(
                    final.search["path_cover_max_function_nodes"], 32,
                )
                self.assertEqual(final.search["random"]["draws"], 0)

    def test_solver_artifacts_are_cleaned_without_resource_warning(self):
        with tempfile.TemporaryDirectory() as temporary:
            executor = LiveContinuationExecutor(
                LiveStateStore(Path(temporary) / "store")
            )
            artifact = executor._feasibility._artifact(
                "test", "cleanup", lambda: "(set-logic QF_BV)\n"
            )
            artifact_root = artifact.parent
            self.assertTrue(artifact.is_file())

            with warnings.catch_warnings(record=True) as recorded:
                warnings.simplefilter("always", ResourceWarning)
                del executor
                gc.collect()

            self.assertFalse(artifact_root.exists())
            self.assertFalse(any(
                issubclass(item.category, ResourceWarning)
                for item in recorded
            ))

    def test_restart_finishes_exactly_the_durable_frontier(self):
        with tempfile.TemporaryDirectory() as temporary:
            store_root = os.path.join(temporary, "store")
            frontier_root = os.path.join(temporary, "frontier")
            store = LiveStateStore(store_root)
            first_executor = LiveContinuationExecutor(store)
            root = first_executor.create(
                branching_program(), input_bytes=b"A"
            )
            first = first_executor.resume_persistent(
                root,
                frontier_root,
                owner="first",
                max_claims=1,
                max_steps_per_claim=3,
                max_states_per_claim=1,
            )
            first_executor.close()
            self.assertEqual(first["schema"], LIVE_PERSISTENT_EXECUTION_SCHEMA)
            self.assertEqual(first["claims_completed"], 1)
            self.assertEqual(first["frontier"]["ready"], 2)
            self.assertTrue(first["bounded"])

            second_executor = LiveContinuationExecutor(
                LiveStateStore(store_root)
            )
            second = second_executor.resume_persistent(
                root,
                frontier_root,
                owner="second",
                max_claims=4,
                max_steps_per_claim=2,
                max_states_per_claim=1,
            )
            second_executor.close()
            self.assertEqual(second["claims_completed"], 2)
            self.assertEqual(second["frontier"]["ready"], 0)
            self.assertEqual(second["frontier"]["leased"], 0)
            self.assertEqual(second["frontier"]["done"], 3)
            self.assertFalse(second["bounded"])
            self.assertEqual(
                sorted(
                    halted["value"]
                    for execution in second["executions"]
                    for halted in execution["result"]["halted"]
                ),
                [1, 2],
            )
            snapshot = PersistentLiveStateFrontier(frontier_root).snapshot()
            self.assertGreaterEqual(
                snapshot.search["location_counts"][0][1], 1
            )
            telemetry = second["state_search"]
            self.assertEqual(telemetry["outcome_attempts"], 3)
            self.assertEqual(telemetry["outcome_completions"], 3)
            self.assertEqual(telemetry["outcome_coverage_gain"], 3)

    def test_new_process_restores_search_and_completes_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            store_root = os.path.join(temporary, "store")
            frontier_root = os.path.join(temporary, "frontier")
            executor = LiveContinuationExecutor(LiveStateStore(store_root))
            root = executor.create(branching_program(), input_bytes=b"A")
            executor.resume_persistent(
                root,
                frontier_root,
                owner="parent",
                max_claims=1,
                max_steps_per_claim=3,
                max_states_per_claim=1,
            )
            executor.close()
            child = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "util/symcc_live_state.py"),
                    store_root,
                    "resume-persistent",
                    root,
                    frontier_root,
                    "--owner",
                    "child",
                    "--max-claims",
                    "4",
                    "--max-steps",
                    "2",
                    "--max-states",
                    "1",
                ],
                cwd=ROOT,
                env={**os.environ, "PYTHONPATH": str(ROOT / "util")},
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(child.returncode, 0, child.stderr)
            result = json.loads(child.stdout)
            self.assertEqual(result["frontier"]["done"], 3)
            self.assertFalse(result["bounded"])
            inspected = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "util/symcc_live_state.py"),
                    store_root,
                    "frontier-inspect",
                    frontier_root,
                ],
                cwd=ROOT,
                env={**os.environ, "PYTHONPATH": str(ROOT / "util")},
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(inspected.returncode, 0, inspected.stderr)
            inspection = json.loads(inspected.stdout)
            self.assertTrue(inspection["verified"])
            self.assertEqual(inspection["done"], 3)
            self.assertEqual(inspection["state_search"]["selection_rounds"], 3)

    def test_heartbeat_loss_rejects_execution_and_requeues_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = LiveStateStore(os.path.join(temporary, "store"))
            frontier_root = os.path.join(temporary, "frontier")
            executor = LiveContinuationExecutor(store)
            root = executor.create(branching_program(), input_bytes=b"A")
            original_resume = executor.resume

            def delayed_resume(*args, **kwargs):
                time.sleep(0.08)
                return original_resume(*args, **kwargs)

            with mock.patch.object(
                executor, "resume", side_effect=delayed_resume,
            ), mock.patch.object(
                PersistentLiveStateFrontier,
                "heartbeat",
                return_value=None,
            ), self.assertRaisesRegex(RuntimeError, "heartbeat failed"):
                executor.resume_persistent(
                    root,
                    frontier_root,
                    owner="owner",
                    max_claims=1,
                    max_steps_per_claim=3,
                    max_states_per_claim=1,
                    lease_ttl=0.1,
                )
            snapshot = PersistentLiveStateFrontier(
                frontier_root, lease_ttl=0.1
            ).snapshot()
            self.assertEqual(snapshot.ready, (root,))
            self.assertEqual(snapshot.leases, ())
            telemetry = LiveStateSearchPolicy.from_snapshot(
                snapshot.search
            ).telemetry()
            self.assertEqual(telemetry["outcome_attempts"], 1)
            # The expired token cannot publish even failure telemetry.  Only
            # authoritative expiry recovery requeues the checkpoint.
            self.assertEqual(telemetry["outcome_completions"], 0)
            self.assertEqual(telemetry["outcome_failures"], 0)

    def test_heartbeat_covers_slow_frontier_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = LiveStateStore(os.path.join(temporary, "store"))
            frontier_root = os.path.join(temporary, "frontier")
            executor = LiveContinuationExecutor(store)
            root = executor.create(branching_program(), input_bytes=b"A")
            original_complete = executor._complete_persistent_claim
            original_heartbeat = PersistentLiveStateFrontier.heartbeat
            publication_started = threading.Event()
            publication_renewed = threading.Event()
            heartbeats_during_publication = []

            def delayed_complete(*args, **kwargs):
                publication_started.set()
                if not publication_renewed.wait(timeout=3.0):
                    raise RuntimeError("heartbeat did not cover publication")
                return original_complete(*args, **kwargs)

            def recording_heartbeat(frontier, lease, **kwargs):
                renewed = original_heartbeat(frontier, lease, **kwargs)
                if publication_started.is_set():
                    heartbeats_during_publication.append(time.monotonic())
                    if renewed is not None:
                        publication_renewed.set()
                return renewed

            with mock.patch.object(
                executor,
                "_complete_persistent_claim",
                side_effect=delayed_complete,
            ), mock.patch.object(
                PersistentLiveStateFrontier,
                "heartbeat",
                new=recording_heartbeat,
            ):
                result = executor.resume_persistent(
                    root,
                    frontier_root,
                    owner="owner",
                    max_claims=1,
                    max_steps_per_claim=3,
                    max_states_per_claim=1,
                    lease_ttl=1.0,
                )
            executor.close()

            self.assertGreaterEqual(len(heartbeats_during_publication), 1)
            self.assertEqual(result["claims_completed"], 1)
            snapshot = PersistentLiveStateFrontier(
                frontier_root, lease_ttl=1.0,
            ).snapshot()
            self.assertEqual(snapshot.leases, ())
            self.assertIn(root, snapshot.done)

    def test_execution_failure_is_recorded_before_requeue(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = LiveStateStore(os.path.join(temporary, "store"))
            frontier_root = os.path.join(temporary, "frontier")
            executor = LiveContinuationExecutor(store)
            root = executor.create(branching_program(), input_bytes=b"A")
            with mock.patch.object(
                executor,
                "resume",
                side_effect=RuntimeError("injected execution failure"),
            ), self.assertRaisesRegex(RuntimeError, "injected execution failure"):
                executor.resume_persistent(
                    root,
                    frontier_root,
                    owner="owner",
                    max_claims=1,
                    max_steps_per_claim=3,
                    max_states_per_claim=1,
                )

            snapshot = PersistentLiveStateFrontier(frontier_root).snapshot()
            self.assertEqual(snapshot.ready, (root,))
            self.assertEqual(snapshot.leases, ())
            telemetry = LiveStateSearchPolicy.from_snapshot(
                snapshot.search
            ).telemetry()
            self.assertEqual(telemetry["outcome_attempts"], 1)
            self.assertEqual(telemetry["outcome_completions"], 1)
            self.assertEqual(telemetry["outcome_failures"], 1)

    def test_frontier_binding_rejects_a_different_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = LiveStateStore(os.path.join(temporary, "store"))
            frontier_root = os.path.join(temporary, "frontier")
            executor = LiveContinuationExecutor(store)
            first = executor.create(branching_program(), input_bytes=b"A")
            second = executor.create(branching_program(), input_bytes=b"B")
            executor.resume_persistent(
                first,
                frontier_root,
                owner="owner",
                max_claims=1,
                max_steps_per_claim=1,
                max_states_per_claim=1,
            )
            with self.assertRaisesRegex(ValueError, "already bound"):
                executor.resume_persistent(
                    second,
                    frontier_root,
                    owner="owner",
                    max_claims=1,
                )

    def test_two_executors_complete_distinct_ready_states(self):
        with tempfile.TemporaryDirectory() as temporary:
            store_root = os.path.join(temporary, "store")
            frontier_root = os.path.join(temporary, "frontier")
            executor = LiveContinuationExecutor(LiveStateStore(store_root))
            root = executor.create(branching_program(), input_bytes=b"A")
            executor.resume_persistent(
                root,
                frontier_root,
                owner="producer",
                max_claims=1,
                max_steps_per_claim=3,
                max_states_per_claim=1,
            )
            executor.close()
            barrier = threading.Barrier(3)
            results = []
            failures = []

            def consume(owner):
                local = LiveContinuationExecutor(LiveStateStore(store_root))
                try:
                    barrier.wait()
                    results.append(local.resume_persistent(
                        root,
                        frontier_root,
                        owner=owner,
                        max_claims=1,
                        max_steps_per_claim=2,
                        max_states_per_claim=1,
                        candidate_window=1,
                    ))
                except BaseException as error:
                    failures.append(error)
                finally:
                    local.close()

            threads = [
                threading.Thread(target=consume, args=(owner,))
                for owner in ("worker-a", "worker-b")
            ]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join()
            self.assertEqual(failures, [])
            self.assertEqual(sum(item["claims_completed"] for item in results), 2)
            final = PersistentLiveStateFrontier(frontier_root).snapshot()
            self.assertEqual(final.ready, ())
            self.assertEqual(final.leases, ())
            self.assertEqual(len(final.done), 3)
            final_policy = LiveStateSearchPolicy.from_snapshot(
                final.search
            ).telemetry()
            self.assertEqual(final_policy["outcome_attempts"], 3)
            self.assertEqual(final_policy["outcome_completions"], 3)
            self.assertEqual(final_policy["outcome_coverage_gain"], 3)


if __name__ == "__main__":
    unittest.main()
