# RUN: python3 %s

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from expressive_coverage import ExpressiveCoverageTree  # noqa: E402
from hybrid_feedback import SolverTelemetry  # noqa: E402
from structural_tasks import ProgramTaskGraph  # noqa: E402


GRAPH = ProgramTaskGraph.from_lines("""\
#N m:a parser
#SITE 10 m:a parser br parser.c:7
#BRANCH 10 m:a m:b m:c
#N m:d lexer
#SITE 20 m:d lexer switch parser.c:12
#SWITCH 20 m:d m:e 2 40:m:f 41:m:g
""".splitlines())


class ExpressiveCoverageTreeTests(unittest.TestCase):
    @staticmethod
    def telemetry(target=0, reached=False):
        return SolverTelemetry.from_mapping({
            "input_bytes": 32,
            "symbolic_branches": 3,
            "solver_queries": 1,
            "solver_sat": int(reached),
            "solver_time_us": 1000,
            "generated": int(reached),
            "target_branch": target,
            "target_reached": reached,
            "open_branches": [102, 202],
            "branch_trace": [
                [0, 101, 102, 10, 1, 1],
                [101, 201, 202, 20, 0, 1],
                [201, 301, 302, 20, 0, 0],
            ],
            "comparison_taints": [
                [10, 101, 2, 4, 5, 1, 1],
                [20, 201, 1, 9, 9, 0, 1],
                [20, 301, 1, 17, 17, 0, 0],
            ],
            "data_features": [[77, 6, 8]],
        })

    def test_builds_contextual_tree_and_compresses_repeated_shapes(self):
        tree = ExpressiveCoverageTree(GRAPH, max_nodes=128)
        duplicates = tree.observe(
            "seed", self.telemetry(),
            reward=0.6, coverage_delta=2, interesting_cases=1,
            elapsed=0.1, killed=False, now=10.0)

        self.assertEqual(duplicates, 1)
        self.assertEqual(tree.filtered_duplicates, 1)
        self.assertGreaterEqual(tree.loop_compressions, 1)
        switch = tree.nodes[tree.branch_nodes[201]]
        self.assertEqual(switch.branch_type, "switch")
        self.assertEqual(switch.location, "parser.c:12")
        self.assertTrue(switch.function.endswith("::lexer"))
        self.assertEqual(switch.dependency_lo, 9)
        self.assertEqual(switch.dependency_hi, 9)
        self.assertGreater(tree.priority(202), 0.0)

    def test_round_trip_and_atomic_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = os.path.join(tmp, "ect.json")
            tree = ExpressiveCoverageTree(
                GRAPH, max_nodes=128, export_path=output)
            tree.observe(
                "seed", self.telemetry(target=202, reached=True),
                reward=0.8, coverage_delta=3, interesting_cases=1,
                elapsed=0.2, killed=False, now=10.0)
            self.assertTrue(tree.export())
            raw = json.loads(Path(output).read_text())
            self.assertEqual(raw["kind"], "symcc-expressive-coverage-tree")

            restored = ExpressiveCoverageTree(GRAPH, max_nodes=128)
            restored.restore(raw)
            self.assertEqual(restored.observations, 1)
            self.assertIn(202, restored.branch_nodes)
            node = restored.nodes[restored.branch_nodes[202]]
            self.assertEqual(node.status, "sat")


if __name__ == "__main__":
    unittest.main()
