# RUN: python3 %s

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from heap_path_optimality import HeapPathOptimizer  # noqa: E402


class HeapPathTests(unittest.TestCase):
    def test_alias_and_target_objectives(self):
        optimizer = HeapPathOptimizer()
        optimizer.add({"path_id": "hard", "target_distance": 20,
                       "allocation_bytes": 100000, "novelty": .1})
        optimizer.add({"path_id": "alias", "target_distance": 2,
                       "alias_count": 3, "novelty": .8})
        selected = optimizer.select()
        self.assertEqual(selected[0].path_id, "alias")
        self.assertEqual(optimizer.evidence(selected)["selected"], ["alias"])

    def test_capacity_is_bounded(self):
        optimizer = HeapPathOptimizer(max_paths=1)
        optimizer.add({"path_id": "a", "novelty": 0})
        optimizer.add({"path_id": "b", "novelty": 1})
        self.assertEqual([item.path_id for item in optimizer.select()], ["b"])

    def test_nonfinite_and_boolean_fields_are_rejected(self):
        with self.assertRaises(ValueError):
            optimizer = HeapPathOptimizer()
            optimizer.add({"path_id": "nan", "novelty": float("nan")})
        with self.assertRaises(ValueError):
            optimizer = HeapPathOptimizer()
            optimizer.add({"path_id": "bool", "depth": True})


if __name__ == "__main__":
    unittest.main()
