# RUN: python3 %s

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from distributed_state import LiveStateStore  # noqa: E402
from live_continuation import LiveContinuationExecutor  # noqa: E402


CAPABILITIES = [
    "bounded-pointer-union",
    "bounded-memory-definedness-phi",
    "bounded-multicell-memory-definedness-phi",
    "bounded-phi-correlated-pointer-domain-multicell-memory-definedness-phi",
    "bounded-multicell-alias-graph",
    "bounded-shared-phi-edge-discriminator",
]


def edge(tag_value):
    return [
        {
            "op": "const",
            "dst": "shared_pointer_edge_tag",
            "value": tag_value,
            "bits": 32,
        },
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


def guarded_load(addresses):
    return {
        "address": {"var": "pointer"},
        "alias_cases": [
            {
                "addresses": [address],
                "guards": [{
                    "value": {"var": "shared_pointer_edge_tag"},
                    "bits": 32,
                    "equals": tag,
                }],
            }
            for address, tag in addresses
        ],
    }


def program():
    contracts = []
    for load, neighbor in (("left", "right"), ("right", "left")):
        contracts.append({
            "load": load,
            "defined": f"{load}_defined",
            "block": "merge",
            "multicell": True,
            "phi_correlated_pointer_domains": True,
            "alias_graph_neighbors": [neighbor],
            "incoming": [{"block": "edge_a"}, {"block": "edge_b"}],
        })
    return {
        "schema": "symcc-live-program-v1",
        "entry": "main",
        "input_size": 0,
        "memory_size": 8,
        "memory_hex": "00" * 8,
        "endianness": "little",
        "lowering": {"capabilities": list(CAPABILITIES)},
        "memory_objects": [{
            "name": "cells",
            "kind": "static",
            "address": 0,
            "size": 8,
            "read_only": False,
        }],
        "functions": {
            "main": {
                "entry": "entry",
                "blocks": {
                    "entry": [{
                        "op": "branch",
                        "condition": {"const": 1, "bits": 1},
                        "true": "pred_a",
                        "false": "pred_b",
                    }],
                    "pred_a": [{"op": "jump", "target": "edge_a"}],
                    "pred_b": [{"op": "jump", "target": "edge_b"}],
                    "edge_a": edge(1),
                    "edge_b": edge(2),
                    "merge": [
                        dict(
                            guarded_load(((0, 1), (1, 2))),
                            op="load", dst="left", bits=8, bytes=1,
                        ),
                        dict(
                            guarded_load(((1, 1), (2, 2))),
                            op="load", dst="right", bits=8, bytes=1,
                        ),
                        {"op": "return", "value": {"var": "left"}},
                    ],
                },
                "phi_edge_discriminators": [{
                    "block": "merge",
                    "tag": "shared_pointer_edge_tag",
                    "bits": 32,
                    "incoming": [
                        {"edge": "edge_a", "predecessor": "pred_a", "value": 1},
                        {"edge": "edge_b", "predecessor": "pred_b", "value": 2},
                    ],
                }],
                "memory_defined_phis": contracts,
            },
        },
    }


class LiveSharedPhiEdgeDiscriminatorTests(unittest.TestCase):
    def validate(self, artifact):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        executor = LiveContinuationExecutor(LiveStateStore(temporary.name))
        return executor.create(artifact)

    def test_correlated_overlap_is_admitted(self):
        self.assertTrue(self.validate(program()))

    def test_assignment_drift_is_rejected(self):
        artifact = program()
        artifact["functions"]["main"]["phi_edge_discriminators"][0][
            "incoming"
        ][0]["value"] = 2
        with self.assertRaisesRegex(ValueError, "shared PHI edge"):
            self.validate(artifact)

        omitted_edge = program()
        omitted_edge["functions"]["main"][
            "phi_edge_discriminators"
        ][0]["incoming"].pop()
        with self.assertRaisesRegex(ValueError, "shared PHI edge"):
            self.validate(omitted_edge)

    def test_capability_and_contract_are_closed(self):
        missing_capability = program()
        missing_capability["lowering"]["capabilities"].remove(
            "bounded-shared-phi-edge-discriminator"
        )
        with self.assertRaisesRegex(ValueError, "shared PHI edge"):
            self.validate(missing_capability)

        missing_contract = program()
        missing_contract["functions"]["main"].pop(
            "phi_edge_discriminators"
        )
        with self.assertRaisesRegex(ValueError, "shared PHI edge"):
            self.validate(missing_contract)

    def test_definition_outside_declared_edges_is_rejected(self):
        artifact = program()
        artifact["functions"]["main"]["blocks"]["entry"].insert(0, {
            "op": "const",
            "dst": "shared_pointer_edge_tag",
            "value": 1,
            "bits": 32,
        })
        with self.assertRaisesRegex(ValueError, "shared PHI edge"):
            self.validate(artifact)

    def test_correlated_load_must_use_registered_tag(self):
        artifact = program()
        load = artifact["functions"]["main"]["blocks"]["merge"][0]
        for case in load["alias_cases"]:
            case["guards"][0]["value"] = {"var": "unregistered_tag"}
        with self.assertRaisesRegex(ValueError, "does not guard every"):
            self.validate(artifact)


if __name__ == "__main__":
    unittest.main()
