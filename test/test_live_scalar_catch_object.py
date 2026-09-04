# RUN: python3 %s

from pathlib import Path
import copy
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from distributed_state import LiveStateStore  # noqa: E402
from live_continuation import LiveContinuationExecutor  # noqa: E402


CAPABILITIES = [
    "bounded-cleanup-exception-unwind",
    "bounded-typed-exception-matching",
    "bounded-exception-catch-lifecycle",
    "bounded-trivial-scalar-catch-object",
]


def scalar_catch_program():
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
                        "dst": "normal_result",
                        "normal_target": "normal",
                        "unwind_target": "landing",
                    }],
                    "normal": [{
                        "op": "return",
                        "value": {"var": "normal_result"},
                    }],
                    "landing": [
                        {
                            "op": "exception_match",
                            "types": [],
                            "cleanup": False,
                            "catch_all": True,
                        },
                        {
                            "op": "exception_token",
                            "dst": "token",
                            "bits": 64,
                        },
                        {
                            "op": "exception_begin_catch",
                            "token": {"var": "token"},
                            "token_bits": 64,
                        },
                        {
                            "op": "exception_value",
                            "dst": "caught",
                            "bits": 64,
                        },
                        {"op": "exception_end_catch"},
                        {
                            "op": "return",
                            "value": {"var": "caught"},
                        },
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
                        "normal": "normal",
                        "unwind": "cleanup",
                        "site": "8201",
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


class LiveScalarCatchObjectTests(unittest.TestCase):
    def execute(self, program, *, max_steps=32):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = LiveStateStore(temporary.name)
        executor = LiveContinuationExecutor(store)
        root = executor.create(program)
        return store, executor, executor.resume(root, max_steps=max_steps)

    def test_exact_width_scalar_payload_is_read_after_begin(self):
        store, _executor, result = self.execute(scalar_catch_program())
        self.assertEqual(result["halted"][0]["status"], "returned")
        self.assertEqual(result["halted"][0]["value"], 42)
        values = dict(store.restore_continuation(
            result["halted"][0]["checkpoint"]
        ).symbolic_store)
        self.assertFalse(any(
            name.startswith("@exception:") for name in values
        ))

    def test_scalar_payload_survives_caught_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = LiveStateStore(temporary)
            executor = LiveContinuationExecutor(store)
            paused = executor.resume(
                executor.create(scalar_catch_program()), max_steps=7
            )
            checkpoint = paused["frontier"][0]
            restored = LiveContinuationExecutor(store)
            completed = restored.resume(checkpoint, max_steps=3)
            self.assertEqual(completed["halted"][0]["value"], 42)

    def test_width_mismatch_fails_closed(self):
        program = scalar_catch_program()
        program["functions"]["main"]["blocks"]["landing"][3]["bits"] = 32
        with self.assertRaisesRegex(ValueError, "object width mismatch"):
            self.execute(program)

    def test_value_before_begin_and_after_end_fail_closed(self):
        before = scalar_catch_program()
        landing = before["functions"]["main"]["blocks"]["landing"]
        landing[2], landing[3] = landing[3], landing[2]

        after = scalar_catch_program()
        landing = after["functions"]["main"]["blocks"]["landing"]
        landing[3], landing[4] = landing[4], landing[3]

        for program in (before, after):
            with self.subTest(program=program), self.assertRaisesRegex(
                ValueError, "no active scalar catch"
            ):
                self.execute(program)

    def test_capability_dependencies_are_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            executor = LiveContinuationExecutor(LiveStateStore(temporary))
            missing = scalar_catch_program()
            missing["lowering"]["capabilities"].remove(
                "bounded-trivial-scalar-catch-object"
            )
            with self.assertRaisesRegex(
                ValueError, "exception value instruction is invalid"
            ):
                executor.create(missing)

            dependency = scalar_catch_program()
            dependency["lowering"]["capabilities"].remove(
                "bounded-exception-catch-lifecycle"
            )
            with self.assertRaisesRegex(
                ValueError, "requires exception lifecycle"
            ):
                executor.create(dependency)

            typed_dependency = scalar_catch_program()
            typed_dependency["lowering"]["capabilities"].remove(
                "bounded-typed-exception-matching"
            )
            with self.assertRaisesRegex(
                ValueError, "requires typed exception capability"
            ):
                executor.create(typed_dependency)

            unused = scalar_catch_program()
            del unused["functions"]["main"]["blocks"]["landing"][3]
            with self.assertRaisesRegex(
                ValueError, "scalar catch object capability has no contract"
            ):
                executor.create(unused)

    def test_instruction_schema_rejects_boolean_and_extra_fields(self):
        invalid = (
            {"op": "exception_value", "dst": "caught", "bits": True},
            {
                "op": "exception_value",
                "dst": "caught",
                "bits": 64,
                "offset": 0,
            },
            {"op": "exception_value", "dst": "", "bits": 64},
        )
        with tempfile.TemporaryDirectory() as temporary:
            executor = LiveContinuationExecutor(LiveStateStore(temporary))
            for instruction in invalid:
                program = copy.deepcopy(scalar_catch_program())
                program["functions"]["main"]["blocks"]["landing"][3] = (
                    instruction
                )
                with self.subTest(instruction=instruction), self.assertRaisesRegex(
                    ValueError, "exception value instruction is invalid"
                ):
                    executor.create(program)


if __name__ == "__main__":
    unittest.main()
