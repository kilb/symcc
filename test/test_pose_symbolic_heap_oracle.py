#!/usr/bin/env python3
# RUN: env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -p no:cacheprovider %s
"""Regression test for the sealed F456 finite-domain oracle."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmark"))

from check_pose_symbolic_heap_oracles import run  # noqa: E402
from benchmark_pose_symbolic_heap import run_benchmark  # noqa: E402


class PoseSymbolicHeapOracleTests(unittest.TestCase):
    def test_all_formal_and_path_optimality_checks_pass(self) -> None:
        result = run()
        self.assertEqual(len(result["checks"]), 9)
        self.assertTrue(all(result["checks"].values()))
        self.assertEqual(result["exhaustive_alias"]["assignments"], 27)
        self.assertEqual(
            result["exhaustive_alias"]["read_matches"], 27
        )
        self.assertEqual(
            result["exhaustive_alias"]["store_matches"], 27
        )
        self.assertEqual(result["cases"]["swap"]["cfg_paths"], 2)
        self.assertEqual(result["cases"]["sum"]["cfg_paths"], 1)
        self.assertEqual(
            result["cases"]["bounded_list_max10"]["cfg_paths"], 12
        )

    def test_mechanism_benchmark_preserves_nonforking_contract(self) -> None:
        result = run_benchmark(repeats=1, sizes=(1, 2, 4))
        self.assertEqual(
            [row["alias_partition_models"] for row in result["rows"]],
            [2, 5, 52],
        )
        self.assertTrue(all(row["cfg_forks"] == 0 for row in result["rows"]))


if __name__ == "__main__":
    unittest.main()
