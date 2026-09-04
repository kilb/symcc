# RUN: python3 %s

import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class SymccHintMutatorTests(unittest.TestCase):
    def test_mutator_consumes_hint_tokens_and_poly_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hints = root / "extras"
            hints.mkdir()
            (hints / "hint_000000").write_bytes(b"MAGIC")
            cache = root / ".poly_cache"
            cache.write_text(
                "1 sat 8 0:65,1:66 2:1:3,3:4:6 -\n",
                encoding="utf-8",
            )

            old_env = os.environ.copy()
            try:
                os.environ["SYMCC_HINT_DIR"] = str(hints)
                os.environ["SYMCC_POLY_CACHE_MUTATOR"] = str(cache)
                os.environ["SYMCC_HINT_MUTATOR_RESCAN"] = "0.05"
                mutator = importlib.import_module("util.afl_symcc_hint_mutator")
                mutator = importlib.reload(mutator)
                mutator.init(7)
                outputs = [
                    mutator.fuzz(bytearray(b"xxxxxxxx"), None, 32)
                    for _ in range(6)
                ]
            finally:
                os.environ.clear()
                os.environ.update(old_env)

            self.assertTrue(any(out[:2] == b"AB" for out in outputs))
            self.assertTrue(any(b"MAGIC" in out for out in outputs))
            self.assertTrue(all(len(out) <= 32 for out in outputs))

    def test_full_matrix_sampling_preserves_correlated_constraints(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / ".poly_cache"
            # x + y == 10 and x - y in [-10, 10].
            cache.write_text(
                "2 sat 2 0:0,1:10 0:0:10,1:0:10 "
                "10:10:0=1,1=1;-10:10:0=1,1=-1\n",
                encoding="utf-8",
            )
            old_env = os.environ.copy()
            try:
                os.environ["SYMCC_HINT_DIR"] = ""
                os.environ["SYMCC_POLY_CACHE_MUTATOR"] = str(cache)
                os.environ["SYMCC_HINT_MUTATOR_RESCAN"] = "0.05"
                mutator = importlib.import_module("util.afl_symcc_hint_mutator")
                mutator = importlib.reload(mutator)
                mutator.init(11)
                outputs = [
                    bytes(mutator.fuzz(bytearray(b"\xff\xff"), None, 2))
                    for _ in range(12)
                ]
            finally:
                os.environ.clear()
                os.environ.update(old_env)

            self.assertTrue(all(len(out) == 2 for out in outputs))
            self.assertTrue(all(out[0] + out[1] == 10 for out in outputs))
            self.assertGreater(len(set(outputs)), 1)
            self.assertEqual(mutator.describe(32), "symcc_poly")

    def test_polytope_mode_does_not_havoc_feasible_fixed_point(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / ".poly_cache"
            cache.write_text(
                "3 sat 1 0:7 0:7:7 7:7:0=1\n",
                encoding="utf-8",
            )
            old_env = os.environ.copy()
            try:
                os.environ["SYMCC_HINT_DIR"] = ""
                os.environ["SYMCC_POLY_CACHE_MUTATOR"] = str(cache)
                os.environ["SYMCC_HINT_MUTATOR_RESCAN"] = "0.05"
                mutator = importlib.import_module("util.afl_symcc_hint_mutator")
                mutator = importlib.reload(mutator)
                mutator.init(5)
                output = bytes(mutator.fuzz(bytearray(b"\x07"), None, 1))
            finally:
                os.environ.clear()
                os.environ.update(old_env)

            self.assertEqual(output, b"\x07")
            self.assertEqual(mutator.describe(32), "symcc_poly")

    def test_mutator_applies_string_constraint_patches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            constraints = root / "strings.jsonl"
            constraints.write_text(
                json.dumps({
                    "schema": "symcc-string-constraint-v1",
                    "op": "strcmp",
                    "site": 9,
                    "result": -1,
                    "taken_equal": False,
                    "symbolic_side": "left",
                    "token_hex": "4d41474943",
                    "nul_terminated": True,
                    "complete": True,
                    "patches": [
                        {"offset": 0, "value": ord("M")},
                        {"offset": 1, "value": ord("A")},
                        {"offset": 2, "value": ord("G")},
                        {"offset": 3, "value": ord("I")},
                        {"offset": 4, "value": ord("C")},
                        {"offset": 5, "value": 0},
                    ],
                }) + "\n",
                encoding="ascii",
            )
            old_env = os.environ.copy()
            try:
                os.environ["SYMCC_HINT_DIR"] = ""
                os.environ["SYMCC_POLY_CACHE_MUTATOR"] = ""
                os.environ["SYMCC_STRING_CONSTRAINTS"] = str(constraints)
                os.environ["SYMCC_HINT_MUTATOR_RESCAN"] = "0.05"
                mutator = importlib.import_module("util.afl_symcc_hint_mutator")
                mutator = importlib.reload(mutator)
                mutator.init(17)
                output = bytes(mutator.fuzz(bytearray(b"xxxxx\0zz"), None, 8))
            finally:
                os.environ.clear()
                os.environ.update(old_env)

            self.assertEqual(output, b"MAGIC\0zz")
            self.assertEqual(mutator.describe(32), "symcc_string")


if __name__ == "__main__":
    unittest.main()
