# RUN: python3 %s

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from cross_theory_selective import CrossTheorySelector  # noqa: E402


class CrossTheorySelectiveTests(unittest.TestCase):
    def test_dependency_closed_slice_fixes_unselected_values(self):
        atoms = [
            {"atom_id": "s", "theory": "STRING", "variables": ["s"],
             "depends_on": ["b"]},
            {"atom_id": "b", "theory": "BV", "variables": ["b"]},
            {"atom_id": "i", "theory": "INT", "variables": ["i"]},
        ]
        decision = CrossTheorySelector().plan(atoms, {"s"}, {"i": 3})
        self.assertEqual(decision.mode, "selective")
        self.assertEqual(decision.selected_atoms, ("b", "s"))
        self.assertEqual(decision.fixed_variables, ("i",))

    def test_missing_dependency_or_concrete_value_falls_back(self):
        atoms = [{"atom_id": "s", "theory": "STRING", "variables": ["s"],
                  "depends_on": ["unknown"]}]
        decision = CrossTheorySelector().plan(atoms, {"s"}, {})
        self.assertEqual(decision.mode, "full")
        self.assertIn("unknown", decision.missing_dependencies)

    def test_duplicate_atom_id_falls_back(self):
        atoms = [
            {"atom_id": "x", "theory": "BV", "variables": ["x"]},
            {"atom_id": "x", "theory": "INT", "variables": ["x"]},
        ]
        self.assertEqual(CrossTheorySelector().plan(atoms, {"x"}).mode, "full")


if __name__ == "__main__":
    unittest.main()
