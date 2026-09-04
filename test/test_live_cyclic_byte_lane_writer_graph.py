# RUN: python3 %s

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from distributed_state import LiveStateStore  # noqa: E402
from live_continuation import LiveContinuationExecutor  # noqa: E402


def cyclic_writer_graph_program():
    return {
        "schema": "symcc-live-program-v1",
        "entry": "main",
        "input_size": 0,
        "memory_size": 4,
        "memory_hex": "00000000",
        "endianness": "little",
        "lowering": {"capabilities": [
            "bounded-cyclic-byte-lane-memory-definedness-phi",
            "bounded-cyclic-byte-lane-writer-graph",
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
                "blocks": {
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
                            "site": "cyclic-writer-graph",
                        },
                        {
                            "op": "branch",
                            "condition": {"var": "again"},
                            "true": "body",
                            "false": "exit",
                        },
                    ],
                    "body": [
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
                        {"op": "jump", "target": "back_edge"},
                    ],
                    "back_edge": [
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
                        {"op": "jump", "target": "merge"},
                    ],
                    "exit": [
                        {"op": "return", "value": {"var": "loaded"}},
                    ],
                },
                "cyclic_byte_lane_memory_definedness_phis": [{
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
                        },
                    ],
                }],
            },
        },
    }


class LiveCyclicByteLaneWriterGraphTests(unittest.TestCase):
    def validate(self, program):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        executor = LiveContinuationExecutor(LiveStateStore(temporary.name))
        return executor.create(program)

    def test_seed_and_backedge_writer_paths_are_address_closed(self):
        self.assertTrue(self.validate(cyclic_writer_graph_program()))

    def test_lane_address_and_store_offset_are_closed(self):
        offset_drift = cyclic_writer_graph_program()
        offset_drift["functions"]["main"][
            "cyclic_byte_lane_memory_definedness_phis"
        ][0]["incoming"][0]["lanes"][0]["store_byte"] = 1
        with self.assertRaisesRegex(ValueError, "last-writer edge"):
            self.validate(offset_drift)

        address_drift = cyclic_writer_graph_program()
        address_drift["functions"]["main"]["blocks"]["body"][1][
            "address"
        ]["const"] = 1
        with self.assertRaisesRegex(ValueError, "writer graph"):
            self.validate(address_drift)

    def test_carry_is_admitted_only_at_the_merge_load_boundary(self):
        program = cyclic_writer_graph_program()
        lane = program["functions"]["main"][
            "cyclic_byte_lane_memory_definedness_phis"
        ][0]["incoming"][1]["lanes"][1]
        lane["source"] = "initial"
        program["functions"]["main"]["blocks"]["back_edge"][1][
            "value"
        ] = {"const": 1, "bits": 1}
        with self.assertRaisesRegex(ValueError, "carry edge"):
            self.validate(program)

    def test_cyclic_poison_sidecar_is_bound_to_its_declared_source(self):
        program = cyclic_writer_graph_program()
        program["functions"]["main"]["blocks"]["body"][1][
            "byte_lane_poison_source"
        ] = "wrong_poison"
        with self.assertRaisesRegex(ValueError, "poison transfer"):
            self.validate(program)

    def test_cyclic_writer_capability_and_contract_are_closed(self):
        missing_capability = cyclic_writer_graph_program()
        missing_capability["lowering"]["capabilities"].remove(
            "bounded-cyclic-byte-lane-writer-graph"
        )
        with self.assertRaisesRegex(ValueError, "writer graph capability"):
            self.validate(missing_capability)

        missing_contract = cyclic_writer_graph_program()
        missing_contract["functions"]["main"][
            "cyclic_byte_lane_memory_definedness_phis"
        ][0].pop("writer_graph")
        with self.assertRaisesRegex(ValueError, "writer graph contract"):
            self.validate(missing_contract)


if __name__ == "__main__":
    unittest.main()
