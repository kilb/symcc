# RUN: python3 %s

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from solution_generator import (  # noqa: E402
    GENERATOR_SCHEMA,
    OPTIMISTIC_SCHEMA,
    QUERY_IR_CONVERTER_SCHEMA,
    TACTIC_CONVERTER_SCHEMA,
    derive_converter_chains,
    generator_hash,
    normalize_generator,
    sample_generator,
)


class SolutionGeneratorTests(unittest.TestCase):
    def test_inverts_composed_word_converter(self):
        nodes = [
            {"id": 0, "op": "read", "bits": 8, "children": [],
             "attrs": {"index": 0}},
            {"id": 1, "op": "read", "bits": 8, "children": [],
             "attrs": {"index": 1}},
            {"id": 2, "op": "concat", "bits": 16, "children": [0, 1],
             "attrs": {}},
            {"id": 3, "op": "constant", "bits": 16, "children": [],
             "attrs": {"value_hex": "1234"}},
            {"id": 4, "op": "xor", "bits": 16, "children": [2, 3],
             "attrs": {}},
            {"id": 5, "op": "constant", "bits": 16, "children": [],
             "attrs": {"value_hex": "b89e"}},
            {"id": 6, "op": "add", "bits": 16, "children": [4, 5],
             "attrs": {}},
            {"id": 7, "op": "constant", "bits": 16, "children": [],
             "attrs": {"value_hex": "5678"}},
            {"id": 8, "op": "equal", "bits": 1, "children": [6, 7],
             "attrs": {}},
        ]
        converters = derive_converter_chains(nodes, [8])
        self.assertEqual(len(converters), 1)
        # ((x ^ 0x1234) + 0xb89e) == 0x5678 -> x == 0x8fee.
        self.assertEqual(
            converters[0]["assignments"], {"0": 0x8F, "1": 0xEE})
        self.assertEqual(
            [step["op"] for step in converters[0]["steps"]],
            ["add", "xor", "concat-split"],
        )

    def test_sampling_is_deterministic_and_verifier_gated(self):
        generator = normalize_generator({
            "schema": GENERATOR_SCHEMA,
            "query_id": "a" * 64,
            "input_size": 2,
            "seed": 123,
            "fixed": {"0": 0x41},
            "ranges": [[1, 0, 10]],
            "fields": [[1]],
            "converter_chain": [],
            "verified_models": [{"0": 0x41, "1": 7}],
            "metrics": {},
        })
        def verifier(value):
            return value[0] == 0x41 and value[1] % 2 == 1

        first, metrics = sample_generator(generator, b"\0\0", 4, verifier)
        second, _ = sample_generator(generator, b"\0\0", 4, verifier)
        self.assertEqual(first, second)
        self.assertEqual(first[0], b"A\x07")
        self.assertTrue(all(verifier(value) for value in first))
        self.assertEqual(metrics["accepted"], 4)
        self.assertGreaterEqual(metrics["field_attempts"], 1)
        self.assertEqual(generator_hash(generator), generator_hash(
            normalize_generator(generator)))

    def test_fixed_completion_is_replayed_when_converter_is_identical(self):
        generator = normalize_generator({
            "schema": GENERATOR_SCHEMA,
            "query_id": "fixed-converter-replay",
            "input_size": 0,
            "seed": 0,
            "fixed": {"0": 0x78, "1": 0x56, "2": 0x34, "3": 0x12},
            "ranges": [],
            "fields": [],
            "converter_chain": [{
                "schema": QUERY_IR_CONVERTER_SCHEMA,
                "root": 1,
                "relation": "equal",
                "steps": [],
                "assignments": {
                    "0": 0x78,
                    "1": 0x56,
                    "2": 0x34,
                    "3": 0x12,
                },
                "proposal_only": True,
                "full_validation_required": True,
            }],
            "verified_models": [],
            "metrics": {},
        })
        expected = bytes.fromhex("78563412")
        accepted, metrics = sample_generator(
            generator,
            bytes.fromhex("05000000"),
            4,
            lambda value: value == expected,
        )
        self.assertEqual(accepted, [expected])
        self.assertEqual(metrics["fixed_base_attempts"], 1)
        self.assertEqual(metrics["converter_attempts"], 0)
        self.assertEqual(metrics["verified"], 1)

    def test_replays_persistent_converter_recipes_cumulatively(self):
        generator = normalize_generator({
            "schema": GENERATOR_SCHEMA,
            "query_id": "converter-replay",
            "input_size": 2,
            "seed": 0,
            "fixed": {},
            "ranges": [],
            "fields": [],
            "converter_chain": [
                {
                    "schema": QUERY_IR_CONVERTER_SCHEMA,
                    "root": 2,
                    "relation": "equal",
                    "steps": [],
                    "assignments": {"0": 0x41},
                    "proposal_only": True,
                    "full_validation_required": True,
                },
                {
                    "schema": QUERY_IR_CONVERTER_SCHEMA,
                    "root": 5,
                    "relation": "equal",
                    "steps": [],
                    "assignments": {"1": 0x42},
                    "proposal_only": True,
                    "full_validation_required": True,
                },
            ],
            "verified_models": [],
            "metrics": {},
        })
        accepted, metrics = sample_generator(
            generator,
            b"\0\0",
            4,
            lambda value: value == b"AB",
        )
        self.assertEqual(accepted, [b"AB"])
        self.assertEqual(metrics["converter_attempts"], 3)
        self.assertEqual(metrics["converter_conflicts"], 0)
        self.assertEqual(metrics["verified"], 3)

    def test_converter_recipe_validation_is_fail_closed(self):
        converter = {
            "schema": QUERY_IR_CONVERTER_SCHEMA,
            "root": 2,
            "relation": "equal",
            "steps": [{"op": "unknown", "bits": 8}],
            "assignments": {"0": 0x41},
            "proposal_only": True,
            "full_validation_required": True,
        }
        generator = {
            "schema": GENERATOR_SCHEMA,
            "query_id": "bad-converter",
            "input_size": 1,
            "seed": 0,
            "fixed": {},
            "ranges": [],
            "fields": [],
            "converter_chain": [converter],
            "verified_models": [],
            "metrics": {},
        }
        with self.assertRaisesRegex(ValueError, "unsupported"):
            normalize_generator(generator)
        converter["steps"] = []
        converter["full_validation_required"] = False
        with self.assertRaisesRegex(ValueError, "requires full validation"):
            normalize_generator(generator)

    def test_field_sampling_emits_boundary_models(self):
        generator = normalize_generator({
            "schema": GENERATOR_SCHEMA,
            "query_id": "field",
            "input_size": 2,
            "seed": 0,
            "fixed": {},
            "ranges": [[0, 1, 9], [1, 2, 8]],
            "fields": [[0, 1]],
            "converter_chain": [],
            "verified_models": [],
            "metrics": {},
        })
        accepted, metrics = sample_generator(
            generator, b"\0\0", 3, lambda value: True)
        self.assertEqual(accepted, [b"\x01\x02", b"\x05\x05", b"\x09\x08"])
        self.assertEqual(metrics["field_attempts"], 3)

    def test_sampling_rejects_out_of_witness_offsets(self):
        generator = normalize_generator({
            "schema": GENERATOR_SCHEMA,
            "query_id": "range-oob",
            "input_size": 0,
            "seed": 0,
            "fixed": {},
            "ranges": [[4, 1, 2]],
            "fields": [[0]],
            "converter_chain": [],
            "verified_models": [],
            "metrics": {},
        })
        with self.assertRaisesRegex(ValueError, "outside witness"):
            sample_generator(generator, b"\0", 1, lambda _value: True)

    def test_rejects_duplicate_ranges(self):
        with self.assertRaises(ValueError):
            normalize_generator({
                "schema": GENERATOR_SCHEMA,
                "query_id": "",
                "input_size": 1,
                "seed": 0,
                "fixed": {},
                "ranges": [[0, 0, 1], [0, 2, 3]],
                "fields": [],
                "converter_chain": [],
                "verified_models": [],
                "metrics": {},
            })

    def test_validates_optimistic_partition_and_verifier_metrics(self):
        base = {
            "schema": GENERATOR_SCHEMA,
            "query_id": "query",
            "input_size": 1,
            "seed": 0,
            "fixed": {},
            "ranges": [[0, 8, 255]],
            "fields": [[0]],
            "converter_chain": [],
            "verified_models": [{"0": 8}],
            "metrics": {
                "full_validation_checks": 3,
                "full_validation_failures": 2,
                "full_validation_accepted": 1,
            },
            "optimistic_simplification": {
                "schema": OPTIMISTIC_SCHEMA,
                "enabled": True,
                "strategy": "target-assertion-slice",
                "original_query_hash": "query",
                "original_assertions": 2,
                "target_assertion_start": 1,
                "kept_assertions": [1],
                "dropped_assertions": [0],
                "implication": "original-implies-weakened",
                "proposal_only": True,
                "full_validation_required": True,
            },
        }
        normalized = normalize_generator(base)
        self.assertTrue(normalized["optimistic_simplification"]["enabled"])

        malformed = json.loads(json.dumps(base))
        malformed["optimistic_simplification"]["kept_assertions"] = [0, 1]
        with self.assertRaisesRegex(ValueError, "partition"):
            normalize_generator(malformed)

        malformed = json.loads(json.dumps(base))
        malformed["metrics"]["full_validation_failures"] = 1
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            normalize_generator(malformed)

    def test_validates_tactic_model_converter_provenance(self):
        generator = {
            "schema": GENERATOR_SCHEMA,
            "query_id": "converter-query",
            "input_size": 2,
            "seed": 0,
            "fixed": {},
            "ranges": [],
            "fields": [],
            "converter_chain": [],
            "verified_models": [{"0": 10, "1": 9}],
            "metrics": {},
            "tactic_model_converter": {
                "schema": TACTIC_CONVERTER_SCHEMA,
                "enabled": True,
                "applied": True,
                "pipeline": ["simplify", "solve-eqs"],
                "original_query_hash": "converter-query",
                "input_assertions": 3,
                "subgoals": 1,
                "converted_models": 2,
                "full_validation_checks": 1,
                "full_validation_failures": 0,
                "full_validation_accepted": 1,
                "errors": 0,
                "full_validation_required": True,
            },
        }
        normalized = normalize_generator(generator)
        self.assertEqual(
            normalized["tactic_model_converter"]["pipeline"],
            ["simplify", "solve-eqs"])

        malformed = json.loads(json.dumps(generator))
        malformed["tactic_model_converter"]["full_validation_accepted"] = 2
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            normalize_generator(malformed)


if __name__ == "__main__":
    unittest.main()
