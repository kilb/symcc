# RUN: python3 %s

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from string_constraints import (  # noqa: E402
    STRING_CONSTRAINT_SCHEMA,
    STRING_OPERATION_SCHEMA,
    STRING_QUERY_SCHEMA,
    SmtLibCliStringBackend,
    StringSolverPortfolio,
    candidate_satisfies_string_query,
    lower_string_query,
    materialize_string_candidates,
    normalize_string_constraint,
    normalize_string_operation,
    normalize_string_query,
    string_query_from_constraint,
    string_solver_backend_from_configuration,
)


class StringConstraintTests(unittest.TestCase):
    def test_materializes_offset_patches_with_verifier(self):
        record = normalize_string_constraint({
            "schema": STRING_CONSTRAINT_SCHEMA,
            "op": "strcmp",
            "site": 7,
            "result": -12,
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
        })
        candidates, metrics = materialize_string_candidates(
            [record], b"xxxxx\0zz", 4,
            lambda value: value.startswith(b"MAGIC\0"))
        self.assertEqual(candidates, [b"MAGIC\0zz"])
        self.assertEqual(metrics["accepted"], 1)

    def test_rejects_duplicate_patch_offsets(self):
        with self.assertRaises(ValueError):
            normalize_string_constraint({
                "schema": STRING_CONSTRAINT_SCHEMA,
                "op": "memcmp",
                "site": 1,
                "result": 1,
                "token_hex": "41",
                "patches": [
                    {"offset": 0, "value": 0x41},
                    {"offset": 0, "value": 0x42},
                ],
            })

    def test_normalizes_and_verifies_string_theory_query(self):
        query = normalize_string_query({
            "schema": STRING_QUERY_SCHEMA,
            "input_size": 8,
            "variables": [{
                "name": "input",
                "offset": 0,
                "capacity": 8,
                "min_length": 5,
                "max_length": 7,
                "nul_terminated": True,
            }],
            "constraints": [
                {
                    "op": "contains",
                    "needle": {"kind": "literal", "value_hex": "4d41474943"},
                    "haystack": {"kind": "var", "name": "input"},
                },
                {
                    "op": "indexof",
                    "haystack": {"kind": "var", "name": "input"},
                    "needle": {"kind": "literal", "value_hex": "4d41474943"},
                    "start": 0,
                    "relation": "eq",
                    "index": 1,
                },
                {
                    "op": "length",
                    "value": {"kind": "var", "name": "input"},
                    "relation": "ge",
                    "length": 6,
                },
                {
                    "op": "equal",
                    "left": {
                        "kind": "substr",
                        "value": {"kind": "var", "name": "input"},
                        "offset": 1,
                        "length": 5,
                    },
                    "right": {"kind": "literal", "value_hex": "4d41474943"},
                },
            ],
        })
        smt2 = lower_string_query(query)
        self.assertIn("str.contains", smt2)
        self.assertIn(
            "(str.contains |symcc!str!0!8!1| "
            "(str.++ (str.from_code 77)",
            smt2,
        )
        self.assertIn("str.indexof", smt2)
        self.assertIn("str.substr", smt2)
        self.assertIn("(declare-fun |0| () (_ BitVec 8))", smt2)
        self.assertIn("(str.from_code (bv2nat |0|))", smt2)
        self.assertTrue(
            candidate_satisfies_string_query(query, b"xMAGIC\0z"))
        self.assertFalse(
            candidate_satisfies_string_query(query, b"xMIGIC\0z"))

    def test_dual_view_combines_byte_and_string_constraints(self):
        query = normalize_string_query({
            "schema": STRING_QUERY_SCHEMA,
            "input_size": 6,
            "variables": [{
                "name": "input",
                "offset": 0,
                "capacity": 6,
                "min_length": 2,
                "max_length": 5,
                "nul_terminated": True,
            }],
            "constraints": [{
                "op": "contains",
                "needle": {"kind": "literal", "value_hex": "4243"},
                "haystack": {"kind": "var", "name": "input"},
            }],
            "byte_constraints": [
                {"offset": 0, "relation": "eq", "value": ord("A")},
                {"offset": 1, "relation": "uge", "value": ord("B")},
            ],
        })
        self.assertEqual(query["views"]["link"], "exact-nul-v1")
        smt2 = lower_string_query(query)
        self.assertIn("(assert (= |0| #x41))", smt2)
        self.assertIn("(assert (bvuge |1| #x42))", smt2)
        self.assertTrue(
            candidate_satisfies_string_query(query, b"ABC\0zz"))
        self.assertFalse(
            candidate_satisfies_string_query(query, b"XBC\0zz"))
        self.assertFalse(
            candidate_satisfies_string_query(query, b"ABCzzz"))

    def test_legacy_query_upgrades_and_length_views_are_exact(self):
        legacy = normalize_string_query({
            "schema": "symcc-string-query-v1",
            "input_size": 3,
            "variables": [{
                "name": "fixed",
                "offset": 0,
                "capacity": 3,
            }],
            "constraints": [{
                "op": "equal",
                "left": {"kind": "var", "name": "fixed"},
                "right": {"kind": "literal", "value_hex": "414243"},
            }],
        })
        self.assertEqual(legacy["schema"], STRING_QUERY_SCHEMA)
        self.assertEqual(legacy["variables"][0]["min_length"], 3)
        self.assertTrue(
            candidate_satisfies_string_query(legacy, b"ABC"))
        with self.assertRaises(ValueError):
            normalize_string_query({
                "schema": STRING_QUERY_SCHEMA,
                "input_size": 3,
                "variables": [{
                    "name": "bad",
                    "offset": 0,
                    "capacity": 3,
                    "min_length": 1,
                    "max_length": 2,
                }],
                "constraints": [{
                    "op": "length",
                    "value": {"kind": "var", "name": "bad"},
                    "relation": "eq",
                    "length": 2,
                }],
            })

    def test_rejects_overlapping_string_variable_spans(self):
        with self.assertRaises(ValueError):
            normalize_string_query({
                "schema": STRING_QUERY_SCHEMA,
                "input_size": 8,
                "variables": [
                    {"name": "left", "offset": 0, "capacity": 4},
                    {"name": "right", "offset": 3, "capacity": 4},
                ],
                "constraints": [{
                    "op": "distinct",
                    "left": {"kind": "var", "name": "left"},
                    "right": {"kind": "var", "name": "right"},
                }],
            })

    def test_solver_candidate_is_semantically_verified(self):
        class FakeBackend:
            name = "fake"

            def solve(self, query, timeout_ms):
                self.query = query
                self.timeout_ms = timeout_ms
                return {"status": "sat", "assignments": {"0": ord("X")}}

        record = normalize_string_constraint({
            "schema": STRING_CONSTRAINT_SCHEMA,
            "op": "strcmp",
            "site": 9,
            "result": 0,
            "taken_equal": True,
            "symbolic_side": "left",
            "token_hex": "4d41474943",
            "nul_terminated": True,
            "complete": True,
            "patches": [
                {"offset": index, "value": value}
                for index, value in enumerate(b"MAGIC\0")
            ],
        })
        query = string_query_from_constraint(record, 8)
        self.assertIsNotNone(query)
        backend = FakeBackend()
        candidates, metrics = materialize_string_candidates(
            [record],
            b"MAGIC\0zz",
            4,
            solver_backend=backend,
            solver_timeout_ms=77,
        )
        self.assertIn(b"XAGIC\0zz", candidates)
        self.assertEqual(backend.timeout_ms, 77)
        self.assertEqual(metrics["solver_verified"], 1)

    def test_parallel_portfolio_validates_sat_and_reports_disagreement(self):
        class Backend:
            def __init__(self, name, result, delay=0.0):
                self.name = name
                self.result = result
                self.delay = delay

            def solve(self, query, timeout_ms):
                del query, timeout_ms
                time.sleep(self.delay)
                return self.result

        record = normalize_string_constraint({
            "schema": STRING_CONSTRAINT_SCHEMA,
            "op": "strcmp",
            "site": 13,
            "result": -1,
            "taken_equal": False,
            "symbolic_side": "left",
            "token_hex": b"MAGIC".hex(),
            "nul_terminated": True,
            "complete": True,
            "patches": [
                {"offset": index, "value": value}
                for index, value in enumerate(b"MAGIC\0")
            ],
        })
        portfolio = StringSolverPortfolio([
            Backend("unsat", {"status": "unsat", "assignments": {}}, 0.01),
            Backend("invalid", {
                "status": "sat",
                "assignments": {"0": ord("X")},
            }),
            Backend("valid", {
                "status": "sat",
                "assignments": {
                    str(index): value
                    for index, value in enumerate(b"MAGIC\0")
                },
            }),
            Backend("duplicate", {
                "status": "sat",
                "assignments": {
                    str(index): value
                    for index, value in enumerate(b"MAGIC\0")
                },
            }),
        ])
        candidates, metrics = materialize_string_candidates(
            [record], b"xxxxx\0", 4, solver_backend=portfolio)
        self.assertIn(b"MAGIC\0", candidates)
        self.assertEqual(metrics["solver_backend_runs"], 4)
        self.assertEqual(metrics["solver_sat"], 3)
        self.assertEqual(metrics["solver_unsat"], 1)
        self.assertEqual(metrics["solver_rejected"], 1)
        self.assertEqual(metrics["solver_disagreements"], 1)
        self.assertEqual(metrics["solver_duplicate_models"], 1)

    def test_smtlib_cli_uses_explicit_byte_model_protocol(self):
        query = normalize_string_query({
            "schema": STRING_QUERY_SCHEMA,
            "input_size": 3,
            "variables": [{
                "name": "fixed",
                "offset": 0,
                "capacity": 3,
            }],
            "constraints": [{
                "op": "equal",
                "left": {"kind": "var", "name": "fixed"},
                "right": {"kind": "literal", "value_hex": "414243"},
            }],
        })
        script = (
            "import pathlib,sys;"
            "assert '(get-value (|0| |1| |2|))' in "
            "pathlib.Path(sys.argv[1]).read_text();"
            "print('sat');"
            "print('((|0| #x41) (|1| #b01000010) "
            "(|2| (_ bv67 8)))')"
        )
        backend = SmtLibCliStringBackend(
            [sys.executable, "-c", script, "{query}"],
            name="stub-smtlib",
        )
        result = backend.solve(query, 1000)
        self.assertEqual(result["status"], "sat")
        self.assertEqual(
            result["assignments"], {"0": 65, "1": 66, "2": 67})
        candidate = bytes(
            result["assignments"][str(index)] for index in range(3))
        self.assertTrue(candidate_satisfies_string_query(query, candidate))

    def test_backend_configuration_builds_bounded_portfolio(self):
        configuration = {
            "parallelism": 1,
            "selection": {
                "max_backends": 1,
                "warmup": 2,
                "explore_every": 32,
            },
            "backends": [
                {
                    "kind": "symcc-json",
                    "name": "z3",
                    "command": ["/bin/false"],
                },
                {
                    "kind": "smtlib",
                    "name": "adapter",
                    "command": ["/bin/false", "{query}"],
                },
            ],
        }
        backend = string_solver_backend_from_configuration(configuration)
        self.assertIsInstance(backend, StringSolverPortfolio)
        self.assertEqual(backend.parallelism, 1)
        self.assertEqual(
            [item.name for item in backend.backends], ["z3", "adapter"])
        self.assertIsNotNone(backend.selection_policy)
        self.assertEqual(backend.selection_policy.max_backends, 1)

    def test_contextual_portfolio_learns_persists_and_conformance_bypasses(self):
        class Backend:
            def __init__(self, name, verified, elapsed_us):
                self.name = name
                self.verified = verified
                self.elapsed_us = elapsed_us
                self.calls = 0

            def solve(self, query, timeout_ms):
                del query, timeout_ms
                self.calls += 1
                return {
                    "status": "sat",
                    "assignments": (
                        {"0": 65, "1": 66, "2": 67}
                        if self.verified else {"0": 88}
                    ),
                    "elapsed_us": self.elapsed_us,
                    "solver": self.name,
                }

        query = normalize_string_query({
            "schema": STRING_QUERY_SCHEMA,
            "input_size": 3,
            "variables": [{
                "name": "fixed",
                "offset": 0,
                "capacity": 3,
            }],
            "constraints": [{
                "op": "equal",
                "left": {"kind": "var", "name": "fixed"},
                "right": {"kind": "literal", "value_hex": "414243"},
            }],
        })
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "string-policy.json")
            selection = {
                "state_path": state_path,
                "max_backends": 1,
                "warmup": 1,
                "explore_every": 1000,
                "exploration": 0.1,
            }
            fast = Backend("fast-valid", True, 1000)
            slow = Backend("slow-invalid", False, 900000)
            portfolio = StringSolverPortfolio(
                [fast, slow], parallelism=2, selection=selection)

            first = portfolio.solve_all(query, 1000)
            self.assertEqual(len(first), 1)
            portfolio.observe_results([(first[0], True)])
            second = portfolio.solve_all(query, 1000)
            self.assertEqual(len(second), 1)
            portfolio.observe_results([(second[0], False)])
            third = portfolio.solve_all(query, 1000)
            self.assertEqual(len(third), 1)
            portfolio.observe_results([(third[0], True)])

            self.assertEqual(fast.calls, 2)
            self.assertEqual(slow.calls, 1)
            metrics = portfolio.drain_metrics()
            self.assertEqual(metrics["solver_backends_selected"], 3)
            self.assertEqual(metrics["solver_backends_skipped"], 3)
            self.assertEqual(metrics["solver_policy_updates"], 3)
            with open(state_path, encoding="utf-8") as stream:
                state = json.load(stream)
            self.assertEqual(
                state["schema"], "symcc-string-backend-policy-v1")
            self.assertEqual(state["global"]["fast-valid"]["verified"], 2)
            self.assertEqual(state["global"]["slow-invalid"]["verified"], 0)

            restarted_fast = Backend("fast-valid", True, 1000)
            restarted_slow = Backend("slow-invalid", False, 900000)
            restarted = StringSolverPortfolio(
                [restarted_fast, restarted_slow],
                parallelism=2,
                selection=selection,
            )
            selected = restarted.solve_all(query, 1000)
            self.assertEqual(
                selected[0]["_portfolio_backend"], "fast-valid")
            all_results = restarted.solve_all_conformance(query, 1000)
            self.assertEqual(
                {result["_portfolio_backend"] for result in all_results},
                {"fast-valid", "slow-invalid"},
            )

    def test_contextual_feedback_uses_concrete_string_verification(self):
        class Backend:
            def __init__(self, name, assignments):
                self.name = name
                self.assignments = assignments

            def solve(self, query, timeout_ms):
                del query, timeout_ms
                return {
                    "status": "sat",
                    "assignments": self.assignments,
                    "elapsed_us": 100,
                }

        record = normalize_string_constraint({
            "schema": STRING_CONSTRAINT_SCHEMA,
            "op": "strcmp",
            "site": 31,
            "result": 0,
            "taken_equal": True,
            "symbolic_side": "left",
            "token_hex": b"MAGIC".hex(),
            "nul_terminated": True,
            "complete": True,
            "patches": [
                {"offset": index, "value": value}
                for index, value in enumerate(b"MAGIC\0")
            ],
        })
        portfolio = StringSolverPortfolio(
            [
                Backend("invalid", {"0": ord("M")}),
                Backend("valid", {"0": ord("X")}),
            ],
            selection={
                "max_backends": 2,
                "warmup": 0,
                "explore_every": 1000,
            },
        )
        candidates, metrics = materialize_string_candidates(
            [record], b"MAGIC\0", 4, solver_backend=portfolio)
        self.assertIn(b"XAGIC\0", candidates)
        state = portfolio.selection_policy.snapshot()
        self.assertEqual(state["global"]["invalid"]["verified"], 0)
        self.assertEqual(state["global"]["valid"]["verified"], 1)
        self.assertEqual(metrics["solver_policy_updates"], 2)
        self.assertEqual(metrics["solver_backends_selected"], 2)

    def test_runtime_operations_become_dual_view_alternatives(self):
        input_bytes = [
            {"offset": index + 2, "value": value}
            for index, value in enumerate(b"ABC\0")
        ]
        length = normalize_string_operation({
            "schema": STRING_OPERATION_SCHEMA,
            "op": "strlen",
            "site": 11,
            "symbolic_role": "value",
            "constant_hex": "",
            "observed_length": 3,
            "observed_index": 3,
            "complete": True,
            "input_bytes": input_bytes,
        })
        length_query = string_query_from_constraint(length, 8)
        self.assertEqual(
            length_query["constraints"][0],
            {
                "op": "length",
                "value": {"kind": "var", "name": "input"},
                "relation": "ne",
                "length": 3,
            },
        )
        self.assertFalse(
            candidate_satisfies_string_query(
                length_query, b"xxABC\0zz"))
        self.assertTrue(
            candidate_satisfies_string_query(
                length_query, b"xxA\0BCzz"))

        search = normalize_string_operation({
            "schema": STRING_OPERATION_SCHEMA,
            "op": "strstr",
            "site": 12,
            "symbolic_role": "haystack",
            "constant_hex": b"BC".hex(),
            "observed_length": 3,
            "observed_index": 1,
            "complete": True,
            "input_bytes": input_bytes,
        })
        search_query = string_query_from_constraint(search, 8)
        self.assertFalse(
            candidate_satisfies_string_query(
                search_query, b"xxABC\0zz"))
        self.assertTrue(
            candidate_satisfies_string_query(
                search_query, b"xxBC\0Azz"))

    def test_operation_artifact_requires_exact_first_nul_span(self):
        with self.assertRaises(ValueError):
            normalize_string_operation({
                "schema": STRING_OPERATION_SCHEMA,
                "op": "strchr",
                "symbolic_role": "haystack",
                "constant_hex": "41",
                "observed_length": 2,
                "observed_index": -1,
                "complete": True,
                "input_bytes": [
                    {"offset": 0, "value": ord("X")},
                    {"offset": 1, "value": 0},
                    {"offset": 2, "value": 0},
                ],
            })

    def test_decimal_conversion_stays_in_exact_atoi_subdomain(self):
        operation = normalize_string_operation({
            "schema": STRING_OPERATION_SCHEMA,
            "op": "atoi",
            "site": 14,
            "symbolic_role": "value",
            "constant_hex": "",
            "observed_length": 3,
            "observed_index": 3,
            "observed_value": 123,
            "complete": True,
            "input_bytes": [
                {"offset": index + 2, "value": value}
                for index, value in enumerate(b"123\0")
            ],
        })
        query = string_query_from_constraint(operation, 8)
        self.assertEqual(
            [item["op"] for item in query["constraints"]],
            ["decimal", "to_int", "to_int", "to_int"],
        )
        smt2 = lower_string_query(query)
        self.assertIn("str.to_int", smt2)
        self.assertIn("re.range", smt2)
        self.assertFalse(
            candidate_satisfies_string_query(query, b"xx123\0zz"))
        self.assertTrue(
            candidate_satisfies_string_query(query, b"xx124\0zz"))
        self.assertFalse(
            candidate_satisfies_string_query(query, b"xxABC\0zz"))

    def test_signed_decimal_strtol10_contract(self):
        operation = normalize_string_operation({
            "schema": STRING_OPERATION_SCHEMA,
            "op": "strtol10",
            "site": 15,
            "symbolic_role": "value",
            "constant_hex": "",
            "observed_length": 4,
            "observed_index": 4,
            "observed_value": -123,
            "complete": True,
            "input_bytes": [
                {"offset": index + 1, "value": value}
                for index, value in enumerate(b"-123\0")
            ],
        })
        query = string_query_from_constraint(operation, 8)
        self.assertEqual(
            [item["signed"] for item in query["constraints"]],
            [True, True, True, True],
        )
        smt2 = lower_string_query(query)
        self.assertIn("str.prefixof (str.from_code 45)", smt2)
        self.assertIn("str.to_re (str.from_code 45)", smt2)
        self.assertFalse(
            candidate_satisfies_string_query(query, b"x-123\0zz"))
        self.assertTrue(
            candidate_satisfies_string_query(query, b"x-124\0zz"))
        self.assertTrue(
            candidate_satisfies_string_query(query, b"x123\0zzz"))
        self.assertFalse(
            candidate_satisfies_string_query(query, b"x+123\0zz"))

    def test_unsigned_decimal_strtoul10_respects_target_width(self):
        maximum = (1 << 64) - 1
        text = str(maximum).encode("ascii") + b"\0"
        operation = normalize_string_operation({
            "schema": STRING_OPERATION_SCHEMA,
            "op": "strtoul10",
            "site": 16,
            "symbolic_role": "value",
            "constant_hex": "",
            "observed_length": len(text) - 1,
            "observed_index": len(text) - 1,
            "observed_value": maximum,
            "integer_bits": 64,
            "complete": True,
            "input_bytes": [
                {"offset": index + 1, "value": value}
                for index, value in enumerate(text)
            ],
        })
        query = string_query_from_constraint(operation, len(text) + 2)
        self.assertEqual(
            [item["signed"] for item in query["constraints"]],
            [False, False, False, False],
        )
        self.assertEqual(
            query["constraints"][2]["integer"], maximum)
        witness = b"x" + text + b"z"
        self.assertFalse(
            candidate_satisfies_string_query(query, witness))
        smaller = b"x18446744073709551614\0z"
        self.assertTrue(
            candidate_satisfies_string_query(query, smaller))
        overflow = b"x99999999999999999999\0z"
        self.assertFalse(
            candidate_satisfies_string_query(query, overflow))
        with self.assertRaises(ValueError):
            normalize_string_operation({
                **operation,
                "observed_value": 1 << 64,
            })

    def test_legacy_materializer_skips_noop_equal_record(self):
        record = normalize_string_constraint({
            "schema": STRING_CONSTRAINT_SCHEMA,
            "op": "strcmp",
            "site": 10,
            "result": 0,
            "taken_equal": True,
            "symbolic_side": "left",
            "token_hex": "4d41474943",
            "nul_terminated": True,
            "complete": True,
            "patches": [
                {"offset": index, "value": value}
                for index, value in enumerate(b"MAGIC\0")
            ],
        })
        candidates, metrics = materialize_string_candidates(
            [record], b"MAGIC\0zz", 4)
        self.assertEqual(candidates, [])
        self.assertEqual(metrics["accepted"], 0)


if __name__ == "__main__":
    unittest.main()
