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
from smt_sequence_training import (  # noqa: E402
    DIMENSION,
    FEATURE_SCHEMA,
    LEGACY_DIMENSION,
    LEGACY_FEATURE_SCHEMA,
    build_sequence_policy,
    context_feature_vector,
    load_sequence_policy,
    verify_sequence_policy,
    write_sequence_policy,
)


def event(
    group: int,
    action: str,
    reward: float,
    *,
    killed: bool = False,
) -> TrajectoryEvent:
    context = {
        "input_bytes": 8 if group == 0 else 65536,
        "difficulty": float(group),
        "timeout_ratio": float(group),
        "solver_unknown_ratio": float(group),
        "dependency_bytes": 0 if group == 0 else 65536,
        "data_quality": 1.0 - float(group),
        "branch_pressure": float(group),
        "targeted": bool(group),
        "timed_out": killed,
    }
    return TrajectoryEvent(
        1, 0.0, action, 0.5, reward, reward, 0.1,
        1, 1, 1, 4, 0.0, killed, context)


def training_events() -> list[TrajectoryEvent]:
    events = []
    for group in (0, 1):
        for index in range(12):
            action = "fast-exact" if index % 2 == 0 else "exact"
            good = (
                (group == 0 and action == "fast-exact")
                or (group == 1 and action == "exact")
            )
            events.append(event(group, action, 0.9 if good else 0.1))
    return events


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


class SmtSequenceTrainingTests(unittest.TestCase):
    def test_v2_query_ir_features_are_bounded_and_scale_aware(self):
        vector = context_feature_vector({
            "input_bytes": 32,
            "data_quality": 0.75,
            "query_ir_queries": 2,
            "query_ir_nodes": 200,
            "query_ir_input_bytes": 8,
            "query_ir_max_bits": 64,
            "query_ir_comparison_ops": 50,
            "query_ir_nonlinear_ops": 10,
            "query_ir_bitwise_ops": 20,
            "query_ir_structural_ops": 30,
        })
        self.assertEqual(len(vector), DIMENSION)
        self.assertEqual(DIMENSION, 16)
        self.assertTrue(all(0.0 <= value <= 1.0 for value in vector))
        self.assertAlmostEqual(vector[12], 0.25)
        self.assertAlmostEqual(vector[13], 0.05)
        self.assertAlmostEqual(vector[14], 0.10)
        self.assertAlmostEqual(vector[15], 0.15)

    def test_v1_artifact_remains_verified_and_accepts_v2_context(self):
        artifact = build_sequence_policy(
            training_events(),
            min_cluster_size=4,
            min_samples=2,
            min_effective_samples=2,
            confidence_z=0.0,
            feature_schema=LEGACY_FEATURE_SCHEMA,
        )
        self.assertEqual(
            artifact["feature_schema"]["schema"], LEGACY_FEATURE_SCHEMA)
        self.assertEqual(
            artifact["feature_schema"]["dimension"], LEGACY_DIMENSION)
        self.assertTrue(verify_sequence_policy(artifact))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "legacy.json")
            write_sequence_policy(path, artifact)
            prior = load_sequence_policy(path)
            self.assertIsNotNone(prior)
            self.assertEqual(prior.dimension, LEGACY_DIMENSION)
            vector = SMTAlgorithmScheduler.context(
                SolverTelemetry(
                    input_bytes=8,
                    query_exports=1,
                    query_ir_nodes=40,
                    query_ir_input_bytes=4,
                    query_ir_max_bits=32,
                ))
            self.assertEqual(len(vector), DIMENSION)
            recommendation, cluster = prior.recommend(vector)
            self.assertIn(recommendation, {"exact", "fast-exact"})
            self.assertIsNotNone(cluster)
            self.assertEqual(prior.feature_schema, LEGACY_FEATURE_SCHEMA)
            self.assertNotEqual(FEATURE_SCHEMA, LEGACY_FEATURE_SCHEMA)

    def test_xmeans_finds_context_specific_sequences_deterministically(self):
        first = build_sequence_policy(
            training_events(),
            min_cluster_size=4,
            min_samples=2,
            min_effective_samples=2,
            confidence_z=0.0,
        )
        second = build_sequence_policy(
            training_events(),
            min_cluster_size=4,
            min_samples=2,
            min_effective_samples=2,
            confidence_z=0.0,
        )
        self.assertEqual(first, second)
        self.assertEqual(first["xmeans"]["clusters"], 2)
        self.assertEqual(
            {cluster["recommended_sequence"] for cluster in first["clusters"]},
            {"fast-exact", "exact"},
        )
        self.assertTrue(verify_sequence_policy(first))

    def test_bic_rejects_a_degenerate_split(self):
        events = [event(0, "exact", 0.5) for _ in range(16)]
        artifact = build_sequence_policy(
            events, min_cluster_size=4, min_samples=2,
            min_effective_samples=2, confidence_z=0.0)
        self.assertEqual(artifact["xmeans"]["clusters"], 1)
        self.assertEqual(artifact["xmeans"]["split_trials"], [])

    def test_timeout_is_censored_and_charged_as_par2(self):
        events = [event(0, "exact", 1.0, killed=True) for _ in range(4)]
        for item in events:
            item.context["budget_sec"] = 5.0
        artifact = build_sequence_policy(
            events,
            timeout_sec=10.0,
            min_samples=2,
            min_effective_samples=2,
            confidence_z=0.0,
        )
        estimate = artifact["clusters"][0]["estimates"]["exact"]
        self.assertEqual(estimate["censored"], 4)
        self.assertEqual(estimate["mean_par2_cost"], 10.0)
        self.assertLess(estimate["local_utility"], 0.0)

    def test_unsupported_cluster_emits_no_recommendation(self):
        artifact = build_sequence_policy(
            [event(0, "exact", 1.0), event(0, "fast-exact", 0.0)],
            min_samples=4,
            min_effective_samples=4,
            confidence_z=0.0,
        )
        self.assertEqual(
            artifact["clusters"][0]["recommended_sequence"], "")

    def test_digest_and_rehashed_structural_tampering_are_rejected(self):
        artifact = build_sequence_policy(
            training_events(),
            min_cluster_size=4,
            min_samples=2,
            min_effective_samples=2,
            confidence_z=0.0,
        )
        digest_tamper = copy.deepcopy(artifact)
        digest_tamper["clusters"][0]["recommended_sequence"] = "exact"
        self.assertFalse(verify_sequence_policy(digest_tamper))

        structural_tamper = copy.deepcopy(artifact)
        structural_tamper["xmeans"]["final_bic"] += 1.0
        rehash(structural_tamper)
        self.assertFalse(verify_sequence_policy(structural_tamper))

    def test_verified_policy_is_only_a_scheduler_preference(self):
        events = (
            [event(0, "polyhedral-exact", 0.9) for _ in range(4)]
            + [event(0, "exact", 0.1) for _ in range(4)]
        )
        artifact = build_sequence_policy(
            events,
            min_samples=2,
            min_effective_samples=2,
            confidence_z=0.0,
        )
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = os.path.join(tmp, "prior.json")
            write_sequence_policy(policy_path, artifact)
            prior = load_sequence_policy(
                policy_path,
                allowed_actions=("exact", "polyhedral-exact"),
            )
            self.assertIsNotNone(prior)
            scheduler = SMTAlgorithmScheduler(
                None, prior_path=policy_path, seed=1)

            class ExploitRng:
                def random(self):
                    return 0.0

            scheduler.rng = ExploitRng()
            assignment = scheduler.select("seed", input_bytes=8)
            self.assertEqual(assignment.sequence, "polyhedral-exact")
            self.assertEqual(
                assignment.overrides["SYMCC_ALGORITHM_PRIOR_SHA256"],
                artifact["artifact_sha256"],
            )
            self.assertAlmostEqual(
                assignment.propensity,
                0.90 + 0.10 / len(scheduler.sequences),
            )

            damaged_path = os.path.join(tmp, "damaged.json")
            damaged = copy.deepcopy(artifact)
            damaged["clusters"][0]["sse"] += 1.0
            with open(damaged_path, "w", encoding="utf-8") as stream:
                json.dump(damaged, stream)
            self.assertIsNone(
                SMTAlgorithmScheduler(None, prior_path=damaged_path).prior)

    def test_cli_trains_and_verifies_the_same_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            trajectory = os.path.join(tmp, "trajectory.jsonl")
            output = os.path.join(tmp, "prior.json")
            with open(trajectory, "w", encoding="utf-8") as stream:
                for item in training_events():
                    stream.write(json.dumps(item.__dict__) + "\n")
            trained = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "util" / "smt_sequence_training.py"),
                    trajectory,
                    "--output", output,
                    "--min-cluster-size", "4",
                    "--min-samples", "2",
                    "--min-effective-samples", "2",
                    "--confidence-z", "0",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            report = json.loads(trained.stdout)
            self.assertTrue(report["verified"])
            verified = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "util" / "smt_sequence_training.py"),
                    "--verify", output,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertTrue(json.loads(verified.stdout)["verified"])


if __name__ == "__main__":
    unittest.main()
