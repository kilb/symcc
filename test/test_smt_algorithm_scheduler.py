# RUN: python3 %s

from pathlib import Path
import json
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from hybrid_feedback import SolverTelemetry  # noqa: E402
from smt_algorithm_scheduler import SMTAlgorithmScheduler  # noqa: E402


class SMTAlgorithmSchedulerTests(unittest.TestCase):
    def test_query_ir_structure_enters_context(self):
        telemetry = SolverTelemetry(
            input_bytes=16,
            query_exports=2,
            query_ir_nodes=200,
            query_ir_input_bytes=8,
            query_ir_max_bits=64,
            query_ir_comparison_ops=50,
            query_ir_nonlinear_ops=10,
            query_ir_bitwise_ops=20,
            query_ir_structural_ops=30,
        )
        vector = SMTAlgorithmScheduler.context(telemetry)
        self.assertEqual(len(vector), SMTAlgorithmScheduler.DIMENSION)
        self.assertEqual(len(vector), 16)
        self.assertEqual(vector[12:], (0.25, 0.05, 0.10, 0.15))

    def test_legacy_nine_dimensional_state_is_zero_extended(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(json.dumps({
                "schema": 1,
                "observations": 3,
                "sequence_number": 4,
                "clusters": [{
                    "centroid": [1.0] + [0.25] * 8,
                    "m2": [0.0] * 9,
                    "samples": 3,
                }],
                "arms": {},
            }), encoding="utf-8")
            scheduler = SMTAlgorithmScheduler(str(path), seed=1)
            self.assertEqual(len(scheduler.clusters), 1)
            self.assertEqual(len(scheduler.clusters[0].centroid), 16)
            self.assertEqual(scheduler.clusters[0].centroid[9:], [0.0] * 7)
            self.assertEqual(scheduler.observations, 3)

    def test_sequence_advances_across_seed_replays(self):
        with tempfile.TemporaryDirectory() as tmp:
            scheduler = SMTAlgorithmScheduler(
                str(Path(tmp) / "state.json"), seed=1)
            # Cold-start enumeration selects exact first.
            exact = scheduler.select("a", input_bytes=32)
            self.assertEqual(exact.sequence, "exact")
            scheduler.observe(
                exact.token,
                path="a",
                telemetry=SolverTelemetry(input_bytes=32),
                reward=0.1,
                elapsed=0.1,
                killed=False,
            )
            # The next cold arm is a two-stage sequence and must continue on
            # the following replay instead of re-running stage zero.
            first = scheduler.select("b", input_bytes=32)
            second = scheduler.select("b", input_bytes=32)
            self.assertEqual(first.sequence, "fast-exact")
            self.assertEqual(first.stage_index, 0)
            self.assertEqual(second.sequence, first.sequence)
            self.assertEqual(second.stage_index, 1)
            self.assertNotEqual(first.token, second.token)

    def test_abandon_rewinds_undispatched_stage_but_discard_does_not(self):
        scheduler = SMTAlgorithmScheduler(None, seed=1)
        first = scheduler.select("seed", input_bytes=32)
        self.assertTrue(scheduler.abandon(first.token))
        self.assertNotIn(first.token, scheduler.pending)
        retry = scheduler.select("seed", input_bytes=32)
        self.assertEqual(
            (retry.sequence, retry.stage_index),
            (first.sequence, first.stage_index),
        )

        active_after_dispatch = dict(scheduler.active_sequences)
        self.assertTrue(scheduler.discard(retry.token))
        self.assertEqual(scheduler.active_sequences, active_after_dispatch)
        self.assertFalse(scheduler.observe(
            retry.token,
            path="seed",
            telemetry=None,
            reward=1.0,
            elapsed=0.1,
            killed=False,
        ))

    def test_lifo_abandon_restores_sequence_number_and_token(self):
        scheduler = SMTAlgorithmScheduler(None, seed=19)
        first = scheduler.select("seed-a", input_bytes=8)
        second = scheduler.select("seed-b", input_bytes=8)
        self.assertEqual(scheduler.sequence_number, 2)

        self.assertTrue(scheduler.abandon(second.token))
        self.assertEqual(scheduler.sequence_number, 1)
        self.assertTrue(scheduler.abandon(first.token))
        self.assertEqual(scheduler.sequence_number, 0)

        replayed = scheduler.select("seed-a", input_bytes=8)
        self.assertEqual(replayed.token, first.token)
        self.assertEqual(
            (replayed.sequence, replayed.stage_index),
            (first.sequence, first.stage_index),
        )

    def test_timeout_is_charged_as_par2_cost(self):
        scheduler = SMTAlgorithmScheduler(None, timeout_sec=10.0, seed=2)
        assignment = scheduler.select("seed", input_bytes=8)
        telemetry = SolverTelemetry.from_mapping({
            "input_bytes": 8,
            "solver_queries": 1,
            "solver_unknown": 1,
        })
        self.assertTrue(scheduler.observe(
            assignment.token,
            path="seed",
            telemetry=telemetry,
            reward=1.0,
            elapsed=1.0,
            killed=False,
        ))
        arm = scheduler.arms[f"0:{assignment.sequence}"]
        self.assertEqual(arm.cost_sum, 20.0)
        self.assertEqual(arm.timeouts, 1)
        self.assertLess(arm.mean, 1.0)

    def test_state_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "state.json")
            scheduler = SMTAlgorithmScheduler(path, seed=3)
            assignment = scheduler.select("seed", input_bytes=64)
            scheduler.observe(
                assignment.token,
                path="seed",
                telemetry=SolverTelemetry(input_bytes=64, generated=1),
                reward=0.5,
                elapsed=0.25,
                killed=False,
            )
            scheduler.save()
            restored = SMTAlgorithmScheduler(path, seed=3)
            self.assertEqual(restored.observations, 1)
            self.assertEqual(len(restored.clusters), 1)
            self.assertEqual(
                restored.arms[f"0:{assignment.sequence}"].observations, 1)

    def test_preferred_sequence_propensity_accounts_for_exploration_hit(self):
        scheduler = SMTAlgorithmScheduler(None, seed=4)

        class ExploreRng:
            def random(self):
                return 0.95

        scheduler.rng = ExploreRng()
        scheduler._select_sequence = lambda cluster: ("exact", 0.25)
        assignment = scheduler.select(
            "seed", input_bytes=16, preferred_sequence="exact")
        self.assertEqual(assignment.sequence, "exact")
        self.assertAlmostEqual(assignment.propensity, 0.925)


if __name__ == "__main__":
    unittest.main()
