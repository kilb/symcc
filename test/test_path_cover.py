# RUN: python3 %s

from pathlib import Path
from types import SimpleNamespace
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from path_cover import (  # noqa: E402
    MinimumPathCoverPlanner,
    enumerate_minimum_path_covers,
)
from structural_tasks import ProgramTaskGraph  # noqa: E402


DIAMOND_GRAPH = """\
#N m:a choose
#ENTRY choose i32_(i32) m:a
#N m:b choose
#N m:c choose
#N m:d choose
#SITE 100 m:a choose br choose.c:2
#BRANCH 100 m:a m:b m:c
#E m:a m:b
#E m:a m:c
#E m:b m:d
#E m:c m:d
"""


class MinimumPathCoverPlannerTests(unittest.TestCase):
    @staticmethod
    def _telemetry(**overrides):
        values = {
            "branch_trace": ((0, 11, 12, 100, 1, 0),),
            "target_branch": 0,
            "target_reached": False,
            "generated": 0,
            "solver_sat": 0,
            "solver_unsat": 0,
            "solver_unknown": 0,
            "z3_timeouts": 0,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_diamond_has_multiple_minimum_path_covers(self):
        planner = MinimumPathCoverPlanner(
            ProgramTaskGraph.from_lines(DIAMOND_GRAPH.splitlines()),
            max_covers=8)

        self.assertTrue(planner.enabled)
        plan = next(iter(planner.plans.values()))
        self.assertEqual(len(plan.components), 4)
        self.assertGreaterEqual(len(plan.covers), 2)
        self.assertEqual({len(cover.paths) for cover in plan.covers}, {2})

    def test_prefix_choices_follow_active_cover_and_penalize_infeasible_edge(self):
        planner = MinimumPathCoverPlanner(
            ProgramTaskGraph.from_lines(DIAMOND_GRAPH.splitlines()),
            max_covers=8)
        planner.observe(
            "seed", self._telemetry(), reward=0.5, coverage_delta=1)

        self.assertIn(11, planner.branch_choices)
        self.assertIn(12, planner.branch_choices)
        before = planner.branch_priority(12, "seed")
        planner.observe(
            "seed",
            self._telemetry(
                target_branch=12,
                target_reached=True,
                solver_unsat=1),
            reward=0.0,
            coverage_delta=0,
        )
        self.assertGreater(planner.choice_failures[12], 0)
        self.assertLess(planner.branch_priority(12, "seed"), before)

    def test_loop_scc_is_collapsed_before_path_cover(self):
        graph = ProgramTaskGraph.from_lines("""\
#N m:a loop
#ENTRY loop i32_(i32) m:a
#N m:b loop
#N m:c loop
#SITE 10 m:b loop br -
#BRANCH 10 m:b m:b m:c
#E m:a m:b
#E m:b m:b
#E m:b m:c
""".splitlines())
        planner = MinimumPathCoverPlanner(graph)
        plan = next(iter(planner.plans.values()))
        self.assertEqual(len(plan.components), 3)
        self.assertTrue(all(
            source != target for source, target in plan.dag_edges))

    def test_iterative_matching_handles_deep_dag_without_recursion(self):
        nodes = tuple(f"n{index:04d}" for index in range(1500))
        adjacency = {
            node: ({nodes[index + 1]} if index + 1 < len(nodes) else set())
            for index, node in enumerate(nodes)
        }
        covers = MinimumPathCoverPlanner._enumerate_covers(adjacency, 2)
        self.assertEqual(len(covers), 1)
        self.assertEqual(covers[0].paths, (nodes,))
        with self.assertRaisesRegex(ValueError, "acyclic"):
            enumerate_minimum_path_covers({"a": {"b"}, "b": {"a"}})

    def test_state_round_trip_preserves_branch_mapping_and_failures(self):
        graph = ProgramTaskGraph.from_lines(DIAMOND_GRAPH.splitlines())
        planner = MinimumPathCoverPlanner(graph)
        planner.observe(
            "seed",
            self._telemetry(
                target_branch=12,
                target_reached=True,
                solver_unsat=1),
            reward=0.0,
            coverage_delta=0,
        )

        restored = MinimumPathCoverPlanner(graph)
        restored.restore(planner.snapshot())
        self.assertIn(12, restored.branch_choices)
        self.assertEqual(restored.choice_failures[12], 1)
        self.assertEqual(restored.infeasible_paths, 1)


if __name__ == "__main__":
    unittest.main()
