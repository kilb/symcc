"""Alpha-normalized Query IR constraint shapes.

The normalization preserves the complete reachable expression DAG while
renaming absolute input-byte indices by first structural occurrence.  It is
therefore suitable for recognizing constraints that differ only in which
input positions instantiate the same symbolic relation.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping, Sequence


SHAPE_SCHEMA = "symcc-alpha-constraint-shape-v1"
SHAPE_NODE_SCHEMA = "symcc-alpha-constraint-node-v1"
_MAX_NODES = 250_000
_MAX_ROOTS = 100_001


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class AlphaConstraintShape:
    shape_hash: str
    root_hashes: tuple[str, ...]
    read_count: int
    node_count: int

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": SHAPE_SCHEMA,
            "shape_hash": self.shape_hash,
            "root_hashes": list(self.root_hashes),
            "read_count": self.read_count,
            "node_count": self.node_count,
        }


class AlphaConstraintShapeIndex:
    """Validated Query IR DAG reusable across several root projections."""

    def __init__(self, nodes: Sequence[Mapping[str, Any]]) -> None:
        if (
            isinstance(nodes, (str, bytes))
            or not isinstance(nodes, Sequence)
            or not 1 <= len(nodes) <= _MAX_NODES
        ):
            raise ValueError("constraint shape requires a bounded non-empty node list")

        validated: list[dict[str, Any]] = []
        for position, raw in enumerate(nodes):
            if not isinstance(raw, Mapping):
                raise ValueError("constraint shape node must be an object")
            node_id = raw.get("id")
            op = raw.get("op")
            bits = raw.get("bits")
            children = raw.get("children", ())
            attrs = raw.get("attrs", {})
            if node_id != position or not _plain_int(node_id):
                raise ValueError("constraint shape node IDs must be dense and ordered")
            if not isinstance(op, str) or not op:
                raise ValueError("constraint shape operation must be non-empty text")
            if not _plain_int(bits) or not 1 <= bits <= 1 << 20:
                raise ValueError("constraint shape bit width is invalid")
            if (
                isinstance(children, (str, bytes))
                or not isinstance(children, Sequence)
                or len(children) > 3
                or any(
                    not _plain_int(child) or not 0 <= child < position
                    for child in children
                )
            ):
                raise ValueError("constraint shape children violate DAG order")
            if not isinstance(attrs, Mapping):
                raise ValueError("constraint shape attributes must be an object")
            normalized_attrs = dict(attrs)
            try:
                _canonical_json(normalized_attrs)
            except (TypeError, ValueError, UnicodeError) as error:
                raise ValueError(
                    "constraint shape attributes are not canonical JSON"
                ) from error
            if op == "read":
                index = normalized_attrs.get("index")
                if not _plain_int(index) or not 0 <= index <= (1 << 32) - 1:
                    raise ValueError("constraint shape read index is invalid")
            validated.append(
                {
                    "op": op,
                    "bits": bits,
                    "children": tuple(children),
                    "attrs": normalized_attrs,
                }
            )
        self.nodes = tuple(validated)

    def shape(self, roots: Sequence[int]) -> AlphaConstraintShape:
        if (
            isinstance(roots, (str, bytes))
            or not isinstance(roots, Sequence)
            or not 1 <= len(roots) <= _MAX_ROOTS
        ):
            raise ValueError("constraint shape requires bounded non-empty roots")

        normalized_roots: list[int] = []
        for root in roots:
            if not _plain_int(root) or not 0 <= root < len(self.nodes):
                raise ValueError("constraint shape root is invalid")
            normalized_roots.append(root)

        reachable: set[int] = set()
        read_slots: dict[int, int] = {}
        pending = list(reversed(normalized_roots))
        while pending:
            node_id = pending.pop()
            if node_id in reachable:
                continue
            reachable.add(node_id)
            node = self.nodes[node_id]
            if node["op"] == "read":
                index = int(node["attrs"]["index"])
                if index not in read_slots:
                    read_slots[index] = len(read_slots)
            pending.extend(reversed(node["children"]))

        node_hashes: dict[int, str] = {}
        for node_id in sorted(reachable):
            node = self.nodes[node_id]
            attrs = dict(node["attrs"])
            if node["op"] == "read":
                index = int(attrs.pop("index"))
                attrs["alpha_slot"] = read_slots[index]
            body = {
                "schema": SHAPE_NODE_SCHEMA,
                "op": node["op"],
                "bits": node["bits"],
                "children": [node_hashes[child] for child in node["children"]],
                "attrs": attrs,
            }
            node_hashes[node_id] = _digest(body)

        root_hashes = tuple(node_hashes[root] for root in normalized_roots)
        descriptor = {
            "schema": SHAPE_SCHEMA,
            "roots": list(root_hashes),
            "read_count": len(read_slots),
            "node_count": len(reachable),
        }
        return AlphaConstraintShape(
            shape_hash=_digest(descriptor),
            root_hashes=root_hashes,
            read_count=len(read_slots),
            node_count=len(reachable),
        )


def alpha_normalized_constraint_shape(
    nodes: Sequence[Mapping[str, Any]],
    roots: Sequence[int],
) -> AlphaConstraintShape:
    """Validate a Query IR DAG and return one alpha-equivalence shape."""

    return AlphaConstraintShapeIndex(nodes).shape(roots)
