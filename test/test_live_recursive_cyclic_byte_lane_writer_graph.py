# RUN: python3 %s

import copy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from distributed_state import LiveStateStore  # noqa: E402
from live_continuation import LiveContinuationExecutor  # noqa: E402
from test_live_multiarm_cyclic_byte_lane_writer_graph import (  # noqa: E402
    multiarm_writer_graph_program,
)


def recursive_writer_graph_program(*, forwarded=False):
    program = multiarm_writer_graph_program(forwarded=forwarded)
    capabilities = program["lowering"]["capabilities"]
    capabilities.remove(
        "bounded-forwarded-multiarm-conditional-cyclic-byte-lane-memory-definedness-phi"
        if forwarded
        else "bounded-multiarm-conditional-cyclic-byte-lane-memory-definedness-phi"
    )
    capabilities.remove("bounded-multiarm-cyclic-byte-lane-writer-graph")
    capabilities.extend([
        (
            "bounded-forwarded-recursive-conditional-cyclic-byte-lane-memory-definedness-phi"
            if forwarded
            else "bounded-recursive-conditional-cyclic-byte-lane-memory-definedness-phi"
        ),
        "bounded-recursive-cyclic-byte-lane-writer-graph",
    ])
    function = program["functions"]["main"]
    old_key = (
        "forwarded_multiarm_conditional_cyclic_byte_lane_memory_definedness_phis"
        if forwarded
        else "multiarm_conditional_cyclic_byte_lane_memory_definedness_phis"
    )
    contract = function.pop(old_key)[0]
    multiarm_transfer = contract.pop("multiarm_transfers")[0]
    arms = {
        arm["route"]: {key: value for key, value in arm.items() if key != "route"}
        for arm in multiarm_transfer["arms"]
    }
    blocks = function["blocks"]
    blocks["inner_branch"][-1]["false"] = "deep_branch"
    deep_store_successor = (
        "deep_store_forward" if forwarded else "deep_store_arm"
    )
    carry_successor = "carry_forward" if forwarded else "carry_arm"
    blocks["deep_branch"] = [
        {
            "op": "nondet",
            "dst": "deep_choice",
            "bits": 1,
            "site": "recursive-writer-graph-deep",
        },
        {
            "op": "branch",
            "condition": {"var": "deep_choice"},
            "true": deep_store_successor,
            "false": carry_successor,
        },
    ]
    blocks["deep_store_arm"] = [
        {"op": "const", "dst": "deep_poison", "value": 1, "bits": 1},
        {
            "op": "store",
            "address": {"const": 0, "bits": 64},
            "value": {"const": 17, "bits": 8},
            "bits": 8,
            "bytes": 1,
            "byte_lane_store": "deep_store",
            "byte_lane_defined": "deep_defined",
            "byte_lane_poison_source": "deep_poison",
        },
        {
            "op": "unary",
            "operator": "identity",
            "dst": "deep_defined",
            "value": {"var": "deep_poison"},
            "bits": 1,
        },
        {"op": "jump", "target": "deep_store_edge"},
    ]
    blocks["deep_store_edge"] = [
        {
            "op": "unary",
            "operator": "identity",
            "dst": "lane0_defined",
            "value": {"var": "deep_defined"},
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
    ]
    if forwarded:
        blocks["deep_store_forward"] = [
            {"op": "jump", "target": "deep_store_arm"}
        ]

    contract["recursive_transfers"] = [{
        "root": "condition",
        "join": "join",
        "depth": 3,
        **({"forwarded": True} if forwarded else {}),
        "branches": [
            {
                "block": "condition",
                "true": blocks["condition"][-1]["true"],
                "false": "inner_branch",
            },
            {
                "block": "inner_branch",
                "true": blocks["inner_branch"][-1]["true"],
                "false": "deep_branch",
            },
            {
                "block": "deep_branch",
                "true": deep_store_successor,
                "false": carry_successor,
            },
        ],
        "leaves": [
            arms["root"],
            arms["inner_true"],
            {
                "block": "deep_store_arm",
                "successor": deep_store_successor,
                "edge": "deep_store_edge",
                "lanes": [
                    {
                        "lane": 0,
                        "source": "store",
                        "store": "deep_store",
                        "store_byte": 0,
                        "store_bytes": 1,
                        "defined": "deep_defined",
                    },
                    {"lane": 1, "source": "carry"},
                ],
            },
            arms["inner_false"],
        ],
    }]
    key = (
        "forwarded_recursive_conditional_cyclic_byte_lane_memory_definedness_phis"
        if forwarded
        else "recursive_conditional_cyclic_byte_lane_memory_definedness_phis"
    )
    function[key] = [contract]
    return program


class LiveRecursiveCyclicByteLaneWriterGraphTests(unittest.TestCase):
    def validate(self, program):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        executor = LiveContinuationExecutor(LiveStateStore(temporary.name))
        return executor.create(program)

    def test_direct_and_forwarded_four_leaf_trees_are_closed(self):
        self.assertTrue(self.validate(recursive_writer_graph_program()))
        self.assertTrue(
            self.validate(recursive_writer_graph_program(forwarded=True))
        )

    def test_deep_store_leaf_rejects_a_later_covering_writer(self):
        program = recursive_writer_graph_program()
        program["functions"]["main"]["blocks"]["deep_store_arm"].insert(
            -1,
            {
                "op": "store",
                "address": {"const": 0, "bits": 64},
                "value": {"const": 19, "bits": 8},
                "bits": 8,
                "bytes": 1,
                "byte_lane_store": "shadow_store",
            },
        )
        with self.assertRaisesRegex(ValueError, "last-writer edge"):
            self.validate(program)

    def test_pure_carry_leaf_rejects_a_covering_writer(self):
        program = recursive_writer_graph_program()
        program["functions"]["main"]["blocks"]["carry_arm"].insert(
            0,
            {
                "op": "store",
                "address": {"const": 1, "bits": 64},
                "value": {"const": 23, "bits": 8},
                "bits": 8,
                "bytes": 1,
                "byte_lane_store": "carry_shadow_store",
            },
        )
        with self.assertRaisesRegex(ValueError, "last-writer edge"):
            self.validate(program)

    def test_recursive_poison_sidecar_is_identity_bound(self):
        program = recursive_writer_graph_program()
        program["functions"]["main"]["blocks"]["deep_store_arm"][1][
            "byte_lane_poison_source"
        ] = "wrong_poison"
        with self.assertRaisesRegex(ValueError, "poison transfer"):
            self.validate(program)

    def test_recursive_writer_capability_and_marker_are_closed(self):
        missing_capability = recursive_writer_graph_program()
        missing_capability["lowering"]["capabilities"].remove(
            "bounded-recursive-cyclic-byte-lane-writer-graph"
        )
        with self.assertRaisesRegex(ValueError, "writer graph capability"):
            self.validate(missing_capability)

        missing_marker = copy.deepcopy(recursive_writer_graph_program())
        missing_marker["functions"]["main"][
            "recursive_conditional_cyclic_byte_lane_memory_definedness_phis"
        ][0].pop("writer_graph")
        with self.assertRaisesRegex(ValueError, "writer graph contract"):
            self.validate(missing_marker)

    def test_specialized_recursive_contract_cannot_borrow_base_proof(self):
        program = recursive_writer_graph_program()
        capabilities = program["lowering"]["capabilities"]
        capabilities.remove(
            "bounded-recursive-conditional-cyclic-byte-lane-memory-definedness-phi"
        )
        capabilities.append(
            "bounded-grouped-recursive-conditional-cyclic-byte-lane-memory-definedness-phi"
        )
        function = program["functions"]["main"]
        contract = function.pop(
            "recursive_conditional_cyclic_byte_lane_memory_definedness_phis"
        )[0]
        contract["recursive_transfers"][0]["grouped"] = True
        function[
            "grouped_recursive_conditional_cyclic_byte_lane_memory_definedness_phis"
        ] = [contract]
        with self.assertRaisesRegex(ValueError, "writer graph"):
            self.validate(program)


if __name__ == "__main__":
    unittest.main()
