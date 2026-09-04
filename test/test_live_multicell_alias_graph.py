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
    "bounded-memory-definedness-phi",
    "bounded-multicell-memory-definedness-phi",
    "bounded-multicell-alias-graph",
]


def alias_graph_program(left, right):
    contracts = []
    for load, neighbor in (("left", "right"), ("right", "left")):
        contracts.append({
            "load": load,
            "defined": f"{load}_defined",
            "block": "merge",
            "multicell": True,
            "alias_graph_neighbors": [neighbor],
            "incoming": [
                {"block": "edge_a"},
                {"block": "edge_b"},
            ],
        })

    def edge():
        return [
            {
                "op": "unary",
                "operator": "identity",
                "dst": "left_defined",
                "value": {"const": 1, "bits": 1},
                "bits": 1,
            },
            {
                "op": "unary",
                "operator": "identity",
                "dst": "right_defined",
                "value": {"const": 1, "bits": 1},
                "bits": 1,
            },
            {"op": "jump", "target": "merge"},
        ]

    return {
        "schema": "symcc-live-program-v1",
        "entry": "main",
        "input_size": 0,
        "memory_size": 16,
        "memory_hex": "00" * 16,
        "endianness": "little",
        "lowering": {"capabilities": list(CAPABILITIES)},
        "memory_objects": [{
            "name": "cells",
            "kind": "static",
            "address": 0,
            "size": 16,
            "read_only": False,
        }],
        "functions": {
            "main": {
                "entry": "entry",
                "blocks": {
                    "entry": [{"op": "jump", "target": "edge_a"}],
                    "edge_a": edge(),
                    "edge_b": edge(),
                    "merge": [
                        dict(left, op="load", dst="left", bits=8, bytes=1),
                        dict(right, op="load", dst="right", bits=8, bytes=1),
                        {"op": "return", "value": {"var": "left"}},
                    ],
                },
                "memory_defined_phis": contracts,
            },
        },
    }


def constant_load(address):
    return {"address": {"const": address, "bits": 64}}


def guarded_load(address, expected):
    return {
        "address": {"var": "pointer"},
        "alias_cases": [{
            "addresses": [address],
            "guards": [{
                "value": {"var": "guard"},
                "bits": 1,
                "equals": expected,
            }],
        }],
    }


def indexed_load(indices):
    return {
        "address": {"var": "pointer"},
        "aliases": [0, 1],
        "alias_index": {"var": "index"},
        "alias_index_bits": 8,
        "alias_index_values": list(indices),
        "alias_index_min": min(indices),
        "alias_index_max": max(indices),
    }


class LiveMulticellAliasGraphTests(unittest.TestCase):
    def validate(self, program):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        executor = LiveContinuationExecutor(LiveStateStore(temporary.name))
        return executor.create(program)

    def test_constant_disjoint_cells_are_admitted(self):
        self.assertTrue(self.validate(alias_graph_program(
            constant_load(0), constant_load(8)
        )))

    def test_guard_correlated_overlap_is_admitted(self):
        self.assertTrue(self.validate(alias_graph_program(
            guarded_load(0, 0), guarded_load(0, 1)
        )))

    def test_symbolic_index_correspondence_closes_address_overlap(self):
        program = alias_graph_program(
            indexed_load((0, 1)), indexed_load((-2, -1))
        )
        self.assertTrue(self.validate(program))

        overlapping = copy.deepcopy(program)
        instruction = overlapping["functions"]["main"]["blocks"][
            "merge"
        ][1]
        instruction["alias_index_values"] = [0, -1]
        instruction["alias_index_min"] = -1
        instruction["alias_index_max"] = 0
        with self.assertRaisesRegex(ValueError, "feasible overlap"):
            self.validate(overlapping)

    def test_graph_capability_and_symmetry_are_closed(self):
        program = alias_graph_program(constant_load(0), constant_load(8))
        missing_capability = copy.deepcopy(program)
        missing_capability["lowering"]["capabilities"].remove(
            "bounded-multicell-alias-graph"
        )
        with self.assertRaisesRegex(ValueError, "multi-cell alias graph"):
            self.validate(missing_capability)

        no_contract = copy.deepcopy(program)
        for contract in no_contract["functions"]["main"][
            "memory_defined_phis"
        ]:
            contract.pop("alias_graph_neighbors")
        with self.assertRaisesRegex(ValueError, "capability has no contract"):
            self.validate(no_contract)

        asymmetric = copy.deepcopy(program)
        asymmetric["functions"]["main"]["memory_defined_phis"][0][
            "alias_graph_neighbors"
        ] = []
        with self.assertRaisesRegex(ValueError, "multi-cell alias graph"):
            self.validate(asymmetric)

    def test_feasible_constant_overlap_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "feasible overlap"):
            self.validate(alias_graph_program(
                constant_load(0), constant_load(0)
            ))


if __name__ == "__main__":
    unittest.main()
