# RUN: python3 %s

from dataclasses import replace
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
    "bounded-exception-object-arena",
]


def exception_object_program(*, initialized=True, repeat=False):
    entry = [
        {
            "op": "exception_alloc",
            "dst": "object",
            "addresses": [64],
            "capacity": 1,
            "size": 8,
            "site": "9001",
            "bits": 64,
        },
    ]
    if initialized:
        entry.append({
            "op": "store",
            "address": {"const": 64, "bits": 64},
            "value": {"const": 42, "bits": 64},
            "bits": 64,
            "bytes": 8,
        })
    entry.append({
        "op": "exception_throw",
        "address": {"var": "object"},
        "addresses": [64],
        "arena_site": "9001",
        "site": "9002",
        "bits": 64,
        "type_id": 17,
        "unwind": "landing",
    })
    landing = [
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
        {
            "op": "exception_object_load",
            "dst": "caught",
            "bits": 64,
            "bytes": 8,
            "offset": 0,
        },
        {"op": "exception_end_catch"},
        (
            {"op": "jump", "target": "entry"}
            if repeat
            else {"op": "return", "value": {"var": "caught"}}
        ),
    ]
    return {
        "schema": "symcc-live-program-v1",
        "entry": "main",
        "input_size": 0,
        "memory_size": 72,
        "endianness": "little",
        "lowering": {"capabilities": list(CAPABILITIES)},
        "memory_objects": [{
            "name": "exception:9001:0",
            "kind": "heap",
            "site": "9001",
            "slot": 0,
            "capacity": 1,
            "address": 64,
            "size": 8,
            "read_only": False,
            "lifetime": "exception-lifecycle",
            "allocation": "bounded-exception-arena",
        }],
        "functions": {
            "main": {
                "entry": "entry",
                "blocks": {"entry": entry, "landing": landing},
            },
        },
    }


def aggregate_exception_object_program():
    program = exception_object_program()
    program["lowering"]["capabilities"].append(
        "bounded-exception-object-fields"
    )
    program["memory_size"] = 76
    memory_object = program["memory_objects"][0]
    memory_object["size"] = 12
    entry = program["functions"]["main"]["blocks"]["entry"]
    entry[0]["size"] = 12
    entry[1:2] = [
        {
            "op": "store",
            "address": {"const": 64 + offset, "bits": 64},
            "value": {"const": value, "bits": 32},
            "bits": 32,
            "bytes": 4,
        }
        for offset, value in ((0, 5), (4, 19), (8, 23))
    ]
    landing = program["functions"]["main"]["blocks"]["landing"]
    landing[3:4] = [
        {
            "op": "exception_object_load",
            "dst": destination,
            "bits": 32,
            "bytes": 4,
            "offset": offset,
        }
        for destination, offset in (("first", 0), ("second", 4), ("third", 8))
    ] + [
        {
            "op": "binary",
            "operator": "add",
            "dst": "partial",
            "left": {"var": "first"},
            "right": {"var": "second"},
            "bits": 32,
        },
        {
            "op": "binary",
            "operator": "add",
            "dst": "caught",
            "left": {"var": "partial"},
            "right": {"var": "third"},
            "bits": 32,
        },
    ]
    return program


class LiveExceptionObjectArenaTests(unittest.TestCase):
    def execute(self, program, *, max_steps=32):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = LiveStateStore(temporary.name)
        executor = LiveContinuationExecutor(store)
        root = executor.create(program)
        return store, executor, root, executor.resume(
            root, max_steps=max_steps
        )

    def test_object_roundtrip_releases_lifetime_but_retains_generation(self):
        store, _executor, _root, result = self.execute(
            exception_object_program()
        )
        self.assertEqual(result["halted"][0]["status"], "returned")
        self.assertEqual(result["halted"][0]["value"], 42)
        values = dict(store.restore_continuation(
            result["halted"][0]["checkpoint"]
        ).symbolic_store)
        self.assertFalse(any(
            name.startswith("@exception:") for name in values
        ))
        self.assertNotIn("@heap:live:64", values)
        self.assertNotIn("@heap:size:64", values)
        self.assertFalse(any(
            name.startswith("@heap:init:") for name in values
        ))
        generation = store.get_expression(
            values["@exception-arena:generation:64"]
        )
        self.assertEqual(generation["value"], 1)

    def test_aggregate_fields_and_multiple_loads_roundtrip(self):
        _store, _executor, _root, result = self.execute(
            aggregate_exception_object_program()
        )
        self.assertEqual(result["halted"][0]["status"], "returned")
        self.assertEqual(result["halted"][0]["value"], 47)

    def test_caught_object_survives_fresh_executor_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = LiveStateStore(temporary)
            executor = LiveContinuationExecutor(store)
            root = executor.create(exception_object_program())
            paused = executor.resume(root, max_steps=6)
            checkpoint = paused["frontier"][0]
            values = dict(
                store.restore_continuation(checkpoint).symbolic_store
            )
            self.assertIn("@exception:object-address", values)
            restored = LiveContinuationExecutor(store)
            completed = restored.resume(checkpoint, max_steps=8)
            self.assertEqual(completed["halted"][0]["value"], 42)

    def test_uninitialized_and_out_of_bounds_reads_fail_closed(self):
        uninitialized = exception_object_program(initialized=False)
        with self.assertRaisesRegex(ValueError, "uninitialized bytes"):
            self.execute(uninitialized)

        out_of_bounds = exception_object_program()
        out_of_bounds["functions"]["main"]["blocks"]["landing"][3][
            "offset"
        ] = 1
        out_of_bounds["lowering"]["capabilities"].append(
            "bounded-exception-object-fields"
        )
        with self.assertRaisesRegex(ValueError, "outside declared objects"):
            self.execute(out_of_bounds)

    def test_owner_and_generation_checkpoint_tampering_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = LiveStateStore(temporary)
            executor = LiveContinuationExecutor(store)
            root = executor.create(exception_object_program())

            allocated = executor.resume(root, max_steps=1)["frontier"][0]
            bundle = store.restore_continuation(allocated)
            values = dict(bundle.symbolic_store)
            values["@exception-arena:owner:64"] = store.put_expression({
                "op": "const", "value": 1, "bits": 64,
            })
            bad_owner = store.put_continuation(replace(
                bundle.descriptor,
                symbolic_store_root=store.put_symbolic_store(values),
                parent=allocated,
            ))
            with self.assertRaisesRegex(ValueError, "invalid arena owner"):
                LiveContinuationExecutor(store).resume(
                    bad_owner, max_steps=1
                )

            thrown = executor.resume(root, max_steps=3)["frontier"][0]
            bundle = store.restore_continuation(thrown)
            values = dict(bundle.symbolic_store)
            values["@exception:object-generation"] = store.put_expression({
                "op": "const", "value": 2, "bits": 64,
            })
            bad_generation = store.put_continuation(replace(
                bundle.descriptor,
                symbolic_store_root=store.put_symbolic_store(values),
                parent=thrown,
            ))
            with self.assertRaisesRegex(ValueError, "invalid exception object"):
                LiveContinuationExecutor(store).resume(
                    bad_generation, max_steps=1
                )

    def test_same_slot_reallocation_advances_generation(self):
        store, _executor, _root, result = self.execute(
            exception_object_program(repeat=True), max_steps=10
        )
        checkpoint = result["frontier"][0]
        values = dict(store.restore_continuation(
            checkpoint
        ).symbolic_store)
        generation = store.get_expression(
            values["@exception-arena:generation:64"]
        )
        self.assertEqual(generation["value"], 2)
        self.assertIn("@exception-arena:owner:64", values)

    def test_unthrown_owner_is_released_on_function_return(self):
        program = exception_object_program()
        program["functions"]["main"]["blocks"]["entry"] = [
            program["functions"]["main"]["blocks"]["entry"][0],
            {"op": "return", "value": 9},
        ]
        store, _executor, _root, result = self.execute(program)
        self.assertEqual(result["halted"][0]["value"], 9)
        values = dict(store.restore_continuation(
            result["halted"][0]["checkpoint"]
        ).symbolic_store)
        self.assertNotIn("@heap:live:64", values)
        self.assertNotIn("@exception-arena:owner:64", values)
        self.assertIn("@exception-arena:generation:64", values)

    def test_ordinary_heap_operations_cannot_alias_exception_arena(self):
        ordinary_free = exception_object_program()
        entry = ordinary_free["functions"]["main"]["blocks"]["entry"]
        entry.insert(1, {
            "op": "heap_free",
            "address": {"var": "object"},
        })
        with self.assertRaisesRegex(
            ValueError, "ordinary free cannot release"
        ):
            self.execute(ordinary_free)

        masquerading_alloc = exception_object_program()
        allocation = masquerading_alloc[
            "functions"
        ]["main"]["blocks"]["entry"][0]
        allocation.clear()
        allocation.update({
            "op": "heap_alloc",
            "dst": "object",
            "addresses": [64],
            "capacity": 1,
            "size": 8,
            "site": "9001",
            "allocator": "malloc",
            "bits": 64,
        })
        with tempfile.TemporaryDirectory() as temporary:
            executor = LiveContinuationExecutor(LiveStateStore(temporary))
            with self.assertRaisesRegex(
                ValueError, "does not match its memory pool"
            ):
                executor.create(masquerading_alloc)

    def test_schema_and_capability_dependencies_are_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            executor = LiveContinuationExecutor(LiveStateStore(temporary))
            missing = exception_object_program()
            missing["lowering"]["capabilities"].remove(
                "bounded-exception-object-arena"
            )
            with self.assertRaisesRegex(
                ValueError, "heap memory object contract is invalid"
            ):
                executor.create(missing)

            dependency = exception_object_program()
            dependency["lowering"]["capabilities"].remove(
                "bounded-exception-catch-lifecycle"
            )
            with self.assertRaisesRegex(
                ValueError, "requires exception lifecycle"
            ):
                executor.create(dependency)

            invalid = copy.deepcopy(exception_object_program())
            invalid["functions"]["main"]["blocks"]["entry"][0][
                "nullable"
            ] = False
            with self.assertRaisesRegex(
                ValueError, "exception allocation does not match its arena"
            ):
                executor.create(invalid)

            missing_fields = aggregate_exception_object_program()
            missing_fields["lowering"]["capabilities"].remove(
                "bounded-exception-object-fields"
            )
            with self.assertRaisesRegex(
                ValueError, "exception object load instruction is invalid"
            ):
                executor.create(missing_fields)

            empty_fields = exception_object_program()
            empty_fields["lowering"]["capabilities"].append(
                "bounded-exception-object-fields"
            )
            with self.assertRaisesRegex(
                ValueError, "fields capability has no nonzero-offset"
            ):
                executor.create(empty_fields)

            orphan_fields = aggregate_exception_object_program()
            orphan_fields["lowering"]["capabilities"].remove(
                "bounded-exception-object-arena"
            )
            with self.assertRaisesRegex(
                ValueError, "fields require exception object arena"
            ):
                executor.create(orphan_fields)


if __name__ == "__main__":
    unittest.main()
