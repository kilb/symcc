# RUN: python3 %s

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmark"))

from check_structured_agentic_oracles import run_oracle  # noqa: E402


class StructuredAgenticOracleTests(unittest.TestCase):
    def test_three_arm_oracle_is_task_paired_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_oracle(Path(tmp))
        self.assertTrue(all(result["checks"].values()))
        comparison = result["comparison"]
        self.assertEqual(comparison["task_count"], 3)
        self.assertEqual(comparison["arms"]["online"]["candidate_actions"], 3)
        self.assertEqual(comparison["arms"]["shadow"]["candidate_actions"], 0)
        self.assertEqual(comparison["arms"]["fallback"]["requests"], 0)


if __name__ == "__main__":
    unittest.main()
