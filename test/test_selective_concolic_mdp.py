#!/usr/bin/env python3
# RUN: python3 %s

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmark"))

from check_selective_concolic_mdp_oracles import run_oracle  # noqa: E402


class SelectiveConcolicMdpOracleTests(unittest.TestCase):
    def test_finite_partition_and_analytic_mdp_oracles(self) -> None:
        result = run_oracle()
        self.assertTrue(result["all_passed"])
        self.assertEqual(result["false_sat"], 0)
        self.assertEqual(result["false_unsat"], 0)
        self.assertEqual(result["candidate_hits"], 16)
        self.assertEqual(result["partial_models"], 1)
        self.assertAlmostEqual(sum(result["laplace_probabilities"]), 1.0)
        self.assertAlmostEqual(
            result["cycle_value"], result["cycle_closed_form"], places=11
        )

    def test_cli_output_is_canonical_and_reproducible(self) -> None:
        script = ROOT / "benchmark" / "check_selective_concolic_mdp_oracles.py"
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "oracle.json"
            first = subprocess.run(
                [sys.executable, str(script), "--output", str(output)],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            second = subprocess.run(
                [sys.executable, str(script)],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            written = output.read_text(encoding="ascii")
        self.assertEqual(first, second)
        self.assertEqual(first, written)
        self.assertTrue(json.loads(first)["all_passed"])


if __name__ == "__main__":
    unittest.main()
