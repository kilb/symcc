# RUN: python3 %s

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from offline_policy import TrajectoryEvent  # noqa: E402
from hybrid_feedback import SolverTelemetry  # noqa: E402
from smt_algorithm_scheduler import SMTAlgorithmScheduler  # noqa: E402
from smt_sequence_training import LEGACY_FEATURE_SCHEMA  # noqa: E402
from smt_sequence_optimizer import (  # noqa: E402
    build_ensemble_sequence_policy,
    load_ensemble_sequence_policy,
    verify_ensemble_sequence_policy,
    write_ensemble_sequence_policy,
)


def event(
    group: int,
    action: str,
    *,
    killed: bool,
    index: int,
) -> TrajectoryEvent:
    context = {
        "input_bytes": 8 if group == 0 else 65536,
        "difficulty": float(group),
        "timeout_ratio": float(killed),
        "solver_unknown_ratio": float(killed),
        "dependency_bytes": 0 if group == 0 else 65536,
        "data_quality": 1.0 - float(group),
        "branch_pressure": float(group),
        "targeted": bool(group),
        "timed_out": killed,
        "solved": not killed,
        "instance_id": f"instance-{group}-{index}",
    }
    reward = 0.0 if killed else 0.8
    return TrajectoryEvent(
        1, float(index), action, 0.5, reward, reward,
        1.0 if killed else 0.1, 1, 1, 1, 4, 0.0, killed, context)


def complementary_events() -> list[TrajectoryEvent]:
    events = []
    for group in (0, 1):
        for index in range(16):
            action = "fast-exact" if index % 2 == 0 else "exact"
            killed = (
                (group == 0 and action == "exact")
                or (group == 1 and action == "fast-exact")
            )
            events.append(event(
                group, action, killed=killed, index=index))
    return events


def stochastic_events() -> list[TrajectoryEvent]:
    events = []
    for index in range(32):
        action = "fast-exact" if index % 2 == 0 else "exact"
        # Both actions have a 50% completion rate in one context. A sequential
        # schedule can therefore improve on either single action.
        killed = (index // 2) % 2 == 0
        events.append(event(0, action, killed=killed, index=index))
    return events


def build(
    events: list[TrajectoryEvent],
    *,
    feature_schema: str | None = None,
) -> dict:
    schema = (
        {"feature_schema": feature_schema}
        if feature_schema is not None else {})
    return build_ensemble_sequence_policy(
        events,
        timeout_sec=30.0,
        max_clusters=4,
        min_cluster_size=4,
        min_model_samples=6,
        bag_estimators=3,
        boost_rounds=2,
        tree_depth=2,
        min_leaf=2,
        max_thresholds=4,
        beam_width=32,
        max_schedule_length=2,
        slice_fractions=(0.5, 1.0),
        uncertainty_z=0.0,
        reward_weight=0.0,
        min_schedule_improvement=0.001,
        seed=7,
        **schema,
    )


def rehash(artifact: dict) -> None:
    core = dict(artifact)
    core.pop("artifact_sha256", None)
    artifact["artifact_sha256"] = hashlib.sha256(json.dumps(
        core,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")).hexdigest()


class SmtSequenceOptimizerTests(unittest.TestCase):
    def test_legacy_ensemble_loads_under_v2_scheduler(self):
        artifact = build(
            complementary_events(),
            feature_schema=LEGACY_FEATURE_SCHEMA,
        )
        self.assertTrue(verify_ensemble_sequence_policy(artifact))
        self.assertEqual(artifact["feature_schema"]["dimension"], 9)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "legacy-ensemble.json")
            write_ensemble_sequence_policy(path, artifact)
            prior = load_ensemble_sequence_policy(path)
            self.assertIsNotNone(prior)
            self.assertEqual(prior.dimension, 9)
            schedule, cluster = prior.schedule(
                SMTAlgorithmScheduler.context(
                    SolverTelemetry(
                        input_bytes=8,
                        query_exports=1,
                        query_ir_nodes=32,
                        query_ir_input_bytes=4,
                        query_ir_max_bits=32,
                    )))
            self.assertTrue(schedule)
            self.assertIsNotNone(cluster)

    def test_complementary_actions_produce_deterministic_global_schedule(self):
        first = build(complementary_events())
        second = build(complementary_events())
        self.assertEqual(first, second)
        schedule = first["global_schedule"]
        self.assertTrue(schedule["optimized_multi_action"])
        self.assertEqual(len(schedule["schedule"]), 2)
        self.assertLess(
            schedule["objective"], schedule["best_single_objective"])
        self.assertLessEqual(
            sum(stage["budget_sec"] for stage in schedule["schedule"]), 30.0)
        self.assertTrue(verify_ensemble_sequence_policy(first))

    def test_under_observed_action_is_excluded_from_model(self):
        events = [
            event(0, "exact", killed=False, index=index)
            for index in range(12)
        ]
        events.extend([
            event(0, "rare", killed=False, index=100 + index)
            for index in range(2)
        ])
        artifact = build(events)
        self.assertEqual(artifact["source"]["modeled_actions"], ["exact"])
        self.assertEqual(
            artifact["global_schedule"]["schedule"][0]["action"], "exact")
        self.assertFalse(
            artifact["global_schedule"]["optimized_multi_action"])

    def test_digest_and_rehashed_optimizer_tampering_are_rejected(self):
        artifact = build(complementary_events())
        digest_tamper = copy.deepcopy(artifact)
        digest_tamper["global_schedule"]["objective"] += 1.0
        self.assertFalse(verify_ensemble_sequence_policy(digest_tamper))

        structural_tamper = copy.deepcopy(artifact)
        structural_tamper["models"]["exact"]["completion"][
            "baseline"] += 0.2
        rehash(structural_tamper)
        self.assertFalse(verify_ensemble_sequence_policy(structural_tamper))

    def test_scheduler_executes_budgeted_prior_across_replays(self):
        artifact = build(stochastic_events())
        self.assertTrue(
            artifact["cluster_schedules"][0]["optimized_multi_action"])
        expected = [
            stage["action"]
            for stage in artifact["cluster_schedules"][0]["schedule"]
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ensemble.json")
            write_ensemble_sequence_policy(path, artifact)
            prior = load_ensemble_sequence_policy(
                path, allowed_actions=("exact", "fast-exact"))
            self.assertIsNotNone(prior)
            scheduler = SMTAlgorithmScheduler(
                None, prior_path=path, seed=3)

            class ExploitRng:
                def random(self):
                    return 0.0

            scheduler.rng = ExploitRng()
            assignments = []
            for _ in range(4):
                assignments.append(
                    scheduler.select("seed", input_bytes=8))
                if len({
                    assignment.sequence for assignment in assignments
                }) >= 2:
                    break
            observed = []
            for assignment in assignments:
                if not observed or assignment.sequence != observed[-1]:
                    observed.append(assignment.sequence)
            self.assertEqual(observed[:2], expected)
            first = assignments[0]
            self.assertEqual(
                first.overrides["SYMCC_ALGORITHM_BUDGET_SEC"], "15")
            self.assertEqual(
                first.overrides["SYMCC_ALGORITHM_PRIOR_SCHEDULE_LENGTH"], "2")
            self.assertGreaterEqual(
                scheduler.prior_budgeted_assignments, 2)
            self.assertEqual(scheduler.prior_schedule_completions, 1)

    def test_cli_trains_and_verifies_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            trajectory = os.path.join(tmp, "trajectory.jsonl")
            output = os.path.join(tmp, "ensemble.json")
            with open(trajectory, "w", encoding="utf-8") as stream:
                for item in complementary_events():
                    stream.write(json.dumps(item.__dict__) + "\n")
            trained = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "util" / "smt_sequence_optimizer.py"),
                    trajectory,
                    "--output", output,
                    "--min-cluster-size", "4",
                    "--min-model-samples", "6",
                    "--bag-estimators", "3",
                    "--boost-rounds", "2",
                    "--tree-depth", "2",
                    "--min-leaf", "2",
                    "--max-thresholds", "4",
                    "--max-schedule-length", "2",
                    "--slice-fractions", "0.5,1",
                    "--uncertainty-z", "0",
                    "--reward-weight", "0",
                    "--seed", "7",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertTrue(json.loads(trained.stdout)["verified"])
            verified = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "util" / "smt_sequence_optimizer.py"),
                    "--verify", output,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertTrue(json.loads(verified.stdout)["verified"])


if __name__ == "__main__":
    unittest.main()
