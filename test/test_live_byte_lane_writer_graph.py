# RUN: python3 %s

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from distributed_state import LiveStateStore  # noqa: E402
from live_continuation import LiveContinuationExecutor  # noqa: E402


def writer_graph_program():
    return {
        "schema": "symcc-live-program-v1",
        "entry": "main",
        "input_size": 0,
        "memory_size": 4,
        "memory_hex": "00000000",
        "endianness": "little",
        "lowering": {"capabilities": [
            "bounded-byte-lane-memory-definedness",
            "bounded-byte-lane-writer-graph",
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
                            "op": "const",
                            "dst": "wide_poison",
                            "value": 1,
                            "bits": 1,
                        },
                        {
                            "op": "store",
                            "address": {"const": 0, "bits": 64},
                            "value": {"const": 258, "bits": 16},
                            "bits": 16,
                            "bytes": 2,
                            "byte_lane_store": "wide",
                            "byte_lane_defined": "wide_defined",
                            "byte_lane_poison_source": "wide_poison",
                        },
                        {
                            "op": "unary",
                            "operator": "identity",
                            "dst": "wide_defined",
                            "value": {"var": "wide_poison"},
                            "bits": 1,
                        },
                        {
                            "op": "const",
                            "dst": "narrow_poison",
                            "value": 1,
                            "bits": 1,
                        },
                        {
                            "op": "store",
                            "address": {"const": 1, "bits": 64},
                            "value": {"const": 3, "bits": 8},
                            "bits": 8,
                            "bytes": 1,
                            "byte_lane_store": "narrow",
                            "byte_lane_defined": "narrow_defined",
                            "byte_lane_poison_source": "narrow_poison",
                        },
                        {
                            "op": "unary",
                            "operator": "identity",
                            "dst": "narrow_defined",
                            "value": {"var": "narrow_poison"},
                            "bits": 1,
                        },
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
                            "left": {"var": "wide_defined"},
                            "right": {"var": "narrow_defined"},
                            "bits": 1,
                        },
                        {"op": "return", "value": {"var": "loaded"}},
                    ],
                },
                "byte_lane_memory_definedness": [{
                    "load": "loaded",
                    "defined": "loaded_defined",
                    "block": "entry",
                    "bytes": 2,
                    "writer_graph": True,
                    "lanes": [
                        {
                            "lane": 0,
                            "source": "store",
                            "store": "wide",
                            "store_byte": 0,
                            "store_bytes": 2,
                            "defined": "wide_defined",
                        },
                        {
                            "lane": 1,
                            "source": "store",
                            "store": "narrow",
                            "store_byte": 0,
                            "store_bytes": 1,
                            "defined": "narrow_defined",
                        },
                    ],
                }],
            },
        },
    }


class LiveByteLaneWriterGraphTests(unittest.TestCase):
    def validate(self, program):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        executor = LiveContinuationExecutor(LiveStateStore(temporary.name))
        return executor.create(program)

    def test_overlapping_last_writer_graph_is_admitted(self):
        self.assertTrue(self.validate(writer_graph_program()))

    def test_lane_address_correspondence_is_closed(self):
        store_byte_drift = writer_graph_program()
        store_byte_drift["functions"]["main"][
            "byte_lane_memory_definedness"
        ][0]["lanes"][0]["store_byte"] = 1
        with self.assertRaisesRegex(ValueError, "last-writer edge"):
            self.validate(store_byte_drift)

        address_drift = writer_graph_program()
        address_drift["functions"]["main"]["blocks"]["entry"][1][
            "address"
        ]["const"] = 2
        with self.assertRaisesRegex(ValueError, "writer graph"):
            self.validate(address_drift)

    def test_shadowed_writer_cannot_replace_last_writer(self):
        program = writer_graph_program()
        lane = program["functions"]["main"][
            "byte_lane_memory_definedness"
        ][0]["lanes"][1]
        lane.update({
            "store": "wide",
            "store_byte": 1,
            "store_bytes": 2,
            "defined": "wide_defined",
        })
        with self.assertRaisesRegex(ValueError, "last-writer edge"):
            self.validate(program)

    def test_poison_sidecar_is_bound_to_declared_source(self):
        program = writer_graph_program()
        store = program["functions"]["main"]["blocks"]["entry"][1]
        store["byte_lane_poison_source"] = "narrow_poison"
        with self.assertRaisesRegex(ValueError, "poison transfer"):
            self.validate(program)

    def test_capability_and_contract_are_closed(self):
        missing_capability = writer_graph_program()
        missing_capability["lowering"]["capabilities"].remove(
            "bounded-byte-lane-writer-graph"
        )
        with self.assertRaisesRegex(ValueError, "writer graph"):
            self.validate(missing_capability)

        missing_contract = writer_graph_program()
        missing_contract["functions"]["main"][
            "byte_lane_memory_definedness"
        ][0].pop("writer_graph")
        with self.assertRaisesRegex(ValueError, "writer graph contract"):
            self.validate(missing_contract)


if __name__ == "__main__":
    unittest.main()
