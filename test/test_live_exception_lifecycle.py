# RUN: python3 %s

from pathlib import Path
from dataclasses import replace
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from distributed_state import LiveStateStore  # noqa: E402
from live_continuation import LiveContinuationExecutor  # noqa: E402
from live_state_search import LiveProgramGraph, location_name  # noqa: E402


CAPABILITIES = [
    "bounded-cleanup-exception-unwind",
    "bounded-typed-exception-matching",
    "bounded-exception-catch-lifecycle",
]


def throwing_worker():
    return {
        "entry": "entry",
        "blocks": {
            "entry": [{
                "op": "throw_if",
                "condition": {"const": 1, "bits": 1},
                "exception": {"const": 42, "bits": 64},
                "exception_bits": 64,
                "type_id": 17,
                "normal": "normal",
                "unwind": "cleanup",
                "site": "8101",
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
    }


def catch_sequence(value=99):
    return [
        {
            "op": "exception_match",
            "types": [17],
            "cleanup": False,
            "catch_all": False,
        },
        {"op": "exception_token", "dst": "token", "bits": 64},
        {
            "op": "exception_begin_catch",
            "token": {"var": "token"},
            "token_bits": 64,
        },
        {"op": "exception_end_catch"},
        {"op": "return", "value": value},
    ]


def lifecycle_program():
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
                    "landing": catch_sequence(),
                },
            },
            "worker": throwing_worker(),
        },
    }


def rethrow_program():
    program = lifecycle_program()
    program["entry"] = "outer"
    program["functions"]["middle"] = program["functions"].pop("main")
    inner = program["functions"]["middle"]["blocks"]["landing"]
    inner[3:] = [{
        "op": "exception_rethrow", "unwind": "rethrow_cleanup",
    }]
    program["functions"]["middle"]["blocks"]["rethrow_cleanup"] = [
        {
            "op": "exception_match",
            "types": [],
            "cleanup": True,
            "catch_all": False,
        },
        {"op": "exception_end_catch"},
        {"op": "throw"},
    ]
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
            "landing": catch_sequence(88),
        },
    }
    return program


class LiveExceptionLifecycleTests(unittest.TestCase):
    def execute(self, program, *, max_steps=64):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = LiveStateStore(temporary.name)
        executor = LiveContinuationExecutor(store)
        root = executor.create(program)
        return store, executor, executor.resume(root, max_steps=max_steps)

    def test_begin_end_catch_destroys_scalar_exception(self):
        store, _executor, result = self.execute(lifecycle_program())
        self.assertEqual(result["halted"][0]["status"], "returned")
        self.assertEqual(result["halted"][0]["value"], 99)
        values = dict(store.restore_continuation(
            result["halted"][0]["checkpoint"]
        ).symbolic_store)
        self.assertFalse(any(
            name.startswith("@exception:") for name in values
        ))

    def test_caught_phase_survives_checkpoint_and_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = LiveStateStore(temporary)
            executor = LiveContinuationExecutor(store)
            paused = executor.resume(
                executor.create(lifecycle_program()), max_steps=7
            )
            self.assertEqual(len(paused["frontier"]), 1)
            checkpoint = paused["frontier"][0]
            values = dict(
                store.restore_continuation(checkpoint).symbolic_store
            )
            self.assertEqual(
                store.get_expression(values["@exception:phase"])["value"],
                2,
            )
            self.assertEqual(
                store.get_expression(
                    values["@exception:catch-depth"]
                )["value"],
                0,
            )
            completed = executor.resume(checkpoint, max_steps=2)
            self.assertEqual(completed["halted"][0]["value"], 99)

    def test_rethrow_preserves_payload_type_and_token_for_outer_catch(self):
        _store, _executor, result = self.execute(rethrow_program())
        self.assertEqual(result["halted"][0]["status"], "returned")
        self.assertEqual(result["halted"][0]["value"], 88)

    def test_search_graph_contains_lifecycle_control_transfers(self):
        program = rethrow_program()
        graph = LiveProgramGraph(program)
        inner_landing = location_name("middle", "landing")
        inner_cleanup = location_name("middle", "rethrow_cleanup")
        self.assertIn(inner_cleanup, graph.adjacency[inner_landing])
        worker_entry = location_name("worker", "entry")
        self.assertEqual(
            graph.adjacency[worker_entry],
            {
                location_name("worker", "normal"),
                location_name("worker", "cleanup"),
            },
        )

    def test_lifecycle_capability_is_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            executor = LiveContinuationExecutor(LiveStateStore(temporary))
            missing = lifecycle_program()
            missing["lowering"]["capabilities"].remove(
                "bounded-exception-catch-lifecycle"
            )
            with self.assertRaisesRegex(
                ValueError, "exception token instruction is invalid"
            ):
                executor.create(missing)

            dependency = lifecycle_program()
            dependency["lowering"]["capabilities"].remove(
                "bounded-cleanup-exception-unwind"
            )
            with self.assertRaisesRegex(
                ValueError, "lifecycle requires cleanup"
            ):
                executor.create(dependency)

            unused = lifecycle_program()
            unused["functions"]["main"]["blocks"]["landing"] = [{
                "op": "exception_match",
                "types": [17],
                "cleanup": False,
                "catch_all": False,
            }, {"op": "return", "value": 1}]
            with self.assertRaisesRegex(
                ValueError, "lifecycle capability has no contract"
            ):
                executor.create(unused)

    def test_token_operand_schema_is_exact(self):
        invalid_tokens = (
            True,
            {"var": "token", "const": 8101, "bits": 64},
            {"const": 8101, "bits": 32},
            {"var": ""},
        )
        with tempfile.TemporaryDirectory() as temporary:
            executor = LiveContinuationExecutor(LiveStateStore(temporary))
            for token in invalid_tokens:
                program = lifecycle_program()
                program["functions"]["main"]["blocks"]["landing"][2][
                    "token"
                ] = token
                with self.subTest(token=token), self.assertRaisesRegex(
                    ValueError, "begin-catch instruction is invalid"
                ):
                    executor.create(program)

    def test_narrow_pointer_token_is_normalized_away_from_null(self):
        program = lifecycle_program()
        program["functions"]["worker"]["blocks"]["entry"][0]["site"] = (
            str(1 << 32)
        )
        landing = program["functions"]["main"]["blocks"]["landing"]
        landing[1]["bits"] = 32
        landing[2]["token_bits"] = 32
        _store, _executor, result = self.execute(program)
        self.assertEqual(result["halted"][0]["value"], 99)

    def test_mispaired_lifecycle_operations_fail_closed(self):
        cases = []
        wrong_token = lifecycle_program()
        wrong_token["functions"]["main"]["blocks"]["landing"][2][
            "token"
        ] = {"const": 9, "bits": 64}
        cases.append((wrong_token, "no matching token"))

        no_begin = lifecycle_program()
        del no_begin["functions"]["main"]["blocks"]["landing"][2]
        cases.append((no_begin, "no active catch"))

        unfinished = lifecycle_program()
        del unfinished["functions"]["main"]["blocks"]["landing"][2:4]
        cases.append((unfinished, "unfinished catch lifecycle"))

        bad_rethrow = lifecycle_program()
        bad_rethrow["functions"]["main"]["blocks"]["landing"][2:] = [{
            "op": "exception_rethrow", "unwind": "landing",
        }]
        cases.append((bad_rethrow, "no active catch"))

        for program, diagnostic in cases:
            with self.subTest(diagnostic=diagnostic):
                with self.assertRaisesRegex(ValueError, diagnostic):
                    self.execute(program)

    def test_cleanup_cannot_materialize_a_catch_token(self):
        program = lifecycle_program()
        landing = program["functions"]["main"]["blocks"]["landing"]
        landing[0] = {
            "op": "exception_match",
            "types": [],
            "cleanup": True,
            "catch_all": False,
        }
        with self.assertRaisesRegex(ValueError, "requires a matched catch"):
            self.execute(program)

    def test_checkpoint_lifecycle_metadata_is_validated(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = LiveStateStore(temporary)
            executor = LiveContinuationExecutor(store)
            root = executor.create(lifecycle_program())
            descriptor = store.restore_continuation(root).descriptor
            def const(value, bits):
                return store.put_expression({
                    "op": "const", "value": value, "bits": bits,
                })
            base = {
                "@exception:active": const(1, 1),
                "@exception:value": const(42, 64),
                "@exception:handler-depth": const(0, 64),
                "@exception:type": const(17, 32),
            }
            cases = (
                (
                    {**base, "@exception:phase": const(1, 2)},
                    "incomplete exception lifecycle",
                ),
                (
                    {
                        **base,
                        "@exception:phase": const(2, 2),
                        "@exception:token": const(8101, 64),
                    },
                    "invalid caught exception",
                ),
                (
                    {
                        **base,
                        "@exception:phase": const(1, 2),
                        "@exception:token": const(8101, 64),
                        "@exception:catch-depth": const(0, 64),
                    },
                    "invalid catch depth",
                ),
            )
            for values, diagnostic in cases:
                checkpoint = store.put_continuation(replace(
                    descriptor,
                    symbolic_store_root=store.put_symbolic_store(values),
                    parent=root,
                ))
                with self.subTest(diagnostic=diagnostic), self.assertRaisesRegex(
                    ValueError, diagnostic
                ):
                    executor.resume(checkpoint, max_steps=1)

            legacy_checkpoint = store.put_continuation(replace(
                descriptor,
                symbolic_store_root=store.put_symbolic_store(base),
                parent=root,
            ))
            with self.assertRaisesRegex(
                ValueError, "does not match its program capability"
            ):
                executor.resume(legacy_checkpoint, max_steps=1)


if __name__ == "__main__":
    unittest.main()
