# RUN: python3 %s

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from distributed_state import LiveStateStore  # noqa: E402
from live_continuation import LiveContinuationExecutor  # noqa: E402


def conditional_writer_graph_program(*, forwarded=False):
    contract_key = (
        "forwarded_conditional_cyclic_byte_lane_memory_definedness_phis"
        if forwarded
        else "conditional_cyclic_byte_lane_memory_definedness_phis"
    )
    conditional_capability = (
        "bounded-forwarded-conditional-cyclic-byte-lane-memory-definedness-phi"
        if forwarded
        else "bounded-conditional-cyclic-byte-lane-memory-definedness-phi"
    )
    store_successor = "store_forward" if forwarded else "store_arm"
    carry_successor = "carry_forward" if forwarded else "carry_arm"
    blocks = {
        "entry": [
            {
                "op": "store",
                "address": {"const": 0, "bits": 64},
                "value": {"const": 42, "bits": 16},
                "bits": 16,
                "bytes": 2,
                "byte_lane_store": "seed_store",
            },
            {"op": "jump", "target": "seed_edge"},
        ],
        "seed_edge": [
            {
                "op": "unary",
                "operator": "identity",
                "dst": "lane0_defined",
                "value": {"const": 1, "bits": 1},
                "bits": 1,
            },
            {
                "op": "unary",
                "operator": "identity",
                "dst": "lane1_defined",
                "value": {"const": 1, "bits": 1},
                "bits": 1,
            },
            {"op": "jump", "target": "merge"},
        ],
        "merge": [
            {
                "op": "load",
                "address": {"const": 0, "bits": 64},
                "dst": "loaded",
                "bits": 16,
                "bytes": 2,
            },
            {
                "op": "binary",
                "operator": "and",
                "dst": "loaded_defined",
                "left": {"var": "lane0_defined"},
                "right": {"var": "lane1_defined"},
                "bits": 1,
            },
            {
                "op": "nondet",
                "dst": "again",
                "bits": 1,
                "site": "conditional-writer-graph-loop",
            },
            {
                "op": "branch",
                "condition": {"var": "again"},
                "true": "condition",
                "false": "exit",
            },
        ],
        "condition": [
            {
                "op": "nondet",
                "dst": "write",
                "bits": 1,
                "site": "conditional-writer-graph-choice",
            },
            {
                "op": "branch",
                "condition": {"var": "write"},
                "true": store_successor,
                "false": carry_successor,
            },
        ],
        "store_arm": [
            {
                "op": "const",
                "dst": "body_poison",
                "value": 1,
                "bits": 1,
            },
            {
                "op": "store",
                "address": {"const": 0, "bits": 64},
                "value": {"const": 7, "bits": 8},
                "bits": 8,
                "bytes": 1,
                "byte_lane_store": "body_store",
                "byte_lane_defined": "body_defined",
                "byte_lane_poison_source": "body_poison",
            },
            {
                "op": "unary",
                "operator": "identity",
                "dst": "body_defined",
                "value": {"var": "body_poison"},
                "bits": 1,
            },
            {"op": "jump", "target": "store_edge"},
        ],
        "carry_arm": [{"op": "jump", "target": "carry_edge"}],
        "store_edge": [
            {
                "op": "unary",
                "operator": "identity",
                "dst": "lane0_defined",
                "value": {"var": "body_defined"},
                "bits": 1,
            },
            {
                "op": "unary",
                "operator": "identity",
                "dst": "lane1_defined",
                "value": {"var": "lane1_defined"},
                "bits": 1,
            },
            {"op": "jump", "target": "join"},
        ],
        "carry_edge": [
            {
                "op": "unary",
                "operator": "identity",
                "dst": "lane0_defined",
                "value": {"var": "lane0_defined"},
                "bits": 1,
            },
            {
                "op": "unary",
                "operator": "identity",
                "dst": "lane1_defined",
                "value": {"var": "lane1_defined"},
                "bits": 1,
            },
            {"op": "jump", "target": "join"},
        ],
        "join": [{"op": "jump", "target": "back_edge"}],
        "back_edge": [
            {
                "op": "unary",
                "operator": "identity",
                "dst": "lane0_defined",
                "value": {"var": "lane0_defined"},
                "bits": 1,
            },
            {
                "op": "unary",
                "operator": "identity",
                "dst": "lane1_defined",
                "value": {"var": "lane1_defined"},
                "bits": 1,
            },
            {"op": "jump", "target": "merge"},
        ],
        "exit": [{"op": "return", "value": {"var": "loaded"}}],
    }
    if forwarded:
        blocks["store_forward"] = [{"op": "jump", "target": "store_arm"}]
        blocks["carry_forward"] = [{"op": "jump", "target": "carry_arm"}]
    transfer = {
        "branch": "condition",
        "store_arm": "store_arm",
        "carry_arm": "carry_arm",
        "store_successor": store_successor,
        "carry_successor": carry_successor,
        "join": "join",
        "store_edge": "store_edge",
        "carry_edge": "carry_edge",
        "store_when_true": True,
        "lanes": [
            {
                "lane": 0,
                "source": "store",
                "store": "body_store",
                "store_byte": 0,
                "store_bytes": 1,
                "defined": "body_defined",
            },
            {"lane": 1, "source": "carry"},
        ],
    }
    if forwarded:
        transfer["forwarded"] = True
    contract = {
        "load": "loaded",
        "defined": "loaded_defined",
        "block": "merge",
        "bytes": 2,
        "lane_defined": ["lane0_defined", "lane1_defined"],
        "writer_graph": True,
        "incoming": [
            {
                "block": "seed_edge",
                "lanes": [
                    {
                        "lane": 0,
                        "source": "store",
                        "store": "seed_store",
                        "store_byte": 0,
                        "store_bytes": 2,
                    },
                    {
                        "lane": 1,
                        "source": "store",
                        "store": "seed_store",
                        "store_byte": 1,
                        "store_bytes": 2,
                    },
                ],
            },
            {
                "block": "back_edge",
                "lanes": [
                    {"lane": 0, "source": "carry"},
                    {"lane": 1, "source": "carry"},
                ],
            },
        ],
        "conditional_transfers": [transfer],
    }
    return {
        "schema": "symcc-live-program-v1",
        "entry": "main",
        "input_size": 0,
        "memory_size": 4,
        "memory_hex": "00000000",
        "endianness": "little",
        "lowering": {"capabilities": [
            conditional_capability,
            "bounded-conditional-cyclic-byte-lane-writer-graph",
        ]},
        "memory_objects": [{
            "name": "buffer",
            "kind": "static",
            "address": 0,
            "size": 4,
            "read_only": False,
        }],
        "functions": {
            "main": {
                "entry": "entry",
                "blocks": blocks,
                contract_key: [contract],
            },
        },
    }


class LiveConditionalCyclicByteLaneWriterGraphTests(unittest.TestCase):
    def validate(self, program):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        executor = LiveContinuationExecutor(LiveStateStore(temporary.name))
        return executor.create(program)

    def test_direct_and_forwarded_leaf_partitions_are_address_closed(self):
        self.assertTrue(self.validate(conditional_writer_graph_program()))
        self.assertTrue(
            self.validate(conditional_writer_graph_program(forwarded=True))
        )

    def test_store_leaf_rejects_a_later_covering_writer(self):
        program = conditional_writer_graph_program()
        store_arm = program["functions"]["main"]["blocks"]["store_arm"]
        store_arm.insert(-1, {
            "op": "store",
            "address": {"const": 0, "bits": 64},
            "value": {"const": 9, "bits": 8},
            "bits": 8,
            "bytes": 1,
            "byte_lane_store": "shadow_store",
        })
        with self.assertRaisesRegex(ValueError, "last-writer edge"):
            self.validate(program)

    def test_carry_leaf_rejects_a_covering_writer(self):
        program = conditional_writer_graph_program()
        carry_arm = program["functions"]["main"]["blocks"]["carry_arm"]
        carry_arm.insert(0, {
            "op": "store",
            "address": {"const": 0, "bits": 64},
            "value": {"const": 9, "bits": 8},
            "bits": 8,
            "bytes": 1,
            "byte_lane_store": "carry_shadow_store",
        })
        with self.assertRaisesRegex(ValueError, "last-writer edge"):
            self.validate(program)

    def test_conditional_poison_sidecar_is_identity_bound(self):
        program = conditional_writer_graph_program()
        program["functions"]["main"]["blocks"]["store_arm"][1][
            "byte_lane_poison_source"
        ] = "wrong_poison"
        with self.assertRaisesRegex(ValueError, "poison transfer"):
            self.validate(program)

    def test_conditional_writer_capability_and_marker_are_closed(self):
        missing_capability = conditional_writer_graph_program()
        missing_capability["lowering"]["capabilities"].remove(
            "bounded-conditional-cyclic-byte-lane-writer-graph"
        )
        with self.assertRaisesRegex(ValueError, "writer graph capability"):
            self.validate(missing_capability)

        missing_marker = conditional_writer_graph_program()
        missing_marker["functions"]["main"][
            "conditional_cyclic_byte_lane_memory_definedness_phis"
        ][0].pop("writer_graph")
        with self.assertRaisesRegex(ValueError, "writer graph contract"):
            self.validate(missing_marker)


if __name__ == "__main__":
    unittest.main()
