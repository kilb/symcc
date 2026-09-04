# RUN: python3 %s

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from live_state_scheduler import LiveState, LiveStateScheduler  # noqa: E402


class LiveStateSchedulerTests(unittest.TestCase):
    def test_multi_objective_selection_and_feedback(self):
        scheduler = LiveStateScheduler(max_states=8, exploration=0.2)
        scheduler.upsert({"state_id": "cheap", "target_distance": 1,
                          "constraint_cost": 1, "novelty": .8,
                          "data_novelty": .7, "pending_branches": 3})
        scheduler.upsert({"state_id": "deep", "depth": 100,
                          "target_distance": 20, "constraint_cost": 50,
                          "novelty": .1, "data_novelty": .1})
        selected = scheduler.select(1)
        self.assertEqual([state.state_id for state in selected], ["cheap"])
        scheduler.observe("cheap", reward=1, elapsed=.1)
        self.assertEqual(scheduler.snapshot()["stats"]["cheap"]["pulls"], 1)

    def test_bounds_and_worker_filter(self):
        scheduler = LiveStateScheduler(max_states=2)
        scheduler.upsert({"state_id": "a", "worker": 1})
        scheduler.upsert({"state_id": "b", "worker": 2})
        scheduler.upsert({"state_id": "c", "worker": 1, "novelty": 1})
        self.assertEqual(
            {state.state_id for state in scheduler.select(2, worker=1)}, {"c"})
        with self.assertRaises(ValueError):
            LiveState.from_mapping({"state_id": "x", "novelty": float("nan")})


if __name__ == "__main__":
    unittest.main()
