# RUN: python3 %s

import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import random
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

import live_state_frontier  # noqa: E402
from live_state_frontier import PersistentLiveStateFrontier  # noqa: E402
from live_state_search import (  # noqa: E402
    LiveProgramGraph,
    LiveStateSearchFeatures,
    LiveStateSearchPolicy,
    search_decision_token,
)


ROOT_CHECKPOINT = "1" * 64
PROGRAM_ROOT = "2" * 64
CHILD_A = "3" * 64
CHILD_B = "4" * 64

CGS_SNAPSHOT_FIELDS = (
    "cgs_target_limit",
    "cgs_rotation_instructions",
    "cgs_max_function_nodes",
    "cgs_max_branches",
    "cgs_instruction_count",
    "cgs_branch_outcomes",
    "cgs_partial_order",
    "cgs_observations",
    "cgs_dropped_branches",
)


def policy_snapshot(strategy="random-state"):
    return LiveStateSearchPolicy((strategy,), seed=17).snapshot()


def selected_search(snapshot=None):
    policy = LiveStateSearchPolicy.from_snapshot(
        policy_snapshot() if snapshot is None else snapshot
    )
    policy.select_index((
        LiveStateSearchFeatures("candidate", "main:entry"),
    ))
    return policy.snapshot()


def diamond_program():
    return {
        "functions": {
            "main": {
                "entry": "entry",
                "blocks": {
                    "entry": [{
                        "op": "branch",
                        "condition": 1,
                        "true": "left",
                        "false": "right",
                        "site": 101,
                    }],
                    "left": [{"op": "jump", "target": "join"}],
                    "right": [{"op": "jump", "target": "join"}],
                    "join": [{"op": "halt", "value": 0}],
                },
            },
        },
    }


class StableSearchSnapshotTests(unittest.TestCase):
    def test_v1_snapshot_upgrades_with_empty_outcome_history(self):
        snapshot = policy_snapshot()
        snapshot["schema"] = "symcc-live-state-search-snapshot-v1"
        snapshot.pop("outcome_stats")
        snapshot.pop("path_cover_max_covers")
        snapshot.pop("path_cover_max_function_nodes")
        snapshot.pop("cbc_state_threshold")
        snapshot.pop("cbc_max_function_nodes")
        snapshot.pop("cbc_max_branches")
        for field in CGS_SNAPSHOT_FIELDS:
            snapshot.pop(field)
        restored = LiveStateSearchPolicy.from_snapshot(snapshot)
        upgraded = restored.snapshot()
        self.assertEqual(
            upgraded["schema"], "symcc-live-state-search-snapshot-v5"
        )
        self.assertEqual(upgraded["outcome_stats"], [])
        self.assertEqual(upgraded["path_cover_max_covers"], 8)
        self.assertEqual(upgraded["path_cover_max_function_nodes"], 4096)

    def test_v2_snapshot_upgrades_with_bound_path_cover_defaults(self):
        snapshot = policy_snapshot("multi-objective")
        snapshot["schema"] = "symcc-live-state-search-snapshot-v2"
        snapshot.pop("path_cover_max_covers")
        snapshot.pop("path_cover_max_function_nodes")
        snapshot.pop("cbc_state_threshold")
        snapshot.pop("cbc_max_function_nodes")
        snapshot.pop("cbc_max_branches")
        for field in CGS_SNAPSHOT_FIELDS:
            snapshot.pop(field)
        restored = LiveStateSearchPolicy.from_snapshot(snapshot)
        self.assertEqual(restored.snapshot()["schema"],
                         "symcc-live-state-search-snapshot-v5")
        self.assertEqual(restored.snapshot()["outcome_stats"], [])

    def test_v3_snapshot_upgrades_with_bound_cbc_defaults(self):
        snapshot = policy_snapshot("path-cover")
        snapshot["schema"] = "symcc-live-state-search-snapshot-v3"
        snapshot.pop("cbc_state_threshold")
        snapshot.pop("cbc_max_function_nodes")
        snapshot.pop("cbc_max_branches")
        for field in CGS_SNAPSHOT_FIELDS:
            snapshot.pop(field)
        restored = LiveStateSearchPolicy.from_snapshot(snapshot).snapshot()
        self.assertEqual(
            restored["schema"], "symcc-live-state-search-snapshot-v5"
        )
        self.assertEqual(restored["cbc_state_threshold"], 5)
        self.assertEqual(restored["cbc_max_function_nodes"], 4096)
        self.assertEqual(restored["cbc_max_branches"], 4096)

    def test_v4_snapshot_upgrades_with_bound_cgs_defaults(self):
        snapshot = policy_snapshot("cbc")
        snapshot["schema"] = "symcc-live-state-search-snapshot-v4"
        for field in CGS_SNAPSHOT_FIELDS:
            snapshot.pop(field)
        restored = LiveStateSearchPolicy.from_snapshot(snapshot).snapshot()
        self.assertEqual(
            restored["schema"], "symcc-live-state-search-snapshot-v5"
        )
        self.assertEqual(restored["cgs_target_limit"], 10)
        self.assertEqual(restored["cgs_rotation_instructions"], 1_000_000)
        self.assertEqual(restored["cgs_max_function_nodes"], 4096)
        self.assertEqual(restored["cgs_max_branches"], 4096)
        self.assertEqual(restored["cgs_branch_outcomes"], [])

    def test_live_path_cover_prefers_uncovered_diamond_arm(self):
        graph = LiveProgramGraph(diamond_program())
        plan = graph._path_cover_plans["main"]
        self.assertGreaterEqual(len(plan.covers), 2)
        covered = {"main:entry", "main:left"}
        left = graph.path_cover_guidance(
            "main:left", (search_decision_token(101, True),), covered,
        )
        right = graph.path_cover_guidance(
            "main:right", (search_decision_token(101, False),), covered,
        )
        assert left is not None and right is not None
        self.assertEqual(left.recognized_decisions, 1)
        self.assertEqual(right.recognized_decisions, 1)
        self.assertGreater(right.remaining, left.remaining)
        self.assertGreater(right.score, left.score)

    def test_path_cover_selection_is_deterministic_and_restart_exact(self):
        policy = LiveStateSearchPolicy(("path-cover",), seed=99)
        features = (
            LiveStateSearchFeatures(
                "low", "main:left", path_cover_score=0.25,
            ),
            LiveStateSearchFeatures(
                "high", "main:right", path_cover_score=0.75,
            ),
        )
        self.assertEqual(policy.select_index(features), 1)
        self.assertEqual(policy.random.draws, 0)
        restored = LiveStateSearchPolicy.from_snapshot(policy.snapshot())
        self.assertEqual(restored.snapshot(), policy.snapshot())

    def test_path_cover_unknown_token_and_oversized_graph_fail_open(self):
        graph = LiveProgramGraph(diamond_program())
        guidance = graph.path_cover_guidance(
            "main:right", (123456789,), {"main:entry"},
        )
        assert guidance is not None
        self.assertEqual(guidance.recognized_decisions, 0)
        self.assertEqual(
            guidance.compatible_covers,
            len(graph._path_cover_plans["main"].covers),
        )
        plan = graph._path_cover_plans["main"]
        colliding = search_decision_token(101, True)
        graph._path_cover_decisions[colliding].add((
            "main",
            plan.component_for["main:entry"],
            plan.component_for["main:right"],
        ))
        ambiguous = graph.path_cover_guidance(
            "main:left", (colliding,), {"main:entry"},
        )
        assert ambiguous is not None
        self.assertEqual(ambiguous.recognized_decisions, 0)
        self.assertEqual(ambiguous.compatible_covers, len(plan.covers))

        blocks = {
            f"b{index}": [{
                "op": "jump",
                "target": f"b{index + 1}",
            }]
            for index in range(16)
        }
        blocks["b16"] = [{"op": "halt", "value": 0}]
        oversized = LiveProgramGraph(
            {"functions": {"main": {"entry": "b0", "blocks": blocks}}},
            path_cover_max_function_nodes=16,
        )
        self.assertIsNone(oversized.path_cover_guidance(
            "main:b0", (), set(),
        ))
        fallback = LiveStateSearchPolicy(("path-cover",))
        self.assertEqual(fallback.select_index((
            LiveStateSearchFeatures("z", "main:b0"),
            LiveStateSearchFeatures("a", "main:b1"),
        )), 0)

    def test_live_path_cover_collapses_loop_scc(self):
        graph = LiveProgramGraph({"functions": {"main": {
            "entry": "entry",
            "blocks": {
                "entry": [{"op": "jump", "target": "head"}],
                "head": [{
                    "op": "branch", "condition": 1,
                    "true": "body", "false": "exit", "site": 77,
                }],
                "body": [{"op": "jump", "target": "head"}],
                "exit": [{"op": "halt", "value": 0}],
            },
        }}})
        plan = graph._path_cover_plans["main"]
        self.assertEqual(len(plan.component_members), 3)
        self.assertEqual(
            plan.component_for["main:head"],
            plan.component_for["main:body"],
        )

    def test_live_path_cover_builds_deep_cfg_without_python_recursion(self):
        blocks = {
            f"b{index:04d}": [{
                "op": "jump", "target": f"b{index + 1:04d}",
            }]
            for index in range(1499)
        }
        blocks["b1499"] = [{"op": "halt", "value": 0}]
        graph = LiveProgramGraph(
            {"functions": {"main": {"entry": "b0000", "blocks": blocks}}},
            path_cover_max_function_nodes=1500,
        )
        guidance = graph.path_cover_guidance("main:b0000", (), set())
        assert guidance is not None
        self.assertEqual(guidance.compatible_covers, 1)
        self.assertEqual(guidance.remaining, 1.0)

    def test_multi_objective_is_deterministic_and_feedback_aware(self):
        policy = LiveStateSearchPolicy(("multi-objective",), seed=91)
        for _ in range(8):
            policy.observe_location(("main:stale",))
        features = [
            LiveStateSearchFeatures(
                "stale", "main:stale", solver_queries=20,
                distance_to_uncovered=4, outcome_key="ctx:stale",
            ),
            LiveStateSearchFeatures(
                "novel", "main:novel", solver_queries=0,
                distance_to_uncovered=0, exits_cycle=True,
                outcome_key="ctx:novel",
            ),
        ]
        self.assertEqual(policy.select_index(features), 1)
        selected = policy.snapshot()
        stats = dict((entry[0], entry[1:]) for entry in selected["outcome_stats"])
        self.assertEqual(stats["ctx:novel"][0], 1)
        self.assertEqual(selected["random"]["draws"], 0)
        policy.observe_outcome(
            "ctx:novel", coverage_gain=3, steps=12, solver_queries=1,
        )
        restored = LiveStateSearchPolicy.from_snapshot(policy.snapshot())
        self.assertEqual(restored.snapshot(), policy.snapshot())
        telemetry = restored.telemetry()
        self.assertEqual(telemetry["outcome_attempts"], 1)
        self.assertEqual(telemetry["outcome_completions"], 1)
        self.assertEqual(telemetry["outcome_coverage_gain"], 3)

    def test_outcome_context_overflow_remains_bounded_and_restorable(self):
        policy = LiveStateSearchPolicy(
            ("multi-objective",), seed=3, counter_limit=16,
        )
        for index in range(40):
            policy.select_index((LiveStateSearchFeatures(
                f"state-{index}", f"main:block-{index}",
                outcome_key=f"ctx:{index}",
            ),))
        snapshot = policy.snapshot()
        self.assertEqual(len(snapshot["outcome_stats"]), 16)
        stats = {entry[0]: entry for entry in snapshot["outcome_stats"]}
        self.assertEqual(stats["@overflow"][1], 25)
        self.assertEqual(
            LiveStateSearchPolicy.from_snapshot(snapshot).snapshot(), snapshot,
        )
        with self.assertRaisesRegex(ValueError, "reserved"):
            policy.select_index((LiveStateSearchFeatures(
                "reserved", "main:reserved", outcome_key="@overflow",
            ),))

    def test_random_stream_and_counters_restore_exactly(self):
        features = [
            LiveStateSearchFeatures("a", "main:a"),
            LiveStateSearchFeatures("b", "main:b"),
            LiveStateSearchFeatures("c", "main:c"),
        ]
        policy = LiveStateSearchPolicy(
            ("random-state", "random-path", "nurs:covnew"), seed=91,
        )
        policy.observe_location(("main:a",))
        policy.observe_location(("main:a", "main:b"))
        for _ in range(7):
            policy.select_index(features)
        restored = LiveStateSearchPolicy.from_snapshot(policy.snapshot())
        self.assertEqual(restored.snapshot(), policy.snapshot())
        self.assertEqual(
            [restored.select_index(features) for _ in range(20)],
            [policy.select_index(features) for _ in range(20)],
        )
        self.assertEqual(restored.snapshot(), policy.snapshot())

    def test_snapshot_rejects_inconsistent_or_unbounded_fields(self):
        snapshot = policy_snapshot()
        mutations = []
        missing = copy.deepcopy(snapshot)
        missing.pop("random")
        mutations.append(missing)
        duplicate = copy.deepcopy(snapshot)
        duplicate["covered_locations"] = ["main:a", "main:a"]
        mutations.append(duplicate)
        inconsistent = copy.deepcopy(snapshot)
        inconsistent["selection_round"] = 1
        mutations.append(inconsistent)
        unknown_random = copy.deepcopy(snapshot)
        unknown_random["random"]["algorithm"] = "python-random"
        mutations.append(unknown_random)
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                LiveStateSearchPolicy.from_snapshot(mutation)

    def test_selection_and_observation_transitions_are_separated(self):
        previous = policy_snapshot()
        selected = selected_search(previous)
        LiveStateSearchPolicy.validate_selection_transition(previous, selected)
        policy = LiveStateSearchPolicy.from_snapshot(selected)
        policy.observe_location(("main:entry", "main:child"))
        observed = policy.snapshot()
        LiveStateSearchPolicy.validate_observation_transition(selected, observed)

        wrong_random = copy.deepcopy(selected)
        wrong_random["random"]["state"] ^= 1
        changed_selection = copy.deepcopy(observed)
        changed_selection["selection_counts"][0][1] += 1
        changed_selection["selection_round"] += 1
        mutations = [
            (previous, wrong_random, "selection"),
            (selected, changed_selection, "observation"),
        ]
        for before, after, kind in mutations:
            validator = (
                LiveStateSearchPolicy.validate_selection_transition
                if kind == "selection"
                else LiveStateSearchPolicy.validate_observation_transition
            )
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                validator(before, after)

    def test_transition_rejects_double_attempt_and_double_completion(self):
        previous = policy_snapshot("multi-objective")
        policy = LiveStateSearchPolicy.from_snapshot(previous)
        feature = LiveStateSearchFeatures(
            "candidate", "main:entry", outcome_key="ctx:entry",
        )
        policy.select_index((feature,))
        selected = policy.snapshot()
        LiveStateSearchPolicy.validate_selection_transition(previous, selected)

        double_attempt = copy.deepcopy(selected)
        double_attempt["outcome_stats"][0][1] += 1
        with self.assertRaisesRegex(ValueError, "outcome stats"):
            LiveStateSearchPolicy.validate_selection_transition(
                previous, double_attempt,
            )

        second_attempt_policy = LiveStateSearchPolicy.from_snapshot(selected)
        second_attempt_policy.select_index((LiveStateSearchFeatures(
            "other", "main:other", outcome_key="ctx:other",
        ),))
        second_attempt = second_attempt_policy.snapshot()
        completed = LiveStateSearchPolicy.from_snapshot(second_attempt)
        completed.observe_outcome(
            "ctx:entry", coverage_gain=1, steps=2, solver_queries=0,
        )
        observed = completed.snapshot()
        LiveStateSearchPolicy.validate_observation_transition(
            second_attempt, observed,
        )
        double_completion_policy = LiveStateSearchPolicy.from_snapshot(
            observed
        )
        double_completion_policy.observe_outcome(
            "ctx:other", coverage_gain=1, steps=1, solver_queries=0,
        )
        double_completion = double_completion_policy.snapshot()
        with self.assertRaisesRegex(ValueError, "multiple attempts"):
            LiveStateSearchPolicy.validate_observation_transition(
                second_attempt, double_completion,
            )


class PersistentLiveStateFrontierTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.frontier = PersistentLiveStateFrontier(
            self.temporary.name,
            lease_ttl=10.0,
        )
        self.initial = self.frontier.initialize(
            ROOT_CHECKPOINT,
            PROGRAM_ROOT,
            policy_snapshot(),
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_claim_complete_and_restart_preserve_partition(self):
        lease = self.frontier.claim(
            ROOT_CHECKPOINT,
            expected_generation=0,
            search=selected_search(),
            owner="coordinator-a",
            worker=7,
            now=100.0,
        )
        self.assertIsNotNone(lease)
        assert lease is not None
        claimed = self.frontier.snapshot()
        self.assertEqual(claimed.ready, ())
        self.assertEqual(claimed.lease_for(ROOT_CHECKPOINT), lease)
        policy = LiveStateSearchPolicy.from_snapshot(claimed.search)
        policy.observe_location(("main:child",))
        update = self.frontier.complete(
            lease,
            [CHILD_A, CHILD_B],
            expected_generation=claimed.generation,
            search=policy.snapshot(),
            now=105.0,
        )
        self.assertEqual(update.status, "completed")
        self.assertEqual(update.snapshot.ready, (CHILD_A, CHILD_B))
        self.assertEqual(update.snapshot.done, (ROOT_CHECKPOINT,))
        restarted = PersistentLiveStateFrontier(self.temporary.name).snapshot()
        self.assertEqual(restarted, update.snapshot)

    def test_generation_conflict_is_non_mutating(self):
        lease = self.frontier.claim(
            ROOT_CHECKPOINT,
            expected_generation=0,
            search=selected_search(),
            owner="owner",
            now=1.0,
        )
        assert lease is not None
        before = self.frontier.snapshot()
        result = self.frontier.complete(
            lease,
            [CHILD_A],
            expected_generation=0,
            search=before.search,
            now=2.0,
        )
        self.assertEqual(result.status, "conflict")
        self.assertEqual(self.frontier.snapshot(), before)

    def test_post_commit_maintenance_error_does_not_report_commit_failure(self):
        original_transition_paths = self.frontier._transition_paths
        calls = 0

        def fail_after_durable_transition():
            nonlocal calls
            calls += 1
            if calls >= 2:
                raise ValueError("injected post-commit journal scan failure")
            return original_transition_paths()

        with mock.patch.object(
            self.frontier,
            "_transition_paths",
            side_effect=fail_after_durable_transition,
        ):
            lease = self.frontier.claim(
                ROOT_CHECKPOINT,
                expected_generation=0,
                search=selected_search(),
                owner="owner",
                now=1.0,
            )

        self.assertIsNotNone(lease)
        maintenance = self.frontier.maintenance_snapshot()
        self.assertEqual(maintenance["failures"], 1)
        self.assertTrue(maintenance["degraded"])
        self.assertIn("injected post-commit", maintenance["last_error"])
        restarted = PersistentLiveStateFrontier(self.temporary.name).snapshot()
        self.assertEqual(restarted.generation, 1)
        self.assertEqual(restarted.lease_for(ROOT_CHECKPOINT), lease)

    def test_post_commit_identity_and_lease_cleanup_errors_are_degraded(self):
        initial_identity = self.frontier._base_identity()
        with mock.patch.object(
            self.frontier,
            "_base_identity",
            side_effect=(
                initial_identity,
                OSError("injected post-commit identity failure"),
            ),
        ):
            lease = self.frontier.claim(
                ROOT_CHECKPOINT,
                expected_generation=0,
                search=selected_search(),
                owner="owner",
                now=1.0,
            )
        self.assertIsNotNone(lease)
        assert lease is not None
        restarted = PersistentLiveStateFrontier(self.temporary.name)
        claimed = restarted.snapshot()
        self.assertEqual(claimed.generation, 1)

        with mock.patch.object(
            restarted,
            "_remove_lease_state",
            side_effect=OSError("injected committed lease cleanup failure"),
        ):
            completed = restarted.complete(
                lease,
                [CHILD_A],
                expected_generation=claimed.generation,
                search=claimed.search,
                now=2.0,
            )
        self.assertEqual(completed.status, "completed")
        self.assertTrue(restarted.maintenance_snapshot()["degraded"])
        final = PersistentLiveStateFrontier(self.temporary.name).snapshot()
        self.assertEqual(final.generation, 2)
        self.assertEqual(final.done, (ROOT_CHECKPOINT,))

    def test_failed_outcome_and_abandon_commit_atomically(self):
        lease = self.frontier.claim(
            ROOT_CHECKPOINT,
            expected_generation=0,
            search=selected_search(),
            owner="owner",
            now=1.0,
        )
        assert lease is not None
        claimed = self.frontier.snapshot()
        policy = LiveStateSearchPolicy.from_snapshot(claimed.search)
        policy.observe_outcome(
            "main:entry",
            coverage_gain=0,
            steps=0,
            solver_queries=0,
            failed=True,
        )

        conflict = self.frontier.abandon(
            lease,
            expected_generation=0,
            search=policy.snapshot(),
            now=2.0,
        )
        self.assertEqual(conflict.status, "conflict")
        self.assertEqual(self.frontier.snapshot(), claimed)

        abandoned = self.frontier.abandon(
            lease,
            expected_generation=claimed.generation,
            search=policy.snapshot(),
            now=2.0,
        )
        self.assertEqual(abandoned.status, "abandoned")
        self.assertEqual(abandoned.snapshot.ready, (ROOT_CHECKPOINT,))
        self.assertEqual(abandoned.snapshot.leases, ())
        telemetry = LiveStateSearchPolicy.from_snapshot(
            abandoned.snapshot.search
        ).telemetry()
        self.assertEqual(telemetry["outcome_attempts"], 1)
        self.assertEqual(telemetry["outcome_completions"], 1)
        self.assertEqual(telemetry["outcome_failures"], 1)

        with self.assertRaisesRegex(ValueError, "generation and search"):
            self.frontier.abandon(
                lease,
                expected_generation=abandoned.snapshot.generation,
                now=2.0,
            )

    def test_expiry_reclaim_fences_late_completion(self):
        old = self.frontier.claim(
            ROOT_CHECKPOINT,
            expected_generation=0,
            search=selected_search(),
            owner="old",
            now=10.0,
        )
        assert old is not None
        recovered = self.frontier.recover_expired(now=20.0)
        self.assertEqual(recovered.status, "recovered")
        replacement = self.frontier.claim(
            ROOT_CHECKPOINT,
            expected_generation=recovered.snapshot.generation,
            search=selected_search(recovered.snapshot.search),
            owner="new",
            now=20.0,
        )
        assert replacement is not None
        self.assertNotEqual(old.token, replacement.token)
        current = self.frontier.snapshot()
        stale = self.frontier.complete(
            old,
            [CHILD_A],
            expected_generation=current.generation,
            search=current.search,
            now=21.0,
        )
        self.assertEqual(stale.status, "stale")
        self.assertEqual(self.frontier.snapshot(), current)

    def test_heartbeat_renews_only_the_current_token(self):
        lease = self.frontier.claim(
            ROOT_CHECKPOINT,
            expected_generation=0,
            search=selected_search(),
            owner="owner",
            now=2.0,
        )
        assert lease is not None
        claimed_generation = self.frontier.snapshot().generation
        with mock.patch.object(
            self.frontier,
            "_read",
            side_effect=AssertionError("heartbeat must not read the full frontier"),
        ), mock.patch.object(
            self.frontier,
            "_write",
            side_effect=AssertionError("heartbeat must not rewrite the frontier"),
        ):
            renewed = self.frontier.heartbeat(lease, now=8.0)
        self.assertIsNotNone(renewed)
        assert renewed is not None
        self.assertEqual(renewed.token, lease.token)
        self.assertEqual(renewed.expires, 18.0)
        self.assertEqual(self.frontier.snapshot().generation, claimed_generation)
        forged = replace(lease, token="f" * 64)
        self.assertIsNone(self.frontier.heartbeat(forged, now=9.0))
        self.assertIsNone(self.frontier.heartbeat(
            replace(lease, owner="forged-owner"), now=9.0))
        self.assertIsNone(self.frontier.heartbeat(
            replace(lease, worker=lease.worker + 1), now=9.0))
        self.assertIsNone(self.frontier.heartbeat(
            replace(lease, claim_generation=lease.claim_generation + 1),
            now=9.0,
        ))
        self.assertIsNone(self.frontier.heartbeat(
            replace(lease, expires=renewed.expires + 1.0), now=9.0))
        self.assertIsNone(self.frontier.heartbeat(renewed, now=18.0))

    def test_complete_fences_every_lease_identity_field(self):
        lease = self.frontier.claim(
            ROOT_CHECKPOINT,
            expected_generation=0,
            search=selected_search(),
            owner="owner",
            worker=4,
            now=2.0,
        )
        assert lease is not None
        claimed = self.frontier.snapshot()
        forged = (
            replace(lease, owner="forged-owner"),
            replace(lease, worker=lease.worker + 1),
            replace(lease, claim_generation=lease.claim_generation + 1),
            replace(lease, expires=lease.expires + 1.0),
        )
        for candidate in forged:
            with self.subTest(candidate=candidate):
                update = self.frontier.complete(
                    candidate,
                    [CHILD_A],
                    expected_generation=claimed.generation,
                    search=claimed.search,
                    now=3.0,
                )
                self.assertEqual(update.status, "stale")
                self.assertEqual(self.frontier.snapshot(), claimed)

        completed = self.frontier.complete(
            lease,
            [CHILD_A],
            expected_generation=claimed.generation,
            search=claimed.search,
            now=3.0,
        )
        self.assertEqual(completed.status, "completed")

    def test_abandon_fences_every_lease_identity_field(self):
        lease = self.frontier.claim(
            ROOT_CHECKPOINT,
            expected_generation=0,
            search=selected_search(),
            owner="owner",
            worker=4,
            now=2.0,
        )
        assert lease is not None
        claimed = self.frontier.snapshot()
        forged = (
            replace(lease, owner="forged-owner"),
            replace(lease, worker=lease.worker + 1),
            replace(lease, claim_generation=lease.claim_generation + 1),
            replace(lease, expires=lease.expires + 1.0),
        )
        for candidate in forged:
            with self.subTest(candidate=candidate):
                update = self.frontier.abandon(candidate, now=3.0)
                self.assertEqual(update.status, "stale")
                self.assertEqual(self.frontier.snapshot(), claimed)

        abandoned = self.frontier.abandon(lease, now=3.0)
        self.assertEqual(abandoned.status, "abandoned")

    def test_two_concurrent_claims_have_one_winner(self):
        generation = self.frontier.snapshot().generation
        barrier = threading.Barrier(3)
        results = []
        failures = []

        def claim(owner):
            try:
                barrier.wait()
                results.append(self.frontier.claim(
                    ROOT_CHECKPOINT,
                    expected_generation=generation,
                    search=selected_search(),
                    owner=owner,
                    now=4.0,
                ))
            except BaseException as error:
                failures.append(error)

        threads = [
            threading.Thread(target=claim, args=(owner,))
            for owner in ("a", "b")
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(failures, [])
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_transition_journal_compacts_and_restarts_exactly(self):
        with tempfile.TemporaryDirectory() as temporary:
            frontier = PersistentLiveStateFrontier(
                temporary,
                lease_ttl=10.0,
                compaction_records=4,
            )
            frontier.initialize(
                ROOT_CHECKPOINT, PROGRAM_ROOT, policy_snapshot(),
            )
            for step in range(2):
                current = frontier.snapshot()
                lease = frontier.claim(
                    ROOT_CHECKPOINT,
                    expected_generation=current.generation,
                    search=selected_search(current.search),
                    owner=f"owner-{step}",
                    now=float(step),
                )
                assert lease is not None
                frontier.abandon(lease, now=float(step) + 0.5)

            snapshot = frontier.snapshot()
            self.assertEqual(snapshot.generation, 4)
            self.assertEqual(
                list((Path(temporary) / "frontier-transitions").glob("*.json")),
                [],
            )
            restarted = PersistentLiveStateFrontier(temporary).snapshot()
            self.assertEqual(restarted, snapshot)

    def test_expiry_recovery_locks_only_a_bounded_candidate_batch(self):
        with tempfile.TemporaryDirectory() as temporary:
            frontier = PersistentLiveStateFrontier(
                temporary,
                lease_ttl=10.0,
                recovery_batch=3,
            )
            frontier.initialize(
                ROOT_CHECKPOINT, PROGRAM_ROOT, policy_snapshot(),
            )
            current = frontier.snapshot()
            root_lease = frontier.claim(
                ROOT_CHECKPOINT,
                expected_generation=current.generation,
                search=selected_search(current.search),
                owner="root-owner",
                now=0.0,
            )
            assert root_lease is not None
            current = frontier.snapshot()
            children = [f"{index:064x}" for index in range(10, 20)]
            frontier.complete(
                root_lease,
                children,
                expected_generation=current.generation,
                search=current.search,
                now=0.5,
            )
            for index, checkpoint in enumerate(children):
                current = frontier.snapshot()
                lease = frontier.claim(
                    checkpoint,
                    expected_generation=current.generation,
                    search=selected_search(current.search),
                    owner=f"owner-{index}",
                    now=1.0,
                )
                assert lease is not None

            with mock.patch.object(
                frontier, "_lease_lock", wraps=frontier._lease_lock,
            ) as lease_lock:
                recovered = frontier.recover_expired(now=20.0)
            self.assertEqual(recovered.status, "recovered")
            self.assertLessEqual(lease_lock.call_count, 3)
            self.assertGreater(lease_lock.call_count, 0)
            self.assertEqual(
                len(recovered.snapshot.ready), lease_lock.call_count
            )
            self.assertEqual(
                len(recovered.snapshot.leases), 10 - lease_lock.call_count
            )

    def test_expiry_recovery_reads_only_a_bounded_due_hint_batch(self):
        with tempfile.TemporaryDirectory() as temporary:
            frontier = PersistentLiveStateFrontier(
                temporary,
                lease_ttl=10.0,
                recovery_batch=3,
            )
            frontier.initialize(
                ROOT_CHECKPOINT, PROGRAM_ROOT, policy_snapshot(),
            )
            current = frontier.snapshot()
            root_lease = frontier.claim(
                ROOT_CHECKPOINT,
                expected_generation=current.generation,
                search=selected_search(current.search),
                owner="root-owner",
                now=0.0,
            )
            assert root_lease is not None
            current = frontier.snapshot()
            children = [f"{index:064x}" for index in range(30, 40)]
            frontier.complete(
                root_lease,
                children,
                expected_generation=current.generation,
                search=current.search,
                now=0.5,
            )
            for index, checkpoint in enumerate(children):
                current = frontier.snapshot()
                lease = frontier.claim(
                    checkpoint,
                    expected_generation=current.generation,
                    search=selected_search(current.search),
                    owner=f"owner-{index}",
                    now=1.0,
                )
                assert lease is not None
                self.assertIsNotNone(frontier.heartbeat(lease, now=10.5))

            restarted = PersistentLiveStateFrontier(
                temporary,
                lease_ttl=10.0,
                recovery_batch=3,
            )
            with mock.patch.object(
                restarted,
                "_read_lease_state",
                wraps=restarted._read_lease_state,
            ) as read_lease:
                unchanged = restarted.recover_expired(now=20.0)
            self.assertEqual(unchanged.status, "unchanged")
            self.assertEqual(read_lease.call_count, 3)

    def test_heartbeat_expiration_heap_remains_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            frontier = PersistentLiveStateFrontier(
                temporary,
                lease_ttl=1000.0,
                recovery_batch=3,
            )
            frontier.initialize(
                ROOT_CHECKPOINT, PROGRAM_ROOT, policy_snapshot(),
            )
            lease = frontier.claim(
                ROOT_CHECKPOINT,
                expected_generation=0,
                search=selected_search(),
                owner="owner",
                now=0.0,
            )
            assert lease is not None
            for timestamp in range(1, 82):
                self.assertIsNotNone(
                    frontier.heartbeat(lease, now=float(timestamp))
                )
            maintenance = frontier.maintenance_snapshot()
            self.assertEqual(maintenance["expiration_hints"], 1)
            self.assertLessEqual(maintenance["expiration_heap_entries"], 64)

    def test_cached_frontier_fails_closed_if_a_journal_record_disappears(self):
        lease = self.frontier.claim(
            ROOT_CHECKPOINT,
            expected_generation=0,
            search=selected_search(),
            owner="owner",
            now=1.0,
        )
        assert lease is not None
        transition = next(
            (Path(self.temporary.name) / "frontier-transitions").glob("*.json")
        )
        transition.unlink()
        with self.assertRaisesRegex(ValueError, "lost a transition"):
            self.frontier.snapshot()

    def test_byte_limit_rejects_transition_before_durable_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            frontier = PersistentLiveStateFrontier(
                temporary,
                max_bytes=4096,
                lease_ttl=10.0,
            )
            frontier.initialize(
                ROOT_CHECKPOINT, PROGRAM_ROOT, policy_snapshot(),
            )
            lease = frontier.claim(
                ROOT_CHECKPOINT,
                expected_generation=0,
                search=selected_search(),
                owner="owner",
                now=1.0,
            )
            assert lease is not None
            claimed = frontier.snapshot()
            policy = LiveStateSearchPolicy.from_snapshot(claimed.search)
            policy.observe_location(("main:child",))
            children = tuple(
                hashlib.sha256(f"child-{index}".encode("ascii")).hexdigest()
                for index in range(64)
            )
            with self.assertRaisesRegex(ValueError, "exceeds byte limit"):
                frontier.complete(
                    lease,
                    children,
                    expected_generation=claimed.generation,
                    search=policy.snapshot(),
                    now=2.0,
                )
            self.assertEqual(frontier.snapshot(), claimed)
            transitions = list(
                (Path(temporary) / "frontier-transitions").glob("*.json")
            )
            self.assertEqual(len(transitions), 1)
            self.assertEqual(
                PersistentLiveStateFrontier(
                    temporary, max_bytes=4096,
                ).snapshot(),
                claimed,
            )

    def test_incremental_byte_accounting_matches_full_encoding(self):
        self.assertEqual(
            self.frontier._cached_snapshot_bytes,
            self.frontier._encoded_snapshot_size(self.initial),
        )
        lease = self.frontier.claim(
            ROOT_CHECKPOINT,
            expected_generation=0,
            search=selected_search(),
            owner='owner-"-\\-line\nnext',
            now=1.0,
        )
        assert lease is not None
        claimed = self.frontier.snapshot()
        self.assertEqual(
            self.frontier._cached_snapshot_bytes,
            self.frontier._encoded_snapshot_size(claimed),
        )
        abandoned = self.frontier.abandon(lease, now=2.0).snapshot
        self.assertEqual(
            self.frontier._cached_snapshot_bytes,
            self.frontier._encoded_snapshot_size(abandoned),
        )

    def test_failed_atomic_replace_keeps_previous_snapshot(self):
        before = self.frontier.snapshot()
        with mock.patch.object(
            live_state_frontier,
            "durable_replace",
            side_effect=OSError("injected replace failure"),
        ), self.assertRaisesRegex(OSError, "injected replace failure"):
            self.frontier.claim(
                ROOT_CHECKPOINT,
                expected_generation=0,
                search=selected_search(),
                owner="owner",
                now=1.0,
            )
        self.assertEqual(self.frontier.snapshot(), before)
        self.assertEqual(
            list(Path(self.temporary.name).glob(".frontier.*.tmp")), []
        )

    def test_digest_corruption_and_root_rebinding_fail_closed(self):
        path = Path(self.temporary.name) / "frontier.json"
        envelope = json.loads(path.read_text(encoding="ascii"))
        envelope["payload"]["generation"] = 5
        path.write_text(json.dumps(envelope), encoding="ascii")
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            self.frontier.snapshot()

        with tempfile.TemporaryDirectory() as temporary:
            frontier = PersistentLiveStateFrontier(temporary)
            frontier.initialize(
                ROOT_CHECKPOINT, PROGRAM_ROOT, policy_snapshot()
            )
            with self.assertRaisesRegex(ValueError, "already bound"):
                frontier.initialize(CHILD_A, PROGRAM_ROOT, policy_snapshot())

    def test_search_configuration_cannot_change_mid_frontier(self):
        changed = LiveStateSearchPolicy(("dfs",), seed=17).snapshot()
        changed_policy = LiveStateSearchPolicy.from_snapshot(changed)
        changed_policy.select_index((
            LiveStateSearchFeatures("candidate", "main:entry"),
        ))
        with self.assertRaisesRegex(ValueError, "changes configuration"):
            self.frontier.claim(
                ROOT_CHECKPOINT,
                expected_generation=0,
                search=changed_policy.snapshot(),
                owner="owner",
            )
        self.assertEqual(self.frontier.snapshot(), self.initial)

    def test_recomputed_digest_cannot_hide_noncanonical_or_illegal_fencing(self):
        lease = self.frontier.claim(
            ROOT_CHECKPOINT,
            expected_generation=0,
            search=selected_search(),
            owner="owner",
            now=1.0,
        )
        assert lease is not None
        path = next(
            (Path(self.temporary.name) / "frontier-transitions").glob("*.json")
        )

        def rewrite(mutator):
            envelope = json.loads(path.read_text(encoding="ascii"))
            mutator(envelope["payload"])
            canonical = json.dumps(
                envelope["payload"],
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            envelope["sha256"] = hashlib.sha256(canonical).hexdigest()
            path.write_text(json.dumps(envelope), encoding="ascii")

        original = path.read_bytes()
        rewrite(lambda payload: payload["leases_add"][0].update(
            owner=" owner "
        ))
        with self.assertRaisesRegex(ValueError, "not canonical"):
            self.frontier.snapshot()
        path.write_bytes(original)
        rewrite(lambda payload: payload["leases_add"][0].update(
            claim_generation=payload["after_generation"] + 1
        ))
        with self.assertRaisesRegex(ValueError, "fencing is invalid"):
            self.frontier.snapshot()

    def test_randomized_restart_sequence_preserves_partition_and_generation(self):
        randomizer = random.Random(23)
        child_counter = 10
        previous_generation = 0
        for step in range(80):
            self.frontier = PersistentLiveStateFrontier(
                self.temporary.name,
                max_states=128,
                lease_ttl=5.0,
            )
            snapshot = self.frontier.snapshot()
            identities = [
                *snapshot.ready,
                *snapshot.done,
                *(lease.checkpoint_id for lease in snapshot.leases),
            ]
            self.assertEqual(len(identities), len(set(identities)))
            self.assertIn(ROOT_CHECKPOINT, identities)
            self.assertGreaterEqual(snapshot.generation, previous_generation)
            previous_generation = snapshot.generation

            actions = []
            if snapshot.ready:
                actions.append("claim")
            if snapshot.leases:
                actions.extend(("heartbeat", "abandon", "complete"))
            if not actions:
                break
            action = randomizer.choice(actions)
            if action == "claim":
                self.frontier.claim(
                    snapshot.ready[0],
                    expected_generation=snapshot.generation,
                    search=selected_search(snapshot.search),
                    owner=f"worker-{step % 3}",
                    worker=step % 3,
                    now=float(step),
                )
            else:
                lease = randomizer.choice(snapshot.leases)
                if action == "heartbeat":
                    self.frontier.heartbeat(lease, now=float(step))
                elif action == "abandon":
                    self.frontier.abandon(lease, now=float(step))
                else:
                    children = []
                    if child_counter < 60 and randomizer.random() < 0.7:
                        children.append(f"{child_counter:064x}")
                        child_counter += 1
                    self.frontier.complete(
                        lease,
                        children,
                        expected_generation=snapshot.generation,
                        search=snapshot.search,
                        now=float(step),
                    )

        final = self.frontier.snapshot()
        final_ids = [
            *final.ready,
            *final.done,
            *(lease.checkpoint_id for lease in final.leases),
        ]
        self.assertEqual(len(final_ids), len(set(final_ids)))


if __name__ == "__main__":
    unittest.main()
