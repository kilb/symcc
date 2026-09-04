# RUN: python3 %s

from pathlib import Path
import copy
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from distributed_state import LiveStateStore  # noqa: E402
from live_continuation import LiveContinuationExecutor  # noqa: E402


def exception_program(*, root_worker=False):
    worker = {
        "entry": "entry",
        "params": ["throws"],
        "blocks": {
            "entry": [{
                "op": "throw_if",
                "condition": {"var": "throws"},
                "exception": {"const": 42, "bits": 64},
                "exception_bits": 64,
                "normal": "normal",
                "unwind": "cleanup",
                "site": 7001,
            }],
            "normal": [{"op": "return", "value": 7}],
            "cleanup": [{"op": "throw"}],
        },
    }
    if root_worker:
        return {
            "schema": "symcc-live-program-v1",
            "entry": "worker",
            "input_size": 0,
            "lowering": {
                "capabilities": ["bounded-cleanup-exception-unwind"],
            },
            "functions": {"worker": worker},
        }
    return {
        "schema": "symcc-live-program-v1",
        "entry": "main",
        "input_size": 1,
        "lowering": {
            "capabilities": ["bounded-cleanup-exception-unwind"],
        },
        "functions": {
            "main": {
                "entry": "entry",
                "blocks": {
                    "entry": [
                        {"op": "input", "dst": "byte", "offset": 0},
                        {"op": "binary", "operator": "ne", "dst": "throws",
                         "left": {"var": "byte"},
                         "right": {"const": 0, "bits": 8}, "bits": 1},
                        {"op": "call", "function": "worker",
                         "args": [{"var": "throws"}], "dst": "result",
                         "normal_target": "normal", "unwind_target": "caught"},
                    ],
                    "normal": [{"op": "halt", "value": {"var": "result"}}],
                    "caught": [{"op": "halt", "value": 99}],
                },
            },
            "worker": worker,
        },
    }


def checkpoint_exception_program():
    program = exception_program(root_worker=True)
    worker = program["functions"]["worker"]
    return {
        "schema": "symcc-live-program-v1",
        "entry": "main",
        "input_size": 0,
        "lowering": copy.deepcopy(program["lowering"]),
        "functions": {
            "main": {
                "entry": "entry",
                "blocks": {
                    "entry": [{
                        "op": "call",
                        "function": "worker",
                        "args": [{"const": 1, "bits": 1}],
                        "dst": "result",
                        "normal_target": "normal",
                        "unwind_target": "caught",
                    }],
                    "normal": [{
                        "op": "return", "value": {"var": "result"},
                    }],
                    "caught": [
                        {
                            "op": "call",
                            "function": "handler_helper",
                            "args": [],
                            "dst": "ignored",
                        },
                        {"op": "return", "value": 99},
                    ],
                },
            },
            "worker": worker,
            "handler_helper": {
                "entry": "entry",
                "blocks": {
                    "entry": [{"op": "return", "value": 3}],
                },
            },
        },
    }


class LiveExceptionTests(unittest.TestCase):
    def test_symbolic_throw_forks_and_unwinds_to_caller_handler(self):
        with tempfile.TemporaryDirectory() as temporary:
            executor = LiveContinuationExecutor(LiveStateStore(temporary))
            root = executor.create(exception_program(), input_bytes=b"\x00")
            result = executor.resume(root, max_steps=32, max_states=8)
            self.assertEqual(result["forks"], 1)
            self.assertEqual(
                sorted(item["value"] for item in result["halted"]), [7, 99])

    def test_root_resume_reports_unhandled_exception(self):
        with tempfile.TemporaryDirectory() as temporary:
            executor = LiveContinuationExecutor(LiveStateStore(temporary))
            program = exception_program(root_worker=True)
            program["functions"]["worker"]["params"] = []
            program["functions"]["worker"]["blocks"]["entry"][0][
                "condition"] = {"const": 1, "bits": 1}
            root = executor.create(program)
            result = executor.resume(root, max_steps=8)
            self.assertEqual(result["halted"][0]["status"], "unhandled-exception")
            self.assertEqual(result["halted"][0]["value"], 42)

    def test_resume_without_active_exception_is_rejected(self):
        program = {
            "schema": "symcc-live-program-v1",
            "entry": "main",
            "input_size": 0,
            "lowering": {
                "capabilities": ["bounded-cleanup-exception-unwind"],
            },
            "functions": {"main": {"entry": "entry", "blocks": {
                "entry": [{"op": "throw"}],
            }}},
        }
        with tempfile.TemporaryDirectory() as temporary:
            executor = LiveContinuationExecutor(LiveStateStore(temporary))
            with self.assertRaisesRegex(ValueError, "no active exception"):
                executor.resume(executor.create(program), max_steps=2)

    def test_exception_contract_is_bound_to_lowering_capability(self):
        with tempfile.TemporaryDirectory() as temporary:
            executor = LiveContinuationExecutor(LiveStateStore(temporary))
            missing = exception_program(root_worker=True)
            missing.pop("lowering")
            with self.assertRaisesRegex(
                ValueError, "cleanup exception capability is missing"
            ):
                executor.create(missing)

            unused = {
                "schema": "symcc-live-program-v1",
                "entry": "main",
                "input_size": 0,
                "lowering": {
                    "capabilities": [
                        "bounded-cleanup-exception-unwind",
                    ],
                },
                "functions": {"main": {
                    "entry": "entry",
                    "blocks": {"entry": [{"op": "halt", "value": 0}]},
                }},
            }
            with self.assertRaisesRegex(
                ValueError, "capability has no contract"
            ):
                executor.create(unused)

    def test_exception_site_is_a_bounded_stable_identifier(self):
        with tempfile.TemporaryDirectory() as temporary:
            executor = LiveContinuationExecutor(LiveStateStore(temporary))
            for invalid in (0, -1, "not-a-site", 1 << 64):
                program = exception_program(root_worker=True)
                program["functions"]["worker"]["blocks"]["entry"][0][
                    "site"
                ] = invalid
                with self.subTest(site=invalid), self.assertRaisesRegex(
                    ValueError, "exception site is invalid"
                ):
                    executor.create(program)

    def test_active_exception_survives_checkpoint_and_clears_on_handler_return(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = LiveStateStore(temporary)
            executor = LiveContinuationExecutor(store)
            root = executor.create(checkpoint_exception_program())

            in_cleanup = executor.resume(root, max_steps=2)
            self.assertEqual(len(in_cleanup["frontier"]), 1)
            cleanup_checkpoint = in_cleanup["frontier"][0]
            cleanup_values = dict(
                store.restore_continuation(
                    cleanup_checkpoint
                ).symbolic_store
            )
            self.assertEqual(
                store.get_expression(cleanup_values["@exception:active"])[
                    "value"
                ],
                1,
            )
            self.assertEqual(
                store.get_expression(
                    cleanup_values["@exception:handler-depth"]
                )["value"],
                1,
            )

            at_handler = executor.resume(cleanup_checkpoint, max_steps=1)
            self.assertEqual(len(at_handler["frontier"]), 1)
            handler_checkpoint = at_handler["frontier"][0]
            handler_values = dict(
                store.restore_continuation(
                    handler_checkpoint
                ).symbolic_store
            )
            self.assertEqual(
                store.get_expression(
                    handler_values["@exception:handler-depth"]
                )["value"],
                0,
            )

            after_helper = executor.resume(
                handler_checkpoint, max_steps=2
            )
            self.assertEqual(len(after_helper["frontier"]), 1)
            after_helper_checkpoint = after_helper["frontier"][0]
            after_helper_values = dict(
                store.restore_continuation(
                    after_helper_checkpoint
                ).symbolic_store
            )
            self.assertIn("@exception:active", after_helper_values)

            completed = executor.resume(
                after_helper_checkpoint, max_steps=1
            )
            self.assertEqual(completed["halted"][0]["status"], "returned")
            self.assertEqual(completed["halted"][0]["value"], 99)
            final_values = dict(
                store.restore_continuation(
                    completed["halted"][0]["checkpoint"]
                ).symbolic_store
            )
            self.assertNotIn("@exception:active", final_values)
            self.assertNotIn("@exception:value", final_values)
            self.assertNotIn("@exception:handler-depth", final_values)


if __name__ == "__main__":
    unittest.main()
