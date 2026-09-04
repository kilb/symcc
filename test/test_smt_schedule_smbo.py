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

from smt_algorithm_scheduler import SMTAlgorithmScheduler  # noqa: E402
from smt_schedule_smbo import (  # noqa: E402
    _expected_improvement,
    build_smbo_schedule_policy,
    load_smbo_schedule_policy,
    verify_smbo_schedule_policy,
    write_smbo_schedule_policy,
)
from smt_sequence_optimizer import (  # noqa: E402
    build_ensemble_sequence_policy,
    write_ensemble_sequence_policy,
)
from offline_policy import TrajectoryEvent  # noqa: E402


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


def ensemble() -> dict:
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
    )


def build() -> dict:
    return build_smbo_schedule_policy(
        ensemble(),
        evaluation_budget=6,
        initial_design=4,
        max_candidates=32,
        max_schedule_length=3,
        slice_fractions=(0.5, 1.0),
        bag_estimators=3,
        boost_rounds=1,
        tree_depth=2,
        min_leaf=2,
        max_thresholds=4,
        exploration=0.0,
        uncertainty_floor=0.01,
        action_uncertainty_z=0.0,
        reward_weight=0.0,
        min_schedule_improvement=0.001,
        seed=11,
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


class SmtScheduleSmboTests(unittest.TestCase):
    def test_bounded_acquisition_trace_is_deterministic(self):
        first = build()
        second = build()
        self.assertEqual(first, second)
        result = first["global_result"]
        self.assertEqual(result["evaluations"], 6)
        self.assertEqual(len(result["initial_design"]), 4)
        self.assertEqual(len(result["acquisition_trace"]), 2)
        self.assertLessEqual(
            result["evaluations"], result["evaluation_budget"])
        self.assertEqual(
            result["posthoc_oracle_evaluations"],
            result["candidate_count"],
        )
        self.assertGreaterEqual(result["simple_regret"], 0.0)
        self.assertTrue(verify_smbo_schedule_policy(first))

    def test_expected_improvement_handles_certainty_and_uncertainty(self):
        self.assertEqual(_expected_improvement(2.0, 0.0, 1.0, 0.0), 0.0)
        self.assertEqual(_expected_improvement(0.5, 0.0, 1.0, 0.1), 0.4)
        self.assertGreater(
            _expected_improvement(1.2, 0.5, 1.0, 0.0), 0.0)

    def test_digest_and_rehashed_acquisition_tampering_are_rejected(self):
        artifact = build()
        digest_tamper = copy.deepcopy(artifact)
        digest_tamper["global_result"]["simple_regret"] += 0.1
        self.assertFalse(verify_smbo_schedule_policy(digest_tamper))

        trace_tamper = copy.deepcopy(artifact)
        trace_tamper["global_result"]["acquisition_trace"][0][
            "expected_improvement"] += 0.1
        rehash(trace_tamper)
        self.assertFalse(verify_smbo_schedule_policy(trace_tamper))

    def test_loader_drives_budgeted_scheduler_with_explicit_kind(self):
        artifact = build()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "smbo.json")
            write_smbo_schedule_policy(path, artifact)
            prior = load_smbo_schedule_policy(
                path, allowed_actions=("exact", "fast-exact"))
            self.assertIsNotNone(prior)
            self.assertEqual(
                prior.kind, "smbo-expected-improvement-sequence")
            expected, _ = prior.budgeted_schedule((
                1.0, 8.0 / (8.0 + 4096.0), 0.0, 0.0, 0.0,
                0.0, 1.0, 0.0, 0.0,
            ))
            scheduler = SMTAlgorithmScheduler(
                None, prior_path=path, seed=3)

            class ExploitRng:
                def random(self):
                    return 0.0

            scheduler.rng = ExploitRng()
            assignment = scheduler.select("seed", input_bytes=8)
            self.assertEqual(assignment.sequence, expected[0][0])
            self.assertEqual(
                assignment.overrides["SYMCC_ALGORITHM_BUDGET_SEC"],
                f"{expected[0][1]:.12g}",
            )
            self.assertEqual(
                scheduler.to_mapping()["prior_kind"],
                "smbo-expected-improvement-sequence",
            )

    def test_cli_trains_and_verifies_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "ensemble.json")
            output = os.path.join(tmp, "smbo.json")
            write_ensemble_sequence_policy(source, ensemble())
            trained = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "util" / "smt_schedule_smbo.py"),
                    source,
                    "--output", output,
                    "--evaluation-budget", "6",
                    "--initial-design", "4",
                    "--max-candidates", "32",
                    "--max-schedule-length", "3",
                    "--slice-fractions", "0.5,1",
                    "--bag-estimators", "3",
                    "--boost-rounds", "1",
                    "--tree-depth", "2",
                    "--min-leaf", "2",
                    "--max-thresholds", "4",
                    "--exploration", "0",
                    "--action-uncertainty-z", "0",
                    "--reward-weight", "0",
                    "--seed", "11",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertTrue(json.loads(trained.stdout)["verified"])
            verified = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "util" / "smt_schedule_smbo.py"),
                    "--verify", output,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertTrue(json.loads(verified.stdout)["verified"])


if __name__ == "__main__":
    unittest.main()
