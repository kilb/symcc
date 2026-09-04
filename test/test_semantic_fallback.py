# RUN: python3 %s

from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from hybrid_feedback import SolverTelemetry  # noqa: E402
from semantic_fallback import SemanticFallbackPlanner  # noqa: E402


class SemanticFallbackTests(unittest.TestCase):
    def test_byte_local_taint_prefers_exact_solve_and_focus(self):
        telemetry = SolverTelemetry(
            generated=2,
            comparison_taints=((11, 101, 4, 8, 11, 1, 1),),
            data_features=((11, 1, 8),),
        )
        planner = SemanticFallbackPlanner(
            None, strategy_count=7, exact_bytes=8, focus_span=32)
        planner.observe(
            "seed", telemetry, coverage_delta=1, interesting_cases=1,
            elapsed=0.2)
        hint = planner.suggest("seed")
        self.assertEqual(hint["strategy"], 0)
        self.assertEqual(hint["s2f_actions"][0], [101, "solve"])
        self.assertEqual(hint["focus_bytes"], "6-13")
        self.assertEqual(hint["route"], "semantic-byte-local")

    def test_timeout_wide_constraint_prefers_sampling(self):
        telemetry = SolverTelemetry(
            generated=0,
            symbolic_branches=10,
            interesting_branches=5,
            solver_time_us=20_000_000,
            z3_solves=4,
            z3_timeouts=2,
            comparison_taints=((22, 202, 8, 0, 127, 1, 1),),
        )
        planner = SemanticFallbackPlanner(None, strategy_count=7)
        planner.observe("seed", telemetry, elapsed=5.0, killed=False)
        hint = planner.suggest("seed")
        self.assertEqual(hint["strategy"], 6)
        self.assertEqual(hint["s2f_actions"][0], [202, "sample"])
        self.assertIn(hint["route"], {
            "semantic-timeout", "semantic-wide-nonlinear"})

    def test_repeated_unproductive_timeouts_become_skip(self):
        telemetry = SolverTelemetry(
            generated=0,
            z3_solves=1,
            z3_timeouts=1,
            comparison_taints=((33, 303, 1, 0, 255, 1, 1),),
        )
        planner = SemanticFallbackPlanner(None, strategy_count=7)
        for _ in range(4):
            planner.observe("seed", telemetry, elapsed=3.0, killed=True)
        hint = planner.suggest("seed")
        self.assertEqual(hint["s2f_actions"][0], [303, "skip"])

    def test_state_persists_branch_summaries(self):
        telemetry = SolverTelemetry(
            generated=1,
            comparison_taints=((44, 404, 2, 4, 5, 1, 1),),
        )
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "semantic.json"
            planner = SemanticFallbackPlanner(str(state), strategy_count=7)
            planner.observe("seed", telemetry, coverage_delta=1)
            planner.save()

            restored = SemanticFallbackPlanner(str(state), strategy_count=7)
            hint = restored.suggest("seed")
            self.assertEqual(hint["s2f_actions"][0][0], 404)
            self.assertEqual(restored.snapshot()["branches"], 1)


if __name__ == "__main__":
    unittest.main()
