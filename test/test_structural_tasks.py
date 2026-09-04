# RUN: python3 %s

from pathlib import Path
from types import SimpleNamespace
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from merge_directed_distance import merge_lines  # noqa: E402
from structural_tasks import (  # noqa: E402
    DynamicStructuralTaskAllocator,
    ProgramTaskGraph,
)


GRAPH_LINES = """\
# symcc-structural-task-graph-v1 module=a.c
#N m1:a foo
#ENTRY foo i32_(i32) m1:a
#N m1:b foo
#SITE 10 m1:b foo br a.c:3
#SWITCH 10 m1:b m1:d 2 7:m1:c 9:m1:e
#E m1:a m1:b
#X m1:b bar
# symcc-structural-task-graph-v1 module=b.c
#N m2:a bar
#ENTRY bar i32_(i32) m2:a
#N m2:b bar
#SITE 20 m2:b bar br b.c:4
#E m2:a m2:b
"""


class ProgramTaskGraphTests(unittest.TestCase):
    def test_cross_module_calls_form_structural_regions(self):
        graph = ProgramTaskGraph.from_lines(GRAPH_LINES.splitlines())

        self.assertTrue(graph.enabled)
        self.assertEqual(len(graph.regions), 2)
        self.assertNotEqual(graph.site_regions[10], graph.site_regions[20])
        self.assertEqual(graph.site_opcodes[10], "br")
        self.assertEqual(graph.site_locations[10], "a.c:3")
        self.assertEqual(
            graph.switch_successors[10],
            (("default", "m1:d"), ("7", "m1:c"), ("9", "m1:e")),
        )
        self.assertTrue(any(source.endswith("::foo") and target.endswith("::bar")
                            for source, target in graph.call_edges))

    def test_recursive_functions_collapse_to_one_region(self):
        graph = ProgramTaskGraph.from_lines("""\
#N m:a left
#N m:b right
#SITE 1 m:a left br -
#SITE 2 m:b right br -
#E m:a m:b
#E m:b m:a
""".splitlines())
        self.assertEqual(graph.site_regions[1], graph.site_regions[2])
        region = graph.regions[graph.site_regions[1]]
        self.assertEqual(len(region.functions), 2)

    def test_merger_preserves_task_graph_without_directed_targets(self):
        merged = merge_lines(GRAPH_LINES.splitlines(True))
        self.assertTrue(any(line.startswith("#SITE 10 ") for line in merged))
        self.assertTrue(any(line.startswith("#ENTRY foo ") for line in merged))


class DynamicStructuralTaskAllocatorTests(unittest.TestCase):
    @staticmethod
    def _telemetry(site, actual, opposite):
        return SimpleNamespace(
            branch_trace=((0, actual, opposite, site, 1, 0),))

    def test_feedback_maps_targets_and_assigns_exclusive_owners(self):
        graph = ProgramTaskGraph.from_lines(GRAPH_LINES.splitlines())
        allocator = DynamicStructuralTaskAllocator(
            graph, rebalance_interval=1.0)
        allocator.observe(
            "seed-a", self._telemetry(10, 11, 12),
            reward=0.7, coverage_delta=2, interesting_cases=1,
            elapsed=1.0, now=10.0)
        allocator.observe(
            "seed-b", self._telemetry(20, 21, 22),
            reward=0.2, coverage_delta=0, interesting_cases=0,
            elapsed=2.0, now=11.0)

        self.assertTrue(allocator.rebalance([1, 2], now=20.0, force=True))
        self.assertEqual(
            set(allocator.worker_regions[1])
            | set(allocator.worker_regions[2]),
            set(graph.regions),
        )
        self.assertTrue(
            set(allocator.worker_regions[1]).isdisjoint(
                allocator.worker_regions[2]))

        owner = next(
            worker for worker in (1, 2)
            if allocator.accepts(worker, "seed-a", 12))
        other = 1 if owner == 2 else 2
        self.assertFalse(allocator.accepts(other, "seed-a", 12))
        items = [("seed-a", None, 12), ("seed-b", None, 22)]
        self.assertEqual(
            allocator.select_index(
                owner, items, 0, active_worker_count=2),
            0,
        )

    def test_feedback_state_round_trip(self):
        graph = ProgramTaskGraph.from_lines(GRAPH_LINES.splitlines())
        allocator = DynamicStructuralTaskAllocator(graph)
        allocator.observe(
            "seed-a", self._telemetry(10, 11, 12),
            reward=0.5, coverage_delta=1, interesting_cases=1,
            elapsed=0.5, now=10.0)

        restored = DynamicStructuralTaskAllocator(graph)
        restored.restore(allocator.snapshot())
        region = graph.site_regions[10]
        self.assertEqual(restored.path_regions["seed-a"], (region,))
        self.assertEqual(restored.branch_regions[12], region)
        self.assertEqual(restored.feedback[region].visits, 1)


if __name__ == "__main__":
    unittest.main()
