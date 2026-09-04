#!/usr/bin/env python3
# RUN: python3 %s

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from adaptive_components import (  # noqa: E402
    AdaptiveParallelismController,
    ComponentPortfolio,
)


class ComponentPortfolioTests(unittest.TestCase):
    def test_factorized_choices_hot_switch_and_learn(self):
        policy = ComponentPortfolio(switch_interval=10.0, seed=7)
        first = policy.select(now=10.0)
        self.assertEqual(set(first), {"seed", "splitter", "solver", "replay"})
        self.assertEqual(policy.select(now=15.0), first)
        policy.observe(first, reward=1.0, elapsed=0.2, killed=False)
        second = policy.select(now=30.0, force=True)
        self.assertEqual(set(second), set(first))
        for family, name in first.items():
            self.assertEqual(policy.arms[family][name].pulls, 1)

    def test_persistence_ignores_unknown_arms(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "components.json")
            policy = ComponentPortfolio(path, switch_interval=0.0, seed=1)
            choices = policy.select(now=1.0)
            policy.observe(choices, reward=0.7, elapsed=0.5, killed=False)
            policy.save()
            raw = json.loads(Path(path).read_text())
            raw["families"]["seed"]["unknown"] = {"pulls": 100}
            Path(path).write_text(json.dumps(raw))
            restored = ComponentPortfolio(path, seed=2)
            self.assertEqual(restored.observations, 1)
            self.assertNotIn("unknown", restored.arms["seed"])


class ParallelismTests(unittest.TestCase):
    @staticmethod
    def observe(controller, reward=1.0, killed=False):
        controller.observe(
            reward=reward,
            coverage_delta=int(reward > 0),
            generated=10,
            interesting=int(reward > 0),
            elapsed=1.0,
            killed=killed,
        )

    def test_scales_up_under_queue_pressure(self):
        ctrl = AdaptiveParallelismController(
            1, 8, initial=2, interval=1.0, step=2)
        self.observe(ctrl)
        self.assertEqual(
            ctrl.recommend(queue_depth=20, busy_workers=2, now=2.0), 4)

    def test_scales_down_when_starved(self):
        ctrl = AdaptiveParallelismController(
            1, 8, initial=4, interval=1.0, step=1)
        self.observe(ctrl)
        self.assertEqual(
            ctrl.recommend(queue_depth=0, busy_workers=1, now=2.0), 3)

    def test_reverses_on_marginal_utility_regression(self):
        ctrl = AdaptiveParallelismController(
            1, 8, initial=2, interval=1.0, step=1)
        self.observe(ctrl, reward=1.0)
        self.assertEqual(
            ctrl.recommend(queue_depth=10, busy_workers=2, now=2.0), 3)
        for _ in range(3):
            self.observe(ctrl, reward=0.0, killed=True)
        self.assertEqual(
            ctrl.recommend(queue_depth=10, busy_workers=3, now=4.0), 2)
        self.assertEqual(ctrl.snapshot()["experiment"]["phase"], "confirm")

    def test_switchback_interpolation_separates_decay_from_scale_gain(self):
        ctrl = AdaptiveParallelismController(
            1, 4, initial=2, interval=1.0, step=1)
        self.observe(ctrl, reward=2.0)
        self.assertEqual(
            ctrl.recommend(queue_depth=10, busy_workers=2, now=1.0), 3)

        # Absolute yield has already decayed relative to a linear 3/2 gain.
        # The controller returns to two workers before deciding.
        self.observe(ctrl, reward=2.25)
        self.assertEqual(
            ctrl.recommend(queue_depth=10, busy_workers=3, now=2.0), 2)
        self.observe(ctrl, reward=1.0)
        self.assertEqual(
            ctrl.recommend(queue_depth=10, busy_workers=2, now=3.0), 3)

        snapshot = ctrl.snapshot()
        self.assertEqual(snapshot["completed_experiments"], 1)
        self.assertEqual(snapshot["accepted_experiments"], 1)
        self.assertIsNone(snapshot["experiment"])
        self.assertEqual(set(snapshot["levels"]), {"2", "3"})

    def test_maximum_scale_starts_with_downward_efficiency_probe(self):
        ctrl = AdaptiveParallelismController(
            1, 8, initial=8, interval=1.0, step=2)
        self.observe(ctrl, reward=1.0)
        self.assertEqual(
            ctrl.recommend(queue_depth=10, busy_workers=8, now=1.0), 6)

    def test_old_dispatch_cohort_cannot_contaminate_trial_window(self):
        ctrl = AdaptiveParallelismController(
            1, 4, initial=2, interval=1.0, step=1)
        self.observe(ctrl, reward=1.0)
        self.assertEqual(
            ctrl.recommend(queue_depth=10, busy_workers=2, now=1.0), 3)
        self.assertEqual(ctrl.cohort, 1)

        ctrl.observe(
            cohort=0,
            reward=100.0,
            coverage_delta=100,
            generated=100,
            interesting=100,
            elapsed=1.0,
            killed=False,
        )
        ctrl.observe(
            cohort=1,
            reward=0.1,
            coverage_delta=0,
            generated=10,
            interesting=0,
            elapsed=1.0,
            killed=False,
        )
        self.assertEqual(ctrl.window.completed, 1)
        self.assertEqual(ctrl.snapshot()["stale_observations"], 1)

    def test_parallel_objective_does_not_mix_raw_feature_count_units(self):
        left = AdaptiveParallelismController(
            1, 2, initial=2, interval=1.0, step=1)
        right = AdaptiveParallelismController(
            1, 2, initial=2, interval=1.0, step=1)
        left.observe(
            reward=0.5, coverage_delta=1, generated=10,
            interesting=1, elapsed=1.0, killed=False)
        right.observe(
            reward=0.5, coverage_delta=10_000, generated=10,
            interesting=10_000, elapsed=1.0, killed=False)
        left.recommend(queue_depth=10, busy_workers=2, now=1.0)
        right.recommend(queue_depth=10, busy_workers=2, now=1.0)
        self.assertEqual(
            left.snapshot()["levels"]["2"]["objective"],
            right.snapshot()["levels"]["2"]["objective"],
        )
        self.assertEqual(
            left.snapshot()["objective_kind"],
            "bounded-hybrid-reward-per-wall-second",
        )


if __name__ == "__main__":
    unittest.main()
