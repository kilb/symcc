# RUN: python3 %s

import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from offline_policy import (  # noqa: E402
    ConservativePolicyGate,
    OfflinePolicyController,
    TrajectoryEvent,
    evaluate_trajectory_file,
    interference_adjusted_reward,
    load_trajectories,
)
from hybrid_feedback import SolverTelemetry  # noqa: E402


def event(action: str, reward: float, propensity: float = 0.5) -> TrajectoryEvent:
    return TrajectoryEvent(
        1, 0.0, action, propensity, reward, reward, 0.1,
        1, 1, 1, 4, 0.0, False, {})


class OfflinePolicyTests(unittest.TestCase):
    def test_interference_penalizes_redundant_parallel_generation(self):
        useful, useful_interference = interference_adjusted_reward(
            0.8, elapsed=1.0, generated=10, interesting=10,
            concurrent_workers=16)
        duplicate, duplicate_interference = interference_adjusted_reward(
            0.8, elapsed=1.0, generated=10, interesting=0,
            concurrent_workers=16)
        self.assertEqual(useful_interference, 0.0)
        self.assertGreater(duplicate_interference, 0.5)
        self.assertGreater(useful, duplicate)

    def test_conservative_gate_requires_support_and_improvement(self):
        events = (
            [event("fast", 0.9) for _ in range(40)]
            + [event("exact", 0.1) for _ in range(40)]
        )
        gate = ConservativePolicyGate(
            ("fast", "exact"), min_effective_samples=20)
        approved, estimates, behavior = gate.evaluate(events)
        self.assertEqual(approved, "fast")
        self.assertGreater(estimates["fast"].lower_bound, behavior)

        unsupported = ConservativePolicyGate(
            ("rare",), min_effective_samples=20)
        approved, estimates, _ = unsupported.evaluate(
            [event("rare", 1.0, 0.01)] * 2
            + [event("other", 0.1, 0.99)] * 100)
        self.assertEqual(approved, "")
        self.assertFalse(estimates["rare"].supported)

    def test_controller_records_reloadable_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            trajectory = os.path.join(tmp, "trajectory.jsonl")
            state = os.path.join(tmp, "policy.json")
            controller = OfflinePolicyController(
                trajectory, state, ("fast", "exact"),
                evaluation_interval=8, min_effective_samples=2)
            for _ in range(8):
                controller.observe(
                    action="fast",
                    propensity=0.5,
                    telemetry=None,
                    reward=0.8,
                    elapsed=0.1,
                    coverage_delta=1,
                    generated=1,
                    interesting=1,
                    concurrent_workers=2,
                    killed=False,
                )
            self.assertEqual(len(load_trajectories(trajectory)), 8)
            self.assertTrue(Path(state).is_file())

    def test_trajectory_records_censoring_instance_and_stage_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = OfflinePolicyController(
                os.path.join(tmp, "trajectory.jsonl"),
                os.path.join(tmp, "state.json"),
                ("exact",),
            )
            recorded = controller.observe(
                action="exact",
                propensity=1.0,
                telemetry=SolverTelemetry.from_mapping({
                    "solver_queries": 1,
                    "solver_unknown": 1,
                }),
                reward=0.0,
                elapsed=15.0,
                coverage_delta=0,
                generated=0,
                interesting=0,
                concurrent_workers=1,
                killed=False,
                instance_id="a" * 200,
                budget_sec=15.0,
            )
            self.assertIsNotNone(recorded)
            self.assertTrue(recorded.context["timed_out"])
            self.assertFalse(recorded.context["solved"])
            self.assertEqual(len(recorded.context["instance_id"]), 128)
            self.assertEqual(recorded.context["budget_sec"], 15.0)

    def test_trajectory_records_query_ir_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = OfflinePolicyController(
                os.path.join(tmp, "trajectory.jsonl"),
                os.path.join(tmp, "state.json"),
                ("exact",),
            )
            recorded = controller.observe(
                action="exact",
                propensity=1.0,
                telemetry=SolverTelemetry(
                    query_exports=2,
                    query_ir_nodes=101,
                    query_ir_input_bytes=7,
                    query_ir_max_bits=128,
                    query_ir_comparison_ops=20,
                    query_ir_nonlinear_ops=3,
                    query_ir_bitwise_ops=11,
                    query_ir_structural_ops=9,
                ),
                reward=0.2,
                elapsed=0.1,
                coverage_delta=0,
                generated=0,
                interesting=0,
                concurrent_workers=1,
                killed=False,
            )
            self.assertEqual(recorded.context["query_ir_queries"], 2)
            self.assertEqual(recorded.context["query_ir_nodes"], 101)
            self.assertEqual(recorded.context["query_ir_max_bits"], 128)
            self.assertEqual(
                recorded.context["query_ir_nonlinear_ops"], 3)

    def test_trajectory_cli_outputs_conservative_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            trajectory = os.path.join(tmp, "trajectory.jsonl")
            with open(trajectory, "w", encoding="utf-8") as stream:
                for item in [event("fast", 0.7, 0.5) for _ in range(4)]:
                    stream.write(json.dumps(item.__dict__) + "\n")
                for item in [event("exact", 0.1, 0.5) for _ in range(4)]:
                    stream.write(json.dumps(item.__dict__) + "\n")

            report = evaluate_trajectory_file(
                trajectory, min_effective_samples=2, min_improvement=0.0)
            self.assertEqual(report["events"], 8)
            self.assertIn("fast", report["estimates"])

            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "util" / "offline_policy.py"),
                    trajectory,
                    "--min-effective-samples", "2",
                    "--min-improvement", "0",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            cli_report = json.loads(result.stdout)
            self.assertEqual(cli_report["events"], 8)
            self.assertIn(cli_report["approved_action"], {"", "fast"})


if __name__ == "__main__":
    unittest.main()
