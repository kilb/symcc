#!/usr/bin/env python3
"""Path-optimal initial symbolic heaps for live SymCC continuations.

The domain is a conservative C adaptation of POSE.  Every symbolic reference
has a stable proxy object.  Null, alias, and fresh-object alternatives remain
inside reference-equality ITE expressions; heap refinement therefore never
forks an execution state.  Only :meth:`PoseHeapState.branch_alias`, which
represents an actual CFG decision, creates child states.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from typing import Any, Iterable, Mapping


POSE_HEAP_SCHEMA = "symcc-pose-symbolic-heap-v1"
POSE_CHECKPOINT_MARKER = "@pose:initial-heap"
NULL_REFERENCE = "null"
_DIGEST_LENGTH = 64
_MAX_TYPES = 128
_MAX_REFERENCES = 256
_MAX_OBJECTS = 256
_MAX_TERMS = 131_072
_MAX_CELLS = 1_048_576
_MAX_CONDITIONS = 65_536


class PoseHeapError(ValueError):
    """The initial symbolic heap contract was violated."""


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise PoseHeapError("POSE heap value is not canonical JSON") from error


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _is_digest(value: Any) -> bool:
    text = str(value)
    return (
        len(text) == _DIGEST_LENGTH
        and all(character in "0123456789abcdef" for character in text)
    )


def _bounded_name(value: Any, what: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 256
        or "\x00" in value
    ):
        raise PoseHeapError(f"invalid {what}")
    return value


def _bounded_uint(value: Any, what: str, maximum: int) -> int:
    if isinstance(value, bool) or (
        isinstance(value, float) and not value.is_integer()
    ):
        raise PoseHeapError(f"invalid {what}")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise PoseHeapError(f"invalid {what}") from error
    if parsed < 0 or parsed > maximum:
        raise PoseHeapError(f"invalid {what}")
    return parsed


@dataclass(frozen=True)
class PoseTypeLayout:
    """A bounded C object layout admitted to the symbolic initial heap."""

    name: str
    size: int
    alignment: int = 1

    def normalized(self) -> "PoseTypeLayout":
        name = _bounded_name(self.name, "POSE type name")
        size = _bounded_uint(self.size, "POSE type size", 1 << 20)
        alignment = _bounded_uint(
            self.alignment, "POSE type alignment", 1 << 16
        )
        if size == 0 or alignment == 0 or alignment & (alignment - 1):
            raise PoseHeapError("POSE type size/alignment is invalid")
        return PoseTypeLayout(name, size, alignment)

    def to_mapping(self) -> dict[str, Any]:
        value = self.normalized()
        return {
            "alignment": value.alignment,
            "name": value.name,
            "size": value.size,
        }


@dataclass(frozen=True)
class PoseAccessResult:
    """Symbolic bytes and guards produced by one non-forking heap access."""

    values: tuple[str, ...]
    initialized: str
    live: str
    valid: str


@dataclass(frozen=True)
class PoseConcreteHeap:
    """A concrete initial object graph materialized from a solver model."""

    roots: Mapping[str, str | None]
    objects: Mapping[str, Mapping[str, Any]]


class PoseHeapState:
    """Functionally immutable POSE-C heap state.

    Public operations return a new state.  The old state remains suitable for
    another continuation child and serializes to the same content digest.
    """

    def __init__(
        self,
        *,
        layouts: Mapping[str, PoseTypeLayout],
        roots: Mapping[str, str],
        references: Mapping[str, Mapping[str, Any]],
        objects: Mapping[str, Mapping[str, Any]] | None = None,
        terms: Mapping[str, Mapping[str, Any]] | None = None,
        equalities: Iterable[tuple[str, str]] = (),
        disequalities: Iterable[tuple[str, str]] = (),
        conditions: Iterable[str] = (),
        metrics: Mapping[str, int] | None = None,
    ) -> None:
        self._layouts = dict(layouts)
        self._roots = dict(roots)
        self._references = {
            key: dict(value) for key, value in references.items()
        }
        self._objects = {
            key: {
                **dict(value),
                "cells": {
                    int(offset): tuple(cell)
                    for offset, cell in dict(value.get("cells", {})).items()
                },
            }
            for key, value in (objects or {}).items()
        }
        self._terms = {key: dict(value) for key, value in (terms or {}).items()}
        self._equalities = tuple(sorted({self._pair(*pair) for pair in equalities}))
        self._disequalities = tuple(
            sorted({self._pair(*pair) for pair in disequalities})
        )
        self._conditions = tuple(dict.fromkeys(str(item) for item in conditions))
        base_metrics = {
            "cfg_forks": 0,
            "heap_refinements": 0,
            "loads": 0,
            "stores": 0,
            "frees": 0,
        }
        base_metrics.update({
            str(key): int(value) for key, value in (metrics or {}).items()
        })
        self._metrics = base_metrics

    @classmethod
    def create(
        cls,
        layouts: Iterable[PoseTypeLayout],
        roots: Iterable[tuple[str, str]],
    ) -> "PoseHeapState":
        normalized_layouts: dict[str, PoseTypeLayout] = {}
        for raw in layouts:
            if not isinstance(raw, PoseTypeLayout):
                raise PoseHeapError("POSE layouts must be PoseTypeLayout values")
            layout = raw.normalized()
            if layout.name in normalized_layouts:
                raise PoseHeapError("duplicate POSE type layout")
            normalized_layouts[layout.name] = layout
            if len(normalized_layouts) > _MAX_TYPES:
                raise PoseHeapError("POSE type budget exceeded")
        if not normalized_layouts:
            raise PoseHeapError("POSE heap needs at least one type")
        root_map: dict[str, str] = {}
        references: dict[str, dict[str, Any]] = {}
        for raw_name, raw_type in roots:
            name = _bounded_name(raw_name, "POSE root name")
            type_name = _bounded_name(raw_type, "POSE root type")
            if name in root_map or type_name not in normalized_layouts:
                raise PoseHeapError("invalid or duplicate POSE root")
            reference = _digest({
                "kind": "pose-root-reference-v1",
                "name": name,
                "type": type_name,
            })
            root_map[name] = reference
            references[reference] = {
                "origin": f"root:{name}",
                "type": type_name,
            }
        if not root_map:
            raise PoseHeapError("POSE heap needs at least one root")
        return cls(
            layouts=normalized_layouts,
            roots=root_map,
            references=references,
        )

    @staticmethod
    def _pair(left: str, right: str) -> tuple[str, str]:
        left = str(left)
        right = str(right)
        return (left, right) if left <= right else (right, left)

    def _clone(self) -> "PoseHeapState":
        return PoseHeapState(
            layouts=self._layouts,
            roots=self._roots,
            references=self._references,
            objects=self._objects,
            terms=self._terms,
            equalities=self._equalities,
            disequalities=self._disequalities,
            conditions=self._conditions,
            metrics=self._metrics,
        )

    @property
    def roots(self) -> dict[str, str]:
        return dict(self._roots)

    @property
    def conditions(self) -> tuple[str, ...]:
        return self._conditions

    @property
    def metrics(self) -> dict[str, int]:
        return dict(self._metrics)

    @property
    def digest(self) -> str:
        return _digest(self.to_mapping())

    def reference(self, root_or_reference: str) -> str:
        value = self._roots.get(str(root_or_reference), str(root_or_reference))
        if value not in self._references:
            raise PoseHeapError("unknown POSE reference")
        return value

    def _parents(self) -> dict[str, str]:
        parents = {
            reference: reference
            for reference in (*self._references, NULL_REFERENCE)
        }

        def find(value: str) -> str:
            while parents[value] != value:
                parents[value] = parents[parents[value]]
                value = parents[value]
            return value

        for left, right in self._equalities:
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                low, high = sorted((left_root, right_root))
                parents[high] = low
        for value in tuple(parents):
            parents[value] = find(value)
        return parents

    def _relation(self, left: str, right: str) -> bool | None:
        parents = self._parents()
        if parents[left] == parents[right]:
            return True
        for first, second in self._disequalities:
            if {
                parents[first], parents[second]
            } == {parents[left], parents[right]}:
                return False
        return None

    def _intern(self, node: Mapping[str, Any]) -> str:
        normalized = json.loads(_canonical(dict(node)).decode("ascii"))
        digest = _digest(normalized)
        self._terms[digest] = normalized
        if len(self._terms) > _MAX_TERMS:
            raise PoseHeapError("POSE term budget exceeded")
        return digest

    def _constant(self, value: int, bits: int) -> str:
        return self._intern({
            "bits": bits,
            "op": "const",
            "value": int(value) & ((1 << bits) - 1),
        })

    def _ref_eq(self, left: str, right: str) -> str:
        relation = self._relation(left, right)
        if relation is not None:
            return self._constant(int(relation), 1)
        return self._intern({
            "bits": 1,
            "left": left,
            "op": "ref_eq",
            "right": right,
        })

    def _not(self, term: str) -> str:
        node = self._terms[term]
        if node["op"] == "const":
            return self._constant(0 if int(node["value"]) else 1, 1)
        return self._intern({"args": [term], "bits": 1, "op": "not"})

    def _and(self, terms: Iterable[str]) -> str:
        unique: list[str] = []
        for term in terms:
            node = self._terms[term]
            if node["op"] == "const":
                if int(node["value"]) == 0:
                    return self._constant(0, 1)
                continue
            if term not in unique:
                unique.append(term)
        if not unique:
            return self._constant(1, 1)
        if len(unique) == 1:
            return unique[0]
        return self._intern({"args": unique, "bits": 1, "op": "and"})

    def _ite(self, condition: str, when_true: str, when_false: str) -> str:
        if when_true == when_false:
            return when_true
        condition_node = self._terms[condition]
        if condition_node["op"] == "const":
            return when_true if int(condition_node["value"]) else when_false
        bits = int(self._terms[when_true]["bits"])
        if int(self._terms[when_false]["bits"]) != bits:
            raise PoseHeapError("POSE ITE arms have different widths")
        return self._intern({
            "args": [condition, when_true, when_false],
            "bits": bits,
            "op": "ite",
        })

    def _concat_little_endian(self, values: Iterable[str]) -> str:
        arguments = tuple(values)
        if not 1 <= len(arguments) <= 8 or any(
            item not in self._terms or int(self._terms[item]["bits"]) != 8
            for item in arguments
        ):
            raise PoseHeapError("POSE word needs one to eight byte terms")
        if len(arguments) == 1:
            return arguments[0]
        return self._intern({
            "args": list(arguments),
            "bits": len(arguments) * 8,
            "op": "concat_le",
        })

    def _append_condition(self, term: str) -> None:
        if term not in self._conditions:
            self._conditions += (term,)
            if len(self._conditions) > _MAX_CONDITIONS:
                raise PoseHeapError("POSE condition budget exceeded")

    def _materialize(self, reference: str) -> None:
        if reference in self._objects:
            return
        metadata = self._references[reference]
        type_name = str(metadata["type"])
        layout = self._layouts[type_name]
        object_id = _digest({
            "kind": "pose-proxy-object-v1",
            "reference": reference,
            "type": type_name,
        })
        live = self._constant(1, 1)
        self._objects[reference] = {
            "cells": {},
            "live": live,
            "object_id": object_id,
            "ordinal": len(self._objects),
            "size": layout.size,
            "type": type_name,
        }
        nonnull = self._not(self._ref_eq(reference, NULL_REFERENCE))
        self._append_condition(nonnull)
        pair = self._pair(reference, NULL_REFERENCE)
        if pair not in self._disequalities:
            self._disequalities = tuple(sorted((*self._disequalities, pair)))
        self._metrics["heap_refinements"] += 1
        if len(self._objects) > _MAX_OBJECTS:
            raise PoseHeapError("POSE object budget exceeded")

    def derive_reference(
        self,
        source_term: str,
        type_name: str,
        *,
        label: str = "field",
    ) -> tuple["PoseHeapState", str]:
        state = self._clone()
        if (
            source_term not in state._terms
            or int(state._terms[source_term]["bits"]) != 64
        ):
            raise PoseHeapError(
                "derived POSE reference needs a 64-bit source term"
            )
        type_name = _bounded_name(type_name, "POSE derived reference type")
        if type_name not in state._layouts:
            raise PoseHeapError("derived POSE reference has unknown type")
        label = _bounded_name(label, "POSE derived reference label")
        reference = _digest({
            "kind": "pose-derived-reference-v1",
            "label": label,
            "source": source_term,
            "type": type_name,
        })
        existing = state._references.get(reference)
        metadata = {"origin": f"term:{source_term}:{label}", "type": type_name}
        if existing is not None and existing != metadata:
            raise PoseHeapError("POSE derived reference identity collision")
        state._references[reference] = metadata
        if len(state._references) > _MAX_REFERENCES:
            raise PoseHeapError("POSE reference budget exceeded")
        return state, reference

    def load_word(
        self,
        root_or_reference: str,
        offset: int,
        width: int,
    ) -> tuple["PoseHeapState", str, PoseAccessResult]:
        if width not in {1, 2, 4, 8}:
            raise PoseHeapError("POSE word width must be 1, 2, 4, or 8")
        state, access = self.load(root_or_reference, offset, width)
        word = state._concat_little_endian(access.values)
        return state, word, access

    def load_reference(
        self,
        root_or_reference: str,
        offset: int,
        type_name: str,
        *,
        label: str,
    ) -> tuple["PoseHeapState", str, PoseAccessResult]:
        state, word, access = self.load_word(root_or_reference, offset, 8)
        state, reference = state.derive_reference(
            word, type_name, label=label
        )
        return state, reference, access

    def _initialize_cell(self, reference: str, offset: int) -> None:
        memory_object = self._objects[reference]
        cells = memory_object["cells"]
        if offset in cells:
            return
        fresh = self._intern({
            "bits": 8,
            "name": f"byte:{memory_object['object_id']}:{offset}",
            "op": "var",
        })
        value = fresh
        candidates = sorted(
            (
                (int(candidate["ordinal"]), candidate_reference)
                for candidate_reference, candidate in self._objects.items()
                if candidate_reference != reference
                and candidate["type"] == memory_object["type"]
                and int(candidate["ordinal"]) < int(memory_object["ordinal"])
            ),
            reverse=True,
        )
        for _ordinal, candidate_reference in candidates:
            self._initialize_cell(candidate_reference, offset)
            candidate_value = self._objects[candidate_reference]["cells"][offset][0]
            value = self._ite(
                self._ref_eq(reference, candidate_reference),
                candidate_value,
                value,
            )
        initialized = self._constant(1, 1)
        cells[offset] = (value, initialized)
        if sum(len(item["cells"]) for item in self._objects.values()) > _MAX_CELLS:
            raise PoseHeapError("POSE cell budget exceeded")

    def load(
        self,
        root_or_reference: str,
        offset: int,
        width: int = 1,
    ) -> tuple["PoseHeapState", PoseAccessResult]:
        state = self._clone()
        reference = state.reference(root_or_reference)
        offset = _bounded_uint(offset, "POSE load offset", 1 << 20)
        width = _bounded_uint(width, "POSE load width", 1 << 20)
        if width == 0:
            raise PoseHeapError("POSE load width is zero")
        state._materialize(reference)
        memory_object = state._objects[reference]
        if offset + width > int(memory_object["size"]):
            raise PoseHeapError("POSE load is outside object bounds")
        values: list[str] = []
        initialized: list[str] = []
        for byte_offset in range(offset, offset + width):
            state._initialize_cell(reference, byte_offset)
            value, init = memory_object["cells"][byte_offset]
            values.append(value)
            initialized.append(init)
        init_guard = state._and(initialized)
        live = str(memory_object["live"])
        valid = state._and((live, init_guard))
        state._append_condition(live)
        state._metrics["loads"] += 1
        return state, PoseAccessResult(tuple(values), init_guard, live, valid)

    def store(
        self,
        root_or_reference: str,
        offset: int,
        values: Iterable[int | str],
    ) -> "PoseHeapState":
        state = self._clone()
        reference = state.reference(root_or_reference)
        offset = _bounded_uint(offset, "POSE store offset", 1 << 20)
        raw_values = tuple(values)
        if not raw_values:
            raise PoseHeapError("POSE store has no bytes")
        state._materialize(reference)
        target = state._objects[reference]
        if offset + len(raw_values) > int(target["size"]):
            raise PoseHeapError("POSE store is outside object bounds")
        normalized: list[str] = []
        for raw in raw_values:
            if isinstance(raw, bool):
                raise PoseHeapError("POSE store byte is invalid")
            if isinstance(raw, int):
                if not 0 <= raw <= 255:
                    raise PoseHeapError("POSE store byte is outside uint8")
                normalized.append(state._constant(raw, 8))
            else:
                digest = str(raw)
                if digest not in state._terms or int(state._terms[digest]["bits"]) != 8:
                    raise PoseHeapError("POSE store term is not an 8-bit value")
                normalized.append(digest)
        compatible = [
            candidate_reference
            for candidate_reference, memory_object in state._objects.items()
            if memory_object["type"] == target["type"]
        ]
        old_cells: dict[tuple[str, int], tuple[str, str]] = {}
        for candidate_reference in compatible:
            for byte_offset in range(offset, offset + len(normalized)):
                state._initialize_cell(candidate_reference, byte_offset)
                old_cells[(candidate_reference, byte_offset)] = tuple(
                    state._objects[candidate_reference]["cells"][byte_offset]
                )
        initialized = state._constant(1, 1)
        for candidate_reference in compatible:
            condition = state._ref_eq(reference, candidate_reference)
            for index, value in enumerate(normalized):
                byte_offset = offset + index
                old_value, old_init = old_cells[(candidate_reference, byte_offset)]
                if candidate_reference == reference:
                    cell = (value, initialized)
                else:
                    cell = (
                        state._ite(condition, value, old_value),
                        state._ite(condition, initialized, old_init),
                    )
                state._objects[candidate_reference]["cells"][byte_offset] = cell
        state._append_condition(str(target["live"]))
        state._metrics["stores"] += 1
        return state

    def free(self, root_or_reference: str) -> "PoseHeapState":
        state = self._clone()
        reference = state.reference(root_or_reference)
        state._materialize(reference)
        target = state._objects[reference]
        state._append_condition(str(target["live"]))
        dead = state._constant(0, 1)
        for candidate_reference, memory_object in state._objects.items():
            if memory_object["type"] != target["type"]:
                continue
            if candidate_reference == reference:
                memory_object["live"] = dead
            else:
                memory_object["live"] = state._ite(
                    state._ref_eq(reference, candidate_reference),
                    dead,
                    str(memory_object["live"]),
                )
        state._metrics["frees"] += 1
        return state

    def assume_alias(
        self,
        left: str,
        right: str,
        equal: bool,
    ) -> "PoseHeapState | None":
        state = self._clone()
        left_reference = (
            NULL_REFERENCE if left == NULL_REFERENCE else state.reference(left)
        )
        right_reference = (
            NULL_REFERENCE if right == NULL_REFERENCE else state.reference(right)
        )
        if (
            equal
            and left_reference != NULL_REFERENCE
            and right_reference != NULL_REFERENCE
            and state._references[left_reference]["type"]
            != state._references[right_reference]["type"]
        ):
            return None
        relation = state._relation(left_reference, right_reference)
        if relation is not None:
            return state if relation is bool(equal) else None
        pair = state._pair(left_reference, right_reference)
        if equal:
            state._equalities = tuple(sorted((*state._equalities, pair)))
        else:
            state._disequalities = tuple(sorted((*state._disequalities, pair)))
        return state

    def branch_alias(
        self,
        left: str,
        right: str,
    ) -> tuple["PoseHeapState", ...]:
        """Fork only for a real CFG reference-comparison decision."""
        children: list[PoseHeapState] = []
        for equal in (True, False):
            child = self.assume_alias(left, right, equal)
            if child is None:
                continue
            child._metrics["cfg_forks"] += 1
            children.append(child)
        return tuple(children)

    def evaluate(self, term: str, model: Mapping[str, Any]) -> int:
        if term not in self._terms:
            raise PoseHeapError("unknown POSE term")
        aliases_raw = model.get("references", {})
        values_raw = model.get("values", {})
        if not isinstance(aliases_raw, Mapping) or not isinstance(values_raw, Mapping):
            raise PoseHeapError("POSE model must contain mapping domains")
        aliases = {
            self._roots.get(str(key), str(key)): value
            for key, value in aliases_raw.items()
        }
        parents = self._parents()

        def reference_value(reference: str) -> Any:
            if reference == NULL_REFERENCE:
                return None
            if reference in aliases:
                return aliases[reference]
            representative = parents[reference]
            if representative in aliases:
                return aliases[representative]
            return representative

        memo: dict[str, int] = {}

        def visit(digest: str, active: set[str]) -> int:
            if digest in memo:
                return memo[digest]
            if digest in active:
                raise PoseHeapError("cycle in POSE term DAG")
            active.add(digest)
            node = self._terms[digest]
            op = node["op"]
            if op == "const":
                value = int(node["value"])
            elif op == "var":
                value = int(values_raw.get(str(node["name"]), 0))
            elif op == "ref_eq":
                value = int(
                    reference_value(str(node["left"]))
                    == reference_value(str(node["right"]))
                )
            else:
                arguments = [visit(str(item), set(active)) for item in node["args"]]
                if op == "not":
                    value = int(not arguments[0])
                elif op == "and":
                    value = int(all(arguments))
                elif op == "ite":
                    value = arguments[1] if arguments[0] else arguments[2]
                elif op == "concat_le":
                    value = sum(
                        byte << (8 * index)
                        for index, byte in enumerate(arguments)
                    )
                else:
                    raise PoseHeapError("unknown POSE term operation")
            bits = int(node["bits"])
            result = value & ((1 << bits) - 1)
            memo[digest] = result
            return result

        return visit(term, set())

    def validate_model(self, model: Mapping[str, Any]) -> bool:
        """Check a solver model against quotient, disequality, and path guards."""
        if not isinstance(model, Mapping):
            raise PoseHeapError("POSE model is not a mapping")
        aliases_raw = model.get("references", {})
        values_raw = model.get("values", {})
        if not isinstance(aliases_raw, Mapping) or not isinstance(values_raw, Mapping):
            raise PoseHeapError("POSE model must contain mapping domains")
        aliases: dict[str, str | None] = {}
        for raw_reference, raw_value in aliases_raw.items():
            reference = self._roots.get(str(raw_reference), str(raw_reference))
            if reference not in self._references:
                raise PoseHeapError("POSE model names an unknown reference")
            if raw_value is not None and (
                not isinstance(raw_value, str)
                or not raw_value
                or len(raw_value.encode("utf-8")) > 256
                or "\x00" in raw_value
            ):
                raise PoseHeapError("POSE model has an invalid object handle")
            aliases[reference] = raw_value
        variable_widths = {
            str(node["name"]): int(node["bits"])
            for node in self._terms.values()
            if node["op"] == "var"
        }
        for raw_name, raw_value in values_raw.items():
            name = str(raw_name)
            if name not in variable_widths:
                raise PoseHeapError("POSE model names an unknown scalar")
            if type(raw_value) is not int:
                raise PoseHeapError("POSE model scalar is not an integer")
            _bounded_uint(
                raw_value,
                "POSE model scalar",
                (1 << variable_widths[name]) - 1,
            )
        parents = self._parents()

        def value(reference: str) -> str | None:
            if reference == NULL_REFERENCE:
                return None
            representative = parents[reference]
            return aliases.get(reference, aliases.get(representative, representative))

        if any(value(left) != value(right) for left, right in self._equalities):
            return False
        if any(value(left) == value(right) for left, right in self._disequalities):
            return False
        return all(bool(self.evaluate(condition, model)) for condition in self._conditions)

    def to_smt2(self) -> str:
        """Lower the complete heap formula to deterministic QF_BV SMT-LIB2."""
        reference_symbol = {
            reference: f"r_{reference}"
            for reference in self._references
        }
        variable_symbol = {
            digest: f"v_{digest}"
            for digest, node in self._terms.items()
            if node["op"] == "var"
        }
        memo: dict[str, str] = {}

        def ref(reference: str) -> str:
            return (
                "(_ bv0 64)"
                if reference == NULL_REFERENCE
                else reference_symbol[reference]
            )

        def term(digest: str) -> str:
            cached = memo.get(digest)
            if cached is not None:
                return cached
            node = self._terms[digest]
            op = node["op"]
            bits = int(node["bits"])
            if op == "const":
                result = f"(_ bv{int(node['value'])} {bits})"
            elif op == "var":
                result = variable_symbol[digest]
            elif op == "ref_eq":
                result = (
                    f"(ite (= {ref(str(node['left']))} "
                    f"{ref(str(node['right']))}) #b1 #b0)"
                )
            else:
                arguments = [term(str(item)) for item in node["args"]]
                if op == "not":
                    result = f"(ite (= {arguments[0]} #b0) #b1 #b0)"
                elif op == "and":
                    predicate = " ".join(
                        f"(= {argument} #b1)" for argument in arguments
                    )
                    result = f"(ite (and {predicate}) #b1 #b0)"
                elif op == "ite":
                    result = (
                        f"(ite (= {arguments[0]} #b1) "
                        f"{arguments[1]} {arguments[2]})"
                    )
                elif op == "concat_le":
                    result = arguments[-1]
                    for argument in reversed(arguments[:-1]):
                        result = f"(concat {result} {argument})"
                else:
                    raise PoseHeapError("unknown POSE term operation")
            memo[digest] = result
            return result

        lines = ["(set-logic QF_BV)"]
        lines.extend(
            f"(declare-fun {reference_symbol[reference]} () (_ BitVec 64))"
            for reference in sorted(reference_symbol)
        )
        lines.extend(
            f"(declare-fun {variable_symbol[digest]} () "
            f"(_ BitVec {int(self._terms[digest]['bits'])}))"
            for digest in sorted(variable_symbol)
        )
        for left, right in self._equalities:
            lines.append(f"(assert (= {ref(left)} {ref(right)}))")
        for left, right in self._disequalities:
            lines.append(f"(assert (not (= {ref(left)} {ref(right)})))")
        lines.extend(
            f"(assert (= {term(condition)} #b1))"
            for condition in self._conditions
        )
        lines.append("(check-sat)")
        return "\n".join(lines) + "\n"

    def materialize_model(self, model: Mapping[str, Any]) -> PoseConcreteHeap:
        if not self.validate_model(model):
            raise PoseHeapError("POSE model violates the symbolic heap constraints")
        aliases_raw = model.get("references", {})
        if not isinstance(aliases_raw, Mapping):
            raise PoseHeapError("POSE model reference domain is invalid")
        aliases = {
            self._roots.get(str(key), str(key)): value
            for key, value in aliases_raw.items()
        }
        concrete_roots: dict[str, str | None] = {}
        for name, reference in self._roots.items():
            raw = aliases.get(reference, reference)
            concrete_roots[name] = None if raw is None else str(raw)
        concrete_objects: dict[str, dict[str, Any]] = {}
        for reference, memory_object in sorted(
            self._objects.items(), key=lambda item: int(item[1]["ordinal"])
        ):
            handle_raw = aliases.get(reference, reference)
            if handle_raw is None:
                continue
            handle = str(handle_raw)
            cells = {
                str(offset): self.evaluate(value, model)
                for offset, (value, _initialized) in memory_object["cells"].items()
            }
            initialized = {
                str(offset): bool(self.evaluate(init, model))
                for offset, (_value, init) in memory_object["cells"].items()
            }
            candidate = {
                "bytes": cells,
                "initialized": initialized,
                "live": bool(self.evaluate(str(memory_object["live"]), model)),
                "size": int(memory_object["size"]),
                "type": str(memory_object["type"]),
            }
            previous = concrete_objects.get(handle)
            if previous is not None:
                for key, value in cells.items():
                    if key in previous["bytes"] and previous["bytes"][key] != value:
                        raise PoseHeapError("POSE model violates alias-cell equality")
                    previous["bytes"][key] = value
                    previous["initialized"][key] = initialized[key]
                previous["live"] = previous["live"] and candidate["live"]
            else:
                concrete_objects[handle] = candidate
        return PoseConcreteHeap(concrete_roots, concrete_objects)

    def to_mapping(self) -> dict[str, Any]:
        objects = []
        for reference, memory_object in sorted(
            self._objects.items(), key=lambda item: int(item[1]["ordinal"])
        ):
            objects.append({
                "cells": [
                    [offset, value, initialized]
                    for offset, (value, initialized)
                    in sorted(memory_object["cells"].items())
                ],
                "live": memory_object["live"],
                "object_id": memory_object["object_id"],
                "ordinal": memory_object["ordinal"],
                "reference": reference,
                "size": memory_object["size"],
                "type": memory_object["type"],
            })
        return {
            "conditions": list(self._conditions),
            "disequalities": [list(pair) for pair in self._disequalities],
            "equalities": [list(pair) for pair in self._equalities],
            "layouts": [
                self._layouts[name].to_mapping() for name in sorted(self._layouts)
            ],
            "metrics": dict(sorted(self._metrics.items())),
            "objects": objects,
            "references": [
                {
                    "id": reference,
                    "origin": metadata["origin"],
                    "type": metadata["type"],
                }
                for reference, metadata in sorted(self._references.items())
            ],
            "roots": [[name, reference] for name, reference in sorted(self._roots.items())],
            "schema": POSE_HEAP_SCHEMA,
            "terms": [
                [digest, self._terms[digest]] for digest in sorted(self._terms)
            ],
        }

    @classmethod
    def from_mapping(cls, raw: Any) -> "PoseHeapState":
        if not isinstance(raw, dict) or set(raw) != {
            "conditions", "disequalities", "equalities", "layouts",
            "metrics", "objects", "references", "roots", "schema", "terms",
        } or raw.get("schema") != POSE_HEAP_SCHEMA:
            raise PoseHeapError("invalid POSE heap snapshot schema")
        layouts_raw = raw["layouts"]
        references_raw = raw["references"]
        roots_raw = raw["roots"]
        objects_raw = raw["objects"]
        terms_raw = raw["terms"]
        if not all(isinstance(value, list) for value in (
            layouts_raw, references_raw, roots_raw, objects_raw, terms_raw,
        )):
            raise PoseHeapError("invalid POSE heap snapshot collections")
        if (
            len(layouts_raw) > _MAX_TYPES
            or len(references_raw) > _MAX_REFERENCES
            or len(objects_raw) > _MAX_OBJECTS
            or len(terms_raw) > _MAX_TERMS
        ):
            raise PoseHeapError("POSE heap snapshot exceeds budget")
        layouts: dict[str, PoseTypeLayout] = {}
        for item in layouts_raw:
            if not isinstance(item, dict) or set(item) != {"alignment", "name", "size"}:
                raise PoseHeapError("invalid POSE type snapshot")
            layout = PoseTypeLayout(
                item["name"], item["size"], item["alignment"]
            ).normalized()
            if layout.name in layouts:
                raise PoseHeapError("duplicate POSE type snapshot")
            layouts[layout.name] = layout
        references: dict[str, dict[str, Any]] = {}
        for item in references_raw:
            if not isinstance(item, dict) or set(item) != {"id", "origin", "type"}:
                raise PoseHeapError("invalid POSE reference snapshot")
            reference = str(item["id"])
            type_name = _bounded_name(item["type"], "POSE reference type")
            origin = _bounded_name(item["origin"], "POSE reference origin")
            if not _is_digest(reference) or reference in references or type_name not in layouts:
                raise PoseHeapError("invalid POSE reference identity")
            references[reference] = {"origin": origin, "type": type_name}
        roots: dict[str, str] = {}
        for item in roots_raw:
            if not isinstance(item, list) or len(item) != 2:
                raise PoseHeapError("invalid POSE root snapshot")
            name = _bounded_name(item[0], "POSE root name")
            reference = str(item[1])
            if name in roots or reference not in references:
                raise PoseHeapError("invalid POSE root reference")
            roots[name] = reference
        terms: dict[str, dict[str, Any]] = {}
        for item in terms_raw:
            if not isinstance(item, list) or len(item) != 2 or not isinstance(item[1], dict):
                raise PoseHeapError("invalid POSE term snapshot")
            digest, node = str(item[0]), dict(item[1])
            if not _is_digest(digest) or digest in terms or _digest(node) != digest:
                raise PoseHeapError("invalid POSE term identity")
            terms[digest] = node
        cls._validate_term_dag(terms, references)
        objects: dict[str, dict[str, Any]] = {}
        cell_count = 0
        for expected_ordinal, item in enumerate(objects_raw):
            if not isinstance(item, dict) or set(item) != {
                "cells", "live", "object_id", "ordinal", "reference",
                "size", "type",
            }:
                raise PoseHeapError("invalid POSE object snapshot")
            reference = str(item["reference"])
            type_name = str(item["type"])
            size = _bounded_uint(item["size"], "POSE object size", 1 << 20)
            ordinal = _bounded_uint(item["ordinal"], "POSE object ordinal", _MAX_OBJECTS)
            live = str(item["live"])
            expected_object = _digest({
                "kind": "pose-proxy-object-v1",
                "reference": reference,
                "type": type_name,
            })
            if (
                reference not in references
                or reference in objects
                or references[reference]["type"] != type_name
                or type_name not in layouts
                or size != layouts[type_name].size
                or ordinal != expected_ordinal
                or str(item["object_id"]) != expected_object
                or live not in terms
                or int(terms[live].get("bits", 0)) != 1
                or not isinstance(item["cells"], list)
            ):
                raise PoseHeapError("invalid POSE object identity")
            cells: dict[int, tuple[str, str]] = {}
            for cell in item["cells"]:
                if not isinstance(cell, list) or len(cell) != 3:
                    raise PoseHeapError("invalid POSE cell snapshot")
                offset = _bounded_uint(cell[0], "POSE cell offset", size - 1)
                value, initialized = str(cell[1]), str(cell[2])
                if (
                    offset in cells
                    or value not in terms
                    or initialized not in terms
                    or int(terms[value].get("bits", 0)) != 8
                    or int(terms[initialized].get("bits", 0)) != 1
                ):
                    raise PoseHeapError("invalid POSE cell term")
                cells[offset] = (value, initialized)
                cell_count += 1
            objects[reference] = {
                "cells": cells,
                "live": live,
                "object_id": expected_object,
                "ordinal": ordinal,
                "size": size,
                "type": type_name,
            }
        if cell_count > _MAX_CELLS:
            raise PoseHeapError("POSE cell snapshot exceeds budget")
        conditions = raw["conditions"]
        metrics_raw = raw["metrics"]
        metric_names = {
            "cfg_forks", "heap_refinements", "loads", "stores", "frees",
        }
        if (
            not isinstance(conditions, list)
            or len(conditions) > _MAX_CONDITIONS
            or len(set(str(item) for item in conditions)) != len(conditions)
        ):
            raise PoseHeapError("invalid POSE condition snapshot")
        if not isinstance(metrics_raw, dict) or set(metrics_raw) != metric_names:
            raise PoseHeapError("invalid POSE metric snapshot")
        metrics = {
            name: _bounded_uint(
                metrics_raw[name], f"POSE metric {name}", (1 << 63) - 1
            )
            for name in metric_names
        }
        state = cls(
            layouts=layouts,
            roots=roots,
            references=references,
            objects=objects,
            terms=terms,
            equalities=cls._parse_pairs(raw["equalities"]),
            disequalities=cls._parse_pairs(raw["disequalities"]),
            conditions=conditions,
            metrics=metrics,
        )
        state._validate_relations()
        if state.to_mapping() != raw:
            raise PoseHeapError("POSE heap snapshot is not canonical")
        return state

    @staticmethod
    def _parse_pairs(raw: Any) -> tuple[tuple[str, str], ...]:
        if not isinstance(raw, list) or len(raw) > _MAX_CONDITIONS:
            raise PoseHeapError("invalid POSE relation set")
        pairs: list[tuple[str, str]] = []
        for item in raw:
            if not isinstance(item, list) or len(item) != 2:
                raise PoseHeapError("invalid POSE relation")
            pairs.append((str(item[0]), str(item[1])))
        return tuple(pairs)

    @staticmethod
    def _validate_term_dag(
        terms: Mapping[str, Mapping[str, Any]],
        references: Mapping[str, Mapping[str, Any]],
    ) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(digest: str) -> None:
            if digest in visited:
                return
            if digest in visiting:
                raise PoseHeapError("cycle in POSE term snapshot")
            visiting.add(digest)
            node = terms[digest]
            op = node.get("op")
            bits = _bounded_uint(node.get("bits"), "POSE term width", 64)
            if bits == 0:
                raise PoseHeapError("POSE term has zero width")
            if op == "const":
                if set(node) != {"bits", "op", "value"}:
                    raise PoseHeapError("invalid POSE constant term")
                _bounded_uint(node["value"], "POSE constant", (1 << bits) - 1)
            elif op == "var":
                if set(node) != {"bits", "name", "op"}:
                    raise PoseHeapError("invalid POSE variable term")
                _bounded_name(node["name"], "POSE variable name")
            elif op == "ref_eq":
                if set(node) != {"bits", "left", "op", "right"} or bits != 1:
                    raise PoseHeapError("invalid POSE reference equality")
                for reference in (str(node["left"]), str(node["right"])):
                    if reference != NULL_REFERENCE and reference not in references:
                        raise PoseHeapError("POSE equality names unknown reference")
            elif op in {"not", "and", "ite", "concat_le"}:
                if set(node) != {"args", "bits", "op"} or not isinstance(node["args"], list):
                    raise PoseHeapError("invalid POSE compound term")
                expected = 1 if op == "not" else 3 if op == "ite" else None
                if expected is not None and len(node["args"]) != expected:
                    raise PoseHeapError("invalid POSE term arity")
                if op == "and" and (bits != 1 or len(node["args"]) < 2):
                    raise PoseHeapError("invalid POSE conjunction")
                if op == "concat_le" and not 2 <= len(node["args"]) <= 8:
                    raise PoseHeapError("invalid POSE little-endian word")
                for child in node["args"]:
                    child = str(child)
                    if child not in terms:
                        raise PoseHeapError("POSE term references missing child")
                    visit(child)
                child_bits = [int(terms[str(child)]["bits"]) for child in node["args"]]
                if op == "not" and (bits != 1 or child_bits != [1]):
                    raise PoseHeapError("invalid POSE negation widths")
                if op == "and" and any(value != 1 for value in child_bits):
                    raise PoseHeapError("invalid POSE conjunction widths")
                if op == "ite" and (
                    child_bits[0] != 1
                    or child_bits[1] != bits
                    or child_bits[2] != bits
                ):
                    raise PoseHeapError("invalid POSE ITE widths")
                if op == "concat_le" and (
                    bits != 8 * len(child_bits)
                    or any(value != 8 for value in child_bits)
                ):
                    raise PoseHeapError("invalid POSE word widths")
            else:
                raise PoseHeapError("unknown POSE term operation")
            visiting.remove(digest)
            visited.add(digest)

        for digest in terms:
            visit(digest)

    def _validate_relations(self) -> None:
        universe = {*self._references, NULL_REFERENCE}
        if not all(
            left in universe and right in universe and left <= right
            for relation in (self._equalities, self._disequalities)
            for left, right in relation
        ):
            raise PoseHeapError("POSE relation references unknown identity")
        if set(self._equalities) & set(self._disequalities):
            raise PoseHeapError("POSE relation is both equal and unequal")
        parents = self._parents()
        if any(parents[left] == parents[right] for left, right in self._disequalities):
            raise PoseHeapError("POSE disequality contradicts alias quotient")
        if len(self._conditions) > _MAX_CONDITIONS or any(
            term not in self._terms or int(self._terms[term].get("bits", 0)) != 1
            for term in self._conditions
        ):
            raise PoseHeapError("invalid POSE path condition")


def persist_pose_heap(
    store: Any,
    checkpoint_id: str,
    state: PoseHeapState,
) -> str:
    """Attach a canonical POSE heap to an immutable continuation checkpoint."""
    from distributed_state import LiveContinuationDescriptor

    if not isinstance(state, PoseHeapState):
        raise PoseHeapError("POSE checkpoint state has the wrong type")
    bundle = store.restore_continuation(checkpoint_id)
    expression = {
        "bits": 1,
        "op": "pose_heap_snapshot",
        "root": state.digest,
        "snapshot": state.to_mapping(),
    }
    heap_expression = store.put_expression(expression)
    values = dict(bundle.symbolic_store)
    values[POSE_CHECKPOINT_MARKER] = heap_expression
    symbolic_store = store.put_symbolic_store(values)
    descriptor = replace(
        bundle.descriptor,
        symbolic_store_root=symbolic_store,
        parent=checkpoint_id,
    )
    if not isinstance(descriptor, LiveContinuationDescriptor):
        raise AssertionError("invalid continuation descriptor replacement")
    return store.put_continuation(descriptor)


def restore_pose_heap(store: Any, checkpoint_id: str) -> PoseHeapState | None:
    """Recover and digest-check a POSE heap from a continuation checkpoint."""
    bundle = store.restore_continuation(checkpoint_id)
    values = dict(bundle.symbolic_store)
    heap_expression = values.get(POSE_CHECKPOINT_MARKER)
    if heap_expression is None:
        return None
    expression = store.get_expression(heap_expression)
    if not isinstance(expression, dict) or set(expression) != {
        "bits", "op", "root", "snapshot",
    } or expression.get("op") != "pose_heap_snapshot" or expression.get("bits") != 1:
        raise PoseHeapError("invalid POSE continuation payload")
    state = PoseHeapState.from_mapping(expression["snapshot"])
    if expression.get("root") != state.digest:
        raise PoseHeapError("POSE continuation payload digest mismatch")
    return state
