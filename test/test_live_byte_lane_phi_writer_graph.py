# RUN: python3 %s

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from distributed_state import LiveStateStore  # noqa: E402
from live_continuation import LiveContinuationExecutor  # noqa: E402


def _arm(prefix, first, second, edge):
    return [
        {
            "op": "const",
            "dst": f"{prefix}_wide_poison",
            "value": 1,
            "bits": 1,
        },
        {
            "op": "store",
            "address": {"const": 0, "bits": 64},
            "value": {"const": first, "bits": 16},
            "bits": 16,
            "bytes": 2,
            "byte_lane_store": f"{prefix}_wide",
            "byte_lane_defined": f"{prefix}_wide_defined",
            "byte_lane_poison_source": f"{prefix}_wide_poison",
        },
        {
            "op": "unary",
            "operator": "identity",
            "dst": f"{prefix}_wide_defined",
            "value": {"var": f"{prefix}_wide_poison"},
            "bits": 1,
        },
        {
            "op": "const",
            "dst": f"{prefix}_narrow_poison",
            "value": 1,
            "bits": 1,
        },
        {
            "op": "store",
            "address": {"const": 1, "bits": 64},
            "value": {"const": second, "bits": 8},
            "bits": 8,
            "bytes": 1,
            "byte_lane_store": f"{prefix}_narrow",
            "byte_lane_defined": f"{prefix}_narrow_defined",
            "byte_lane_poison_source": f"{prefix}_narrow_poison",
        },
        {
            "op": "unary",
            "operator": "identity",
            "dst": f"{prefix}_narrow_defined",
            "value": {"var": f"{prefix}_narrow_poison"},
            "bits": 1,
        },
        {"op": "jump", "target": edge},
    ]


def _lanes(prefix):
    return [
        {
            "lane": 0,
            "source": "store",
            "store": f"{prefix}_wide",
            "store_byte": 0,
            "store_bytes": 2,
            "defined": f"{prefix}_wide_defined",
        },
        {
            "lane": 1,
            "source": "store",
            "store": f"{prefix}_narrow",
            "store_byte": 0,
            "store_bytes": 1,
            "defined": f"{prefix}_narrow_defined",
        },
    ]


def phi_writer_graph_program():
    return {
        "schema": "symcc-live-program-v1",
        "entry": "main",
        "input_size": 0,
        "memory_size": 4,
        "memory_hex": "00000000",
        "endianness": "little",
        "lowering": {"capabilities": [
            "bounded-byte-lane-memory-definedness-phi",
            "bounded-byte-lane-phi-writer-graph",
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
                            "op": "nondet",
                            "dst": "choice",
                            "bits": 1,
                            "site": "phi-writer-choice",
                        },
                        {
                            "op": "branch",
                            "condition": {"var": "choice"},
                            "true": "left",
                            "false": "right",
                        },
                    ],
                    "left": _arm("left", 258, 3, "left_edge"),
                    "right": _arm("right", 1028, 5, "right_edge"),
                    "left_edge": [
                        {
                            "op": "binary",
                            "operator": "and",
                            "dst": "loaded_defined",
                            "left": {"var": "left_wide_defined"},
                            "right": {"var": "left_narrow_defined"},
                            "bits": 1,
                        },
                        {"op": "jump", "target": "merge"},
                    ],
                    "right_edge": [
                        {
                            "op": "binary",
                            "operator": "and",
                            "dst": "loaded_defined",
                            "left": {"var": "right_wide_defined"},
                            "right": {"var": "right_narrow_defined"},
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
                        {"op": "return", "value": {"var": "loaded"}},
                    ],
                },
                "byte_lane_memory_definedness_phis": [{
                    "load": "loaded",
                    "defined": "loaded_defined",
                    "block": "merge",
                    "bytes": 2,
                    "writer_graph": True,
                    "incoming": [
                        {"block": "left_edge", "lanes": _lanes("left")},
                        {"block": "right_edge", "lanes": _lanes("right")},
                    ],
                }],
            },
        },
    }


class LiveByteLanePhiWriterGraphTests(unittest.TestCase):
    def validate(self, program):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        executor = LiveContinuationExecutor(LiveStateStore(temporary.name))
        return executor.create(program)

    def test_each_phi_endpoint_has_an_address_closed_writer_path(self):
        self.assertTrue(self.validate(phi_writer_graph_program()))

    def test_lane_address_and_store_offset_are_closed(self):
        offset_drift = phi_writer_graph_program()
        offset_drift["functions"]["main"][
            "byte_lane_memory_definedness_phis"
        ][0]["incoming"][0]["lanes"][0]["store_byte"] = 1
        with self.assertRaisesRegex(ValueError, "last-writer edge"):
            self.validate(offset_drift)

        address_drift = phi_writer_graph_program()
        address_drift["functions"]["main"]["blocks"]["left"][4][
            "address"
        ]["const"] = 2
        with self.assertRaisesRegex(ValueError, "writer graph"):
            self.validate(address_drift)

    def test_shadowed_phi_writer_cannot_replace_the_last_writer(self):
        program = phi_writer_graph_program()
        lane = program["functions"]["main"][
            "byte_lane_memory_definedness_phis"
        ][0]["incoming"][0]["lanes"][1]
        lane.update({
            "store": "left_wide",
            "store_byte": 1,
            "store_bytes": 2,
            "defined": "left_wide_defined",
        })
        with self.assertRaisesRegex(ValueError, "last-writer edge"):
            self.validate(program)

    def test_phi_poison_sidecar_is_bound_to_its_declared_source(self):
        program = phi_writer_graph_program()
        program["functions"]["main"]["blocks"]["left"][1][
            "byte_lane_poison_source"
        ] = "left_narrow_poison"
        with self.assertRaisesRegex(ValueError, "poison transfer"):
            self.validate(program)

        unreferenced = phi_writer_graph_program()
        unreferenced["lowering"]["capabilities"].append(
            "bounded-byte-lane-memory-definedness"
        )
        legacy_instructions = _arm("legacy", 258, 3, "unused")[:-1]
        for instruction in legacy_instructions:
            instruction.pop("byte_lane_poison_source", None)
        legacy_instructions.extend([
            {
                "op": "load",
                "address": {"const": 0, "bits": 64},
                "dst": "legacy_loaded",
                "bits": 16,
                "bytes": 2,
            },
            {
                "op": "binary",
                "operator": "and",
                "dst": "legacy_loaded_defined",
                "left": {"var": "legacy_wide_defined"},
                "right": {"var": "legacy_narrow_defined"},
                "bits": 1,
            },
            {"op": "return", "value": {"var": "legacy_loaded"}},
        ])
        unreferenced["functions"]["legacy"] = {
            "entry": "entry",
            "blocks": {"entry": legacy_instructions},
            "byte_lane_memory_definedness": [{
                "load": "legacy_loaded",
                "defined": "legacy_loaded_defined",
                "block": "entry",
                "bytes": 2,
                "lanes": _lanes("legacy"),
            }],
        }
        self.assertTrue(self.validate(unreferenced))
        legacy_instructions[1]["byte_lane_poison_source"] = (
            "legacy_wide_poison"
        )
        with self.assertRaisesRegex(ValueError, "not graph-referenced"):
            self.validate(unreferenced)

    def test_phi_writer_capability_and_contract_are_closed(self):
        missing_capability = phi_writer_graph_program()
        missing_capability["lowering"]["capabilities"].remove(
            "bounded-byte-lane-phi-writer-graph"
        )
        with self.assertRaisesRegex(ValueError, "writer graph capability"):
            self.validate(missing_capability)

        missing_contract = phi_writer_graph_program()
        missing_contract["functions"]["main"][
            "byte_lane_memory_definedness_phis"
        ][0].pop("writer_graph")
        with self.assertRaisesRegex(ValueError, "writer graph contract"):
            self.validate(missing_contract)


if __name__ == "__main__":
    unittest.main()
