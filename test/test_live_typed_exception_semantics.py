# RUN: python3 %s

from pathlib import Path
import copy
from dataclasses import replace
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from distributed_state import LiveStateStore  # noqa: E402
from live_continuation import LiveContinuationExecutor  # noqa: E402


CAPABILITIES = [
    "bounded-cleanup-exception-unwind",
    "bounded-typed-exception-matching",
]


def typed_exception_program(
    *,
    thrown_type=11,
    caught_types=(11,),
    catch_all=False,
):
    return {
        "schema": "symcc-live-program-v1",
        "entry": "main",
        "input_size": 0,
        "lowering": {"capabilities": list(CAPABILITIES)},
        "functions": {
            "main": {
                "entry": "entry",
                "blocks": {
                    "entry": [{
                        "op": "call",
                        "function": "worker",
                        "args": [],
                        "dst": "result",
                        "normal_target": "normal",
                        "unwind_target": "landing",
                    }],
                    "normal": [{
                        "op": "return", "value": {"var": "result"},
                    }],
                    "landing": [
                        {
                            "op": "exception_match",
                            "types": list(caught_types),
                            "cleanup": False,
                            "catch_all": catch_all,
                        },
                        {
                            "op": "exception_type",
                            "dst": "selector",
                            "bits": 32,
                        },
                        {"op": "return", "value": {"var": "selector"}},
                    ],
                },
            },
            "worker": {
                "entry": "entry",
                "blocks": {
                    "entry": [{
                        "op": "throw_if",
                        "condition": {"const": 1, "bits": 1},
                        "exception": {"const": 42, "bits": 64},
                        "exception_bits": 64,
                        "type_id": thrown_type,
                        "normal": "normal",
                        "unwind": "cleanup",
                        "site": 7101,
                    }],
                    "normal": [{"op": "return", "value": 7}],
                    "cleanup": [
                        {
                            "op": "exception_match",
                            "types": [],
                            "cleanup": True,
                            "catch_all": False,
                        },
                        {"op": "throw"},
                    ],
                },
            },
        },
    }


class LiveTypedExceptionTests(unittest.TestCase):
    def execute(self, program, *, max_steps=32):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = LiveStateStore(temporary.name)
        executor = LiveContinuationExecutor(store)
        root = executor.create(program)
        return store, executor, executor.resume(root, max_steps=max_steps)

    def test_matching_type_exposes_normalized_selector(self):
        _store, _executor, result = self.execute(
            typed_exception_program()
        )
        self.assertEqual(result["halted"][0]["status"], "returned")
        self.assertEqual(result["halted"][0]["value"], 11)

    def test_nonmatching_type_propagates_to_root(self):
        _store, _executor, result = self.execute(
            typed_exception_program(caught_types=(12,))
        )
        self.assertEqual(
            result["halted"][0]["status"], "unhandled-exception"
        )
        self.assertEqual(result["halted"][0]["value"], 42)
        self.assertEqual(result["halted"][0]["type_id"], 11)

    def test_catch_all_accepts_any_typed_exception(self):
        _store, _executor, result = self.execute(
            typed_exception_program(
                thrown_type=27, caught_types=(), catch_all=True
            )
        )
        self.assertEqual(result["halted"][0]["value"], 27)

    def test_inner_type_mismatch_continues_to_matching_outer_handler(self):
        program = typed_exception_program(caught_types=(12,))
        middle = program["functions"].pop("main")
        program["entry"] = "outer"
        program["functions"]["middle"] = middle
        program["functions"]["outer"] = {
            "entry": "entry",
            "blocks": {
                "entry": [{
                    "op": "call",
                    "function": "middle",
                    "args": [],
                    "dst": "result",
                    "normal_target": "normal",
                    "unwind_target": "landing",
                }],
                "normal": [{
                    "op": "return", "value": {"var": "result"},
                }],
                "landing": [
                    {
                        "op": "exception_match",
                        "types": [11],
                        "cleanup": False,
                        "catch_all": False,
                    },
                    {
                        "op": "exception_type",
                        "dst": "selector",
                        "bits": 32,
                    },
                    {"op": "return", "value": {"var": "selector"}},
                ],
            },
        }
        _store, _executor, result = self.execute(program)
        self.assertEqual(result["halted"][0]["status"], "returned")
        self.assertEqual(result["halted"][0]["value"], 11)

    def test_type_survives_checkpoint_at_matched_landingpad(self):
        program = typed_exception_program()
        with tempfile.TemporaryDirectory() as temporary:
            store = LiveStateStore(temporary)
            executor = LiveContinuationExecutor(store)
            paused = executor.resume(
                executor.create(program), max_steps=5
            )
            self.assertEqual(len(paused["frontier"]), 1)
            checkpoint = paused["frontier"][0]
            values = dict(
                store.restore_continuation(checkpoint).symbolic_store
            )
            self.assertEqual(
                store.get_expression(values["@exception:type"])["value"],
                11,
            )
            completed = executor.resume(checkpoint, max_steps=2)
            self.assertEqual(completed["halted"][0]["value"], 11)
            final_values = dict(
                store.restore_continuation(
                    completed["halted"][0]["checkpoint"]
                ).symbolic_store
            )
            self.assertNotIn("@exception:type", final_values)

    def test_typed_contract_is_capability_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            executor = LiveContinuationExecutor(LiveStateStore(temporary))
            missing = typed_exception_program()
            missing["lowering"]["capabilities"].remove(
                "bounded-typed-exception-matching"
            )
            with self.assertRaisesRegex(
                ValueError, "typed exception capability is missing"
            ):
                executor.create(missing)

            unused = typed_exception_program()
            del unused["functions"]["main"]["blocks"]["landing"][1]
            del unused["functions"]["main"]["blocks"]["landing"][0][
                "types"
            ]
            unused["functions"]["main"]["blocks"]["landing"][0].update({
                "types": [], "cleanup": True, "catch_all": False,
            })
            unused["functions"]["worker"]["blocks"]["entry"][0].pop(
                "type_id"
            )
            with self.assertRaisesRegex(
                ValueError, "typed exception capability has no contract"
            ):
                executor.create(unused)

    def test_invalid_match_contracts_fail_closed(self):
        base = typed_exception_program()
        invalid_contracts = (
            {"types": [], "cleanup": False, "catch_all": False},
            {"types": [11, 11], "cleanup": False, "catch_all": False},
            {"types": [0], "cleanup": False, "catch_all": False},
            {"types": [1 << 32], "cleanup": False, "catch_all": False},
        )
        with tempfile.TemporaryDirectory() as temporary:
            executor = LiveContinuationExecutor(LiveStateStore(temporary))
            for contract in invalid_contracts:
                program = copy.deepcopy(base)
                program["functions"]["main"]["blocks"]["landing"][0] = {
                    "op": "exception_match", **contract,
                }
                with self.subTest(contract=contract), self.assertRaisesRegex(
                    ValueError, "exception match"
                ):
                    executor.create(program)

    def test_checkpoint_exception_metadata_is_semantically_validated(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = LiveStateStore(temporary)
            executor = LiveContinuationExecutor(store)
            root = executor.create(typed_exception_program())
            descriptor = store.restore_continuation(root).descriptor
            one = store.put_expression({
                "op": "const", "value": 1, "bits": 1,
            })
            zero = store.put_expression({
                "op": "const", "value": 0, "bits": 32,
            })
            payload = store.put_expression({
                "op": "const", "value": 42, "bits": 64,
            })
            bad_depth = store.put_expression({
                "op": "const", "value": 99, "bits": 64,
            })
            depth = store.put_expression({
                "op": "const", "value": 0, "bits": 64,
            })
            cases = (
                (
                    {"@exception:type": zero},
                    "orphan exception metadata",
                ),
                (
                    {
                        "@exception:active": one,
                        "@exception:value": payload,
                        "@exception:handler-depth": bad_depth,
                    },
                    "invalid exception handler depth",
                ),
                (
                    {
                        "@exception:active": one,
                        "@exception:value": payload,
                        "@exception:handler-depth": depth,
                        "@exception:type": zero,
                    },
                    "invalid exception type",
                ),
            )
            for values, diagnostic in cases:
                symbolic_store_root = store.put_symbolic_store(values)
                checkpoint = store.put_continuation(replace(
                    descriptor,
                    symbolic_store_root=symbolic_store_root,
                    parent=root,
                ))
                with self.subTest(diagnostic=diagnostic), self.assertRaisesRegex(
                    ValueError, diagnostic
                ):
                    executor.resume(checkpoint, max_steps=1)


if __name__ == "__main__":
    unittest.main()
