# RUN: python3 %s

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from benchmark_statistics import paired_report, summarize  # noqa: E402


class BenchmarkStatisticsTests(unittest.TestCase):
    def test_reproducible_paired_report(self):
        first = paired_report([1, 2, 3, 4], [2, 3, 3, 6], seed=7, samples=100)
        second = paired_report([1, 2, 3, 4], [2, 3, 3, 6], seed=7, samples=100)
        self.assertEqual(first, second)
        self.assertEqual(first["improved_runs"], 3)
        self.assertEqual(first["regressed_runs"], 0)

    def test_invalid_inputs_are_rejected(self):
        with self.assertRaises(ValueError):
            summarize([float("nan")])
        with self.assertRaises(ValueError):
            paired_report([1], [1, 2])


if __name__ == "__main__":
    unittest.main()
