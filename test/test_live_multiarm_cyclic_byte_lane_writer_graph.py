# RUN: python3 %s

import copy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from distributed_state import LiveStateStore  # noqa: E402
from live_continuation import LiveContinuationExecutor  # noqa: E402
from test_live_conditional_cyclic_byte_lane_writer_graph import (  # noqa: E402
    conditional_writer_graph_program,
)


def multiarm_writer_graph_program(*, forwarded=False):
    program = conditional_writer_graph_program(forwarded=forwarded)
    capabilities = program["lowering"]["capabilities"]
    capabilities.remove(
        "bounded-forwarded-conditional-cyclic-byte-lane-memory-definedness-phi"
        if forwarded
        else "bounded-conditional-cyclic-byte-lane-memory-definedness-phi"
    )
    capabilities.remove(
        "bounded-conditional-cyclic-byte-lane-writer-graph"
    )
    capabilities.extend([
        (
            "bounded-forwarded-multiarm-conditional-cyclic-byte-lane-memory-definedness-phi"
            if forwarded
            else "bounded-multiarm-conditional-cyclic-byte-lane-memory-definedness-phi"
        ),
        "bounded-multiarm-cyclic-byte-lane-writer-graph",
    ])
    function = program["functions"]["main"]
    old_key = (
        "forwarded_conditional_cyclic_byte_lane_memory_definedness_phis"
        if forwarded
        else "conditional_cyclic_byte_lane_memory_definedness_phis"
    )
    old_contract = function.pop(old_key)[0]
    old_contract.pop("conditional_transfers")
    blocks = function["blocks"]

    root_successor = "store_forward" if forwarded else "store_arm"
    inner_true_successor = (
        "inner_store_forward" if forwarded else "inner_store_arm"
    )
    inner_false_successor = "carry_forward" if forwarded else "carry_arm"
    blocks["condition"][-1]["true"] = root_successor
    blocks["condition"][-1]["false"] = "inner_branch"
    blocks["inner_branch"] = [
        {
            "op": "nondet",
            "dst": "inner_choice",
            "bits": 1,
            "site": "multiarm-writer-graph-inner",
        },
        {
            "op": "branch",
            "condition": {"var": "inner_choice"},
            "true": inner_true_successor,
            "false": inner_false_successor,
        },
    ]

    root_store = blocks["store_arm"][1]
    root_store["byte_lane_store"] = "root_store"
    root_store["byte_lane_defined"] = "root_defined"
    root_store["byte_lane_poison_source"] = "body_poison"
    blocks["store_arm"][2]["dst"] = "root_defined"
    blocks["store_edge"][0]["value"] = {"var": "root_defined"}

    blocks["inner_store_arm"] = [
        {
            "op": "const",
            "dst": "inner_poison",
            "value": 1,
            "bits": 1,
        },
        {
            "op": "store",
            "address": {"const": 1, "bits": 64},
            "value": {"const": 11, "bits": 8},
            "bits": 8,
            "bytes": 1,
            "byte_lane_store": "inner_store",
            "byte_lane_defined": "inner_defined",
            "byte_lane_poison_source": "inner_poison",
        },
        {
            "op": "unary",
            "operator": "identity",
            "dst": "inner_defined",
            "value": {"var": "inner_poison"},
            "bits": 1,
        },
        {"op": "jump", "target": "inner_store_edge"},
    ]
    blocks["inner_store_edge"] = [
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
            "value": {"var": "inner_defined"},
            "bits": 1,
        },
        {"op": "jump", "target": "join"},
    ]
    if forwarded:
        blocks["inner_store_forward"] = [
            {"op": "jump", "target": "inner_store_arm"}
        ]

    transfer = {
        "root_branch": "condition",
        "inner_branch": "inner_branch",
        "join": "join",
        "arms": [
            {
                "route": "root",
                "block": "store_arm",
                "successor": root_successor,
                "edge": "store_edge",
                "lanes": [
                    {
                        "lane": 0,
                        "source": "store",
                        "store": "root_store",
                        "store_byte": 0,
                        "store_bytes": 1,
                        "defined": "root_defined",
                    },
                    {"lane": 1, "source": "carry"},
                ],
            },
            {
                "route": "inner_true",
                "block": "inner_store_arm",
                "successor": inner_true_successor,
                "edge": "inner_store_edge",
                "lanes": [
                    {"lane": 0, "source": "carry"},
                    {
                        "lane": 1,
                        "source": "store",
                        "store": "inner_store",
                        "store_byte": 0,
                        "store_bytes": 1,
                        "defined": "inner_defined",
                    },
                ],
            },
            {
                "route": "inner_false",
                "block": "carry_arm",
                "successor": inner_false_successor,
                "edge": "carry_edge",
                "lanes": [
                    {"lane": 0, "source": "carry"},
                    {"lane": 1, "source": "carry"},
                ],
            },
        ],
    }
    if forwarded:
        transfer["forwarded"] = True
    old_contract["multiarm_transfers"] = [transfer]
    contract_key = (
        "forwarded_multiarm_conditional_cyclic_byte_lane_memory_definedness_phis"
        if forwarded
        else "multiarm_conditional_cyclic_byte_lane_memory_definedness_phis"
    )
    function[contract_key] = [old_contract]
    return program


class LiveMultiArmCyclicByteLaneWriterGraphTests(unittest.TestCase):
    def validate(self, program):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        executor = LiveContinuationExecutor(LiveStateStore(temporary.name))
        return executor.create(program)

    def test_direct_and_forwarded_three_leaf_partitions_are_closed(self):
        self.assertTrue(self.validate(multiarm_writer_graph_program()))
        self.assertTrue(
            self.validate(multiarm_writer_graph_program(forwarded=True))
        )

    def test_inner_store_leaf_rejects_a_later_covering_writer(self):
        program = multiarm_writer_graph_program()
        arm = program["functions"]["main"]["blocks"]["inner_store_arm"]
        arm.insert(-1, {
            "op": "store",
            "address": {"const": 1, "bits": 64},
            "value": {"const": 13, "bits": 8},
            "bits": 8,
            "bytes": 1,
            "byte_lane_store": "shadow_store",
        })
        with self.assertRaisesRegex(ValueError, "last-writer edge"):
            self.validate(program)

    def test_pure_carry_arm_rejects_a_covering_writer(self):
        program = multiarm_writer_graph_program()
        arm = program["functions"]["main"]["blocks"]["carry_arm"]
        arm.insert(0, {
            "op": "store",
            "address": {"const": 0, "bits": 64},
            "value": {"const": 13, "bits": 8},
            "bits": 8,
            "bytes": 1,
            "byte_lane_store": "carry_shadow_store",
        })
        with self.assertRaisesRegex(ValueError, "last-writer edge"):
            self.validate(program)

    def test_multiarm_poison_sidecar_is_identity_bound(self):
        program = multiarm_writer_graph_program()
        program["functions"]["main"]["blocks"]["inner_store_arm"][1][
            "byte_lane_poison_source"
        ] = "wrong_poison"
        with self.assertRaisesRegex(ValueError, "poison transfer"):
            self.validate(program)

    def test_multiarm_writer_capability_and_marker_are_closed(self):
        missing_capability = multiarm_writer_graph_program()
        missing_capability["lowering"]["capabilities"].remove(
            "bounded-multiarm-cyclic-byte-lane-writer-graph"
        )
        with self.assertRaisesRegex(ValueError, "writer graph capability"):
            self.validate(missing_capability)

        missing_marker = copy.deepcopy(multiarm_writer_graph_program())
        missing_marker["functions"]["main"][
            "multiarm_conditional_cyclic_byte_lane_memory_definedness_phis"
        ][0].pop("writer_graph")
        with self.assertRaisesRegex(ValueError, "writer graph contract"):
            self.validate(missing_marker)


if __name__ == "__main__":
    unittest.main()
