#!/usr/bin/env python3
"""Deterministic incremental QF_BV bit-blasting and CNF identities.

The encoding uses one activation literal per Query IR root.  Root constraints
remain guarded in the permanent formula and a concrete solve supplies the
activation literals as assumptions.  This is the representation needed by
incremental SAT solvers and by assumption-scoped proof exchange.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence


BITBLAST_SCHEMA = "symcc-qfbv-bitblast-cnf-v1"
INCREMENT_SCHEMA = "symcc-qfbv-cnf-increment-v1"
ASSUMPTION_SCHEMA = "symcc-qfbv-cnf-assumptions-v1"
MAX_CNF_VARIABLES = 20_000_000
MAX_CNF_CLAUSES = 100_000_000


class QfbvBitBlastError(ValueError):
    """The Query IR graph cannot be represented by the bounded CNF contract."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _bounded_int(value: Any, name: str, lower: int, upper: int) -> int:
    if isinstance(value, bool):
        raise QfbvBitBlastError(f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise QfbvBitBlastError(f"{name} must be an integer") from error
    if not lower <= result <= upper:
        raise QfbvBitBlastError(f"{name} must be in [{lower}, {upper}]")
    return result


@dataclass(frozen=True)
class CnfIncrement:
    ordinal: int
    root_hash: str
    activation_literal: int
    first_clause_id: int
    last_clause_id: int
    max_variable: int
    parent_formula_sha256: str
    formula_sha256: str
    clause_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": INCREMENT_SCHEMA,
            "ordinal": self.ordinal,
            "root_hash": self.root_hash,
            "activation_literal": self.activation_literal,
            "first_clause_id": self.first_clause_id,
            "last_clause_id": self.last_clause_id,
            "max_variable": self.max_variable,
            "parent_formula_sha256": self.parent_formula_sha256,
            "formula_sha256": self.formula_sha256,
            "clause_sha256": self.clause_sha256,
        }


@dataclass(frozen=True)
class BitBlastPlan:
    query_id: str
    clauses: tuple[tuple[int, ...], ...]
    max_variable: int
    input_literals: tuple[tuple[int, tuple[int, ...]], ...]
    assumptions: tuple[int, ...]
    increments: tuple[CnfIncrement, ...]
    certificate: Mapping[str, Any]

    @property
    def formula_sha256(self) -> str:
        return str(self.certificate["formula_sha256"])

    @property
    def assumption_sha256(self) -> str:
        return str(self.certificate["assumption_sha256"])

    def dimacs(self, *, assumptions_as_units: bool = False) -> str:
        extra = len(self.assumptions) if assumptions_as_units else 0
        lines = [f"p cnf {self.max_variable} {len(self.clauses) + extra}"]
        lines.extend(" ".join(map(str, clause)) + " 0" for clause in self.clauses)
        if assumptions_as_units:
            lines.extend(f"{literal} 0" for literal in self.assumptions)
        return "\n".join(lines) + "\n"

    def input_bytes_from_model(self, true_variables: set[int]) -> dict[int, int]:
        result: dict[int, int] = {}
        for offset, literals in self.input_literals:
            value = 0
            for bit, literal in enumerate(literals):
                truth = abs(literal) in true_variables
                if literal < 0:
                    truth = not truth
                if truth:
                    value |= 1 << bit
            result[offset] = value
        return result


class _Encoder:
    def __init__(
        self,
        query_id: str,
        roots: Sequence[str],
        expressions: Mapping[str, Mapping[str, Any]],
        *,
        max_bits: int,
        max_nodes: int,
        max_input_bytes: int,
        max_variables: int,
        max_clauses: int,
    ) -> None:
        self.query_id = str(query_id)[:128]
        self.roots = tuple(str(root) for root in roots)
        self.expressions = expressions
        self.max_bits = max_bits
        self.max_nodes = max_nodes
        self.max_input_bytes = max_input_bytes
        self.max_variables = max_variables
        self.max_clauses = max_clauses
        self.clauses: list[tuple[int, ...]] = []
        self.next_variable = 1
        self.memo: dict[str, tuple[str, tuple[int, ...]]] = {}
        self.visiting: set[str] = set()
        self.inputs: dict[int, tuple[int, ...]] = {}
        self.operator_counts: dict[str, int] = {}
        self.maximum_width = 1
        self.true_literal = self._variable()
        self._clause((self.true_literal,))

    @property
    def false_literal(self) -> int:
        return -self.true_literal

    def _variable(self) -> int:
        if self.next_variable > self.max_variables:
            raise QfbvBitBlastError("bit-blast exceeds the CNF variable limit")
        result = self.next_variable
        self.next_variable += 1
        return result

    def _clause(self, literals: Sequence[int]) -> None:
        normalized: list[int] = []
        seen: set[int] = set()
        for raw in literals:
            literal = int(raw)
            if literal == 0:
                raise QfbvBitBlastError("CNF clauses cannot contain literal zero")
            if -literal in seen:
                return
            if literal not in seen:
                seen.add(literal)
                normalized.append(literal)
        if len(self.clauses) >= self.max_clauses:
            raise QfbvBitBlastError("bit-blast exceeds the CNF clause limit")
        self.clauses.append(tuple(normalized))

    def _is_true(self, literal: int) -> bool:
        return literal == self.true_literal

    def _is_false(self, literal: int) -> bool:
        return literal == self.false_literal

    def _and(self, left: int, right: int) -> int:
        if self._is_false(left) or self._is_false(right) or left == -right:
            return self.false_literal
        if self._is_true(left):
            return right
        if self._is_true(right) or left == right:
            return left
        output = self._variable()
        self._clause((-output, left))
        self._clause((-output, right))
        self._clause((output, -left, -right))
        return output

    def _or(self, left: int, right: int) -> int:
        if self._is_true(left) or self._is_true(right) or left == -right:
            return self.true_literal
        if self._is_false(left):
            return right
        if self._is_false(right) or left == right:
            return left
        output = self._variable()
        self._clause((output, -left))
        self._clause((output, -right))
        self._clause((-output, left, right))
        return output

    def _xor(self, left: int, right: int) -> int:
        if self._is_false(left):
            return right
        if self._is_false(right):
            return left
        if self._is_true(left):
            return -right
        if self._is_true(right):
            return -left
        if left == right:
            return self.false_literal
        if left == -right:
            return self.true_literal
        output = self._variable()
        self._clause((-left, -right, -output))
        self._clause((left, right, -output))
        self._clause((left, -right, output))
        self._clause((-left, right, output))
        return output

    def _mux(self, condition: int, when_true: int, when_false: int) -> int:
        if self._is_true(condition):
            return when_true
        if self._is_false(condition):
            return when_false
        if when_true == when_false:
            return when_true
        if when_true == -when_false:
            return self._xor(-condition, when_true)
        output = self._variable()
        self._clause((-condition, -when_true, output))
        self._clause((-condition, when_true, -output))
        self._clause((condition, -when_false, output))
        self._clause((condition, when_false, -output))
        return output

    def _reduce_and(self, values: Sequence[int]) -> int:
        result = self.true_literal
        for value in values:
            result = self._and(result, value)
        return result

    def _reduce_or(self, values: Sequence[int]) -> int:
        result = self.false_literal
        for value in values:
            result = self._or(result, value)
        return result

    def _add(self, left: Sequence[int], right: Sequence[int]) -> tuple[int, ...]:
        carry = self.false_literal
        result: list[int] = []
        for first, second in zip(left, right, strict=True):
            pair = self._xor(first, second)
            result.append(self._xor(pair, carry))
            carry = self._or(
                self._and(first, second),
                self._and(carry, pair),
            )
        return tuple(result)

    def _negate(self, value: Sequence[int]) -> tuple[int, ...]:
        one = (self.true_literal,) + (self.false_literal,) * (len(value) - 1)
        return self._add(tuple(-bit for bit in value), one)

    def _subtract(
        self, left: Sequence[int], right: Sequence[int]
    ) -> tuple[int, ...]:
        return self._add(left, self._negate(right))

    def _unsigned_lt(self, left: Sequence[int], right: Sequence[int]) -> int:
        equal = self.true_literal
        less = self.false_literal
        for first, second in reversed(tuple(zip(left, right, strict=True))):
            less = self._or(less, self._and(equal, self._and(-first, second)))
            equal = self._and(equal, -self._xor(first, second))
        return less

    def _equal_bits(self, left: Sequence[int], right: Sequence[int]) -> int:
        return self._reduce_and(
            tuple(-self._xor(first, second)
                  for first, second in zip(left, right, strict=True))
        )

    def _select_bits(
        self,
        condition: int,
        when_true: Sequence[int],
        when_false: Sequence[int],
    ) -> tuple[int, ...]:
        return tuple(
            self._mux(condition, first, second)
            for first, second in zip(when_true, when_false, strict=True)
        )

    def _unsigned_divrem(
        self, dividend: Sequence[int], divisor: Sequence[int]
    ) -> tuple[tuple[int, ...], tuple[int, ...]]:
        width = len(dividend)
        remainder = (self.false_literal,) * width
        quotient = [self.false_literal] * width
        for index in range(width - 1, -1, -1):
            shifted = (dividend[index],) + remainder[:-1]
            greater_equal = -self._unsigned_lt(shifted, divisor)
            remainder = self._select_bits(
                greater_equal,
                self._subtract(shifted, divisor),
                shifted,
            )
            quotient[index] = greater_equal
        return tuple(quotient), remainder

    def _shift(
        self,
        value: Sequence[int],
        amount: Sequence[int],
        *,
        direction: str,
    ) -> tuple[int, ...]:
        width = len(value)
        result = tuple(value)
        for stage, selector in enumerate(amount):
            distance = 1 << stage
            fill = result[-1] if direction == "ashr" else self.false_literal
            if distance >= width:
                shifted = (fill,) * width
            elif direction == "shl":
                shifted = (self.false_literal,) * distance + result[:-distance]
            else:
                shifted = result[distance:] + (fill,) * distance
            result = self._select_bits(selector, shifted, result)
        return result

    def _rotate(
        self,
        value: Sequence[int],
        amount: Sequence[int],
        *,
        left: bool,
    ) -> tuple[int, ...]:
        width = len(value)
        modulus = tuple(
            self.true_literal if (width >> bit) & 1 else self.false_literal
            for bit in range(len(amount))
        )
        _, reduced = self._unsigned_divrem(amount, modulus)
        result = tuple(value)
        for stage, selector in enumerate(reduced):
            distance = (1 << stage) % width
            if distance == 0:
                continue
            shifted = (
                result[-distance:] + result[:-distance]
                if left
                else result[distance:] + result[:distance]
            )
            result = self._select_bits(selector, shifted, result)
        return result

    def _require_arity(
        self, op: str, children: Sequence[str], *allowed: int
    ) -> None:
        if len(children) not in allowed:
            expected = "/".join(map(str, allowed))
            raise QfbvBitBlastError(
                f"{op} expects arity {expected}, got {len(children)}"
            )

    def _node(self, node_hash: str) -> tuple[str, tuple[int, ...]]:
        if node_hash in self.memo:
            return self.memo[node_hash]
        if node_hash in self.visiting:
            raise QfbvBitBlastError("Query IR contains an expression cycle")
        if len(self.memo) >= self.max_nodes:
            raise QfbvBitBlastError("reachable Query IR exceeds the node limit")
        raw = self.expressions.get(node_hash)
        if not isinstance(raw, Mapping):
            raise QfbvBitBlastError("Query IR references a missing expression")
        op = str(raw.get("op", ""))
        bits = _bounded_int(raw.get("bits"), "node bits", 1, self.max_bits)
        children_raw = raw.get("children", ())
        attrs = raw.get("attrs", {})
        if not isinstance(children_raw, list) or not isinstance(attrs, Mapping):
            raise QfbvBitBlastError("malformed Query IR expression")
        children = tuple(str(child) for child in children_raw)
        self.visiting.add(node_hash)
        lowered = tuple(self._node(child) for child in children)
        self.visiting.remove(node_hash)
        self.maximum_width = max(self.maximum_width, bits)
        self.operator_counts[op] = self.operator_counts.get(op, 0) + 1

        def require_bv(index: int, width: int | None = None) -> tuple[int, ...]:
            sort, value = lowered[index]
            if sort != "BV" or (width is not None and len(value) != width):
                raise QfbvBitBlastError(f"{op} has incompatible BV operands")
            return value

        def require_bool(index: int) -> int:
            sort, value = lowered[index]
            if sort != "Bool" or len(value) != 1:
                raise QfbvBitBlastError(f"{op} requires Boolean operands")
            return value[0]

        sort = "BV"
        value: tuple[int, ...]
        if op == "bool":
            self._require_arity(op, children, 0)
            if bits != 1 or not isinstance(attrs.get("value"), bool):
                raise QfbvBitBlastError("bool node has an invalid value")
            sort = "Bool"
            value = (self.true_literal if attrs["value"] else self.false_literal,)
        elif op == "constant":
            self._require_arity(op, children, 0)
            try:
                constant = int(str(attrs.get("value_hex", "")), 16)
            except ValueError as error:
                raise QfbvBitBlastError("constant has invalid hexadecimal value") from error
            if constant < 0 or constant.bit_length() > bits:
                raise QfbvBitBlastError("constant does not fit its bit width")
            value = tuple(
                self.true_literal if (constant >> bit) & 1 else self.false_literal
                for bit in range(bits)
            )
        elif op == "read":
            self._require_arity(op, children, 0)
            if bits != 8:
                raise QfbvBitBlastError("input reads must be 8-bit")
            offset = _bounded_int(attrs.get("index"), "read index", 0, (1 << 32) - 1)
            if offset not in self.inputs:
                if len(self.inputs) >= self.max_input_bytes:
                    raise QfbvBitBlastError("query exceeds the input-byte limit")
                self.inputs[offset] = tuple(self._variable() for _ in range(8))
            value = self.inputs[offset]
        elif op == "concat":
            self._require_arity(op, children, 2)
            first, second = require_bv(0), require_bv(1)
            if bits != len(first) + len(second):
                raise QfbvBitBlastError("concat result width is inconsistent")
            value = second + first
        elif op == "extract":
            self._require_arity(op, children, 1)
            source = require_bv(0)
            index = _bounded_int(attrs.get("index"), "extract index", 0, self.max_bits)
            if index + bits > len(source):
                raise QfbvBitBlastError("extract range exceeds its operand")
            value = source[index:index + bits]
        elif op in {"zext", "sext"}:
            self._require_arity(op, children, 1)
            source = require_bv(0)
            if bits < len(source):
                raise QfbvBitBlastError("extension narrows its operand")
            fill = source[-1] if op == "sext" else self.false_literal
            value = source + (fill,) * (bits - len(source))
        elif op in {"add", "sub", "mul", "udiv", "sdiv", "urem", "srem"}:
            self._require_arity(op, children, 2)
            first, second = require_bv(0, bits), require_bv(1, bits)
            if op == "add":
                value = self._add(first, second)
            elif op == "sub":
                value = self._subtract(first, second)
            elif op == "mul":
                value = (self.false_literal,) * bits
                for index, selector in enumerate(second):
                    shifted = (self.false_literal,) * index + first[:bits - index]
                    value = self._add(value, tuple(
                        self._and(selector, bit) for bit in shifted
                    ))
            elif op in {"udiv", "urem"}:
                quotient, remainder = self._unsigned_divrem(first, second)
                value = quotient if op == "udiv" else remainder
            else:
                sign_first, sign_second = first[-1], second[-1]
                abs_first = self._select_bits(sign_first, self._negate(first), first)
                abs_second = self._select_bits(sign_second, self._negate(second), second)
                quotient, remainder = self._unsigned_divrem(abs_first, abs_second)
                if op == "sdiv":
                    sign = self._xor(sign_first, sign_second)
                    value = self._select_bits(sign, self._negate(quotient), quotient)
                else:
                    value = self._select_bits(
                        sign_first, self._negate(remainder), remainder
                    )
        elif op in {"neg", "not"}:
            self._require_arity(op, children, 1)
            source = require_bv(0, bits)
            value = self._negate(source) if op == "neg" else tuple(-bit for bit in source)
        elif op in {"and", "or", "xor"}:
            self._require_arity(op, children, 1, 2, 3)
            operands = tuple(require_bv(index, bits) for index in range(len(lowered)))
            value = operands[0]
            gate = {"and": self._and, "or": self._or, "xor": self._xor}[op]
            for operand in operands[1:]:
                value = tuple(gate(a, b) for a, b in zip(value, operand, strict=True))
        elif op in {"shl", "lshr", "ashr"}:
            self._require_arity(op, children, 2)
            value = self._shift(require_bv(0, bits), require_bv(1, bits), direction=op)
        elif op in {"equal", "distinct"}:
            self._require_arity(op, children, 2)
            if lowered[0][0] != lowered[1][0] or len(lowered[0][1]) != len(lowered[1][1]) or bits != 1:
                raise QfbvBitBlastError("equality operand sorts do not match")
            equality = self._equal_bits(lowered[0][1], lowered[1][1])
            sort = "Bool"
            value = (equality if op == "equal" else -equality,)
        elif op in {"ult", "ule", "ugt", "uge", "slt", "sle", "sgt", "sge"}:
            self._require_arity(op, children, 2)
            first = require_bv(0)
            second = require_bv(1, len(first))
            if bits != 1:
                raise QfbvBitBlastError("comparison result must be Boolean")
            if op[0] == "s":
                first = first[:-1] + (-first[-1],)
                second = second[:-1] + (-second[-1],)
            if op.endswith("lt"):
                comparison = self._unsigned_lt(first, second)
            elif op.endswith("le"):
                comparison = -self._unsigned_lt(second, first)
            elif op.endswith("gt"):
                comparison = self._unsigned_lt(second, first)
            else:
                comparison = -self._unsigned_lt(first, second)
            sort = "Bool"
            value = (comparison,)
        elif op in {"land", "lor"}:
            self._require_arity(op, children, 1, 2, 3)
            if bits != 1:
                raise QfbvBitBlastError("logical result must be Boolean")
            operands = tuple(require_bool(index) for index in range(len(lowered)))
            sort = "Bool"
            value = ((self._reduce_and(operands) if op == "land" else self._reduce_or(operands)),)
        elif op == "lnot":
            self._require_arity(op, children, 1)
            if bits != 1:
                raise QfbvBitBlastError("logical not result must be Boolean")
            sort = "Bool"
            value = (-require_bool(0),)
        elif op == "ite":
            self._require_arity(op, children, 3)
            condition = require_bool(0)
            if lowered[1][0] != lowered[2][0] or len(lowered[1][1]) != len(lowered[2][1]) or bits != len(lowered[1][1]):
                raise QfbvBitBlastError("ite branch sorts do not match")
            sort = lowered[1][0]
            value = self._select_bits(condition, lowered[1][1], lowered[2][1])
        elif op in {"rol", "ror"}:
            self._require_arity(op, children, 2)
            value = self._rotate(
                require_bv(0, bits), require_bv(1, bits), left=op == "rol"
            )
        else:
            raise QfbvBitBlastError(f"operator {op or '<empty>'} is not bit-blastable")

        if len(value) != bits:
            raise QfbvBitBlastError(f"{op} produced an inconsistent bit width")
        result = (sort, value)
        self.memo[node_hash] = result
        return result

    @staticmethod
    def _clauses_sha256(clauses: Sequence[Sequence[int]]) -> str:
        return _digest(_canonical_json(tuple(tuple(item) for item in clauses)))

    def finish(self) -> BitBlastPlan:
        if not self.roots:
            raise QfbvBitBlastError("query has no roots")
        increments: list[CnfIncrement] = []
        assumptions: list[int] = []
        parent = _digest(_canonical_json({
            "schema": INCREMENT_SCHEMA,
            "parent_formula_sha256": "",
            "clauses": self._clauses_sha256(self.clauses),
        }))
        for ordinal, root_hash in enumerate(self.roots):
            first_clause = len(self.clauses) + 1
            sort, root = self._node(root_hash)
            if sort != "Bool" or len(root) != 1:
                raise QfbvBitBlastError("query roots must be Boolean")
            activation = self._variable()
            self._clause((-activation, root[0]))
            last_clause = len(self.clauses)
            added = self.clauses[first_clause - 1:last_clause]
            clause_sha256 = self._clauses_sha256(added)
            body = {
                "schema": INCREMENT_SCHEMA,
                "ordinal": ordinal,
                "root_hash": root_hash,
                "activation_literal": activation,
                "first_clause_id": first_clause,
                "last_clause_id": last_clause,
                "max_variable": self.next_variable - 1,
                "parent_formula_sha256": parent,
                "clause_sha256": clause_sha256,
            }
            formula_sha256 = _digest(_canonical_json(body))
            increments.append(CnfIncrement(
                ordinal=ordinal,
                root_hash=root_hash,
                activation_literal=activation,
                first_clause_id=first_clause,
                last_clause_id=last_clause,
                max_variable=self.next_variable - 1,
                parent_formula_sha256=parent,
                formula_sha256=formula_sha256,
                clause_sha256=clause_sha256,
            ))
            assumptions.append(activation)
            parent = formula_sha256
        input_items = tuple(sorted(self.inputs.items()))
        assumption_identity = {
            "schema": ASSUMPTION_SCHEMA,
            "formula_sha256": parent,
            "literals": tuple(assumptions),
        }
        certificate: dict[str, Any] = {
            "schema": BITBLAST_SCHEMA,
            "query_id": self.query_id,
            "formula_sha256": parent,
            "assumption_sha256": _digest(_canonical_json(assumption_identity)),
            "cnf_sha256": self._clauses_sha256(self.clauses),
            "input_map_sha256": _digest(_canonical_json(input_items)),
            "root_count": len(self.roots),
            "node_count": len(self.memo),
            "variable_count": self.next_variable - 1,
            "clause_count": len(self.clauses),
            "input_offset_count": len(input_items),
            "maximum_width": self.maximum_width,
            "operator_counts": dict(sorted(self.operator_counts.items())),
            "activation_guarded": True,
            "bit_order": "least-significant-first",
            "cnf_protocol": "deterministic-tseitin-ripple-restoring-v1",
        }
        certificate["certificate_sha256"] = _digest(_canonical_json(certificate))
        return BitBlastPlan(
            query_id=self.query_id,
            clauses=tuple(self.clauses),
            max_variable=self.next_variable - 1,
            input_literals=input_items,
            assumptions=tuple(assumptions),
            increments=tuple(increments),
            certificate=certificate,
        )


def bitblast_qfbv_query(
    query_id: str,
    roots: Sequence[str],
    expressions: Mapping[str, Mapping[str, Any]],
    capabilities: Mapping[str, Any] | None = None,
    *,
    max_variables: int = MAX_CNF_VARIABLES,
    max_clauses: int = MAX_CNF_CLAUSES,
) -> BitBlastPlan:
    """Return a deterministic activation-guarded CNF plan for Query IR."""
    raw = capabilities or {}
    max_bits = _bounded_int(raw.get("max_bits", 4096), "max_bits", 1, 1 << 20)
    max_nodes = _bounded_int(raw.get("max_nodes", 250_000), "max_nodes", 1, 250_000)
    max_inputs = _bounded_int(
        raw.get("max_input_bytes", 4096), "max_input_bytes", 0, 65536
    )
    encoder = _Encoder(
        query_id,
        roots,
        expressions,
        max_bits=max_bits,
        max_nodes=max_nodes,
        max_input_bytes=max_inputs,
        max_variables=_bounded_int(
            max_variables, "max_variables", 1, MAX_CNF_VARIABLES
        ),
        max_clauses=_bounded_int(max_clauses, "max_clauses", 1, MAX_CNF_CLAUSES),
    )
    return encoder.finish()


def extend_bitblast_assumptions(
    plan: BitBlastPlan,
    additional_assumptions: Sequence[int],
) -> BitBlastPlan:
    """Return an immutable plan scoped to additional solve assumptions.

    The permanent CNF/formula identity is unchanged.  The assumption and
    certificate identities are recomputed so proof receipts cannot be replayed
    between the base query and a partition cube.
    """
    certificate = dict(plan.certificate)
    supplied_certificate = certificate.pop("certificate_sha256", None)
    if (
        not isinstance(supplied_certificate, str)
        or supplied_certificate != _digest(_canonical_json(certificate))
    ):
        raise QfbvBitBlastError("bit-blast certificate identity changed")
    expected_assumption = _digest(
        _canonical_json(
            {
                "schema": ASSUMPTION_SCHEMA,
                "formula_sha256": plan.formula_sha256,
                "literals": tuple(plan.assumptions),
            }
        )
    )
    if plan.assumption_sha256 != expected_assumption:
        raise QfbvBitBlastError("bit-blast assumption identity changed")

    extras: list[int] = []
    occupied = {abs(literal) for literal in plan.assumptions}
    for raw_literal in additional_assumptions:
        if type(raw_literal) is not int:
            raise QfbvBitBlastError("additional assumption must be an integer")
        literal = _bounded_int(
            raw_literal,
            "additional assumption",
            -plan.max_variable,
            plan.max_variable,
        )
        if literal == 0:
            raise QfbvBitBlastError("additional assumption must be nonzero")
        variable = abs(literal)
        if variable in occupied:
            raise QfbvBitBlastError("assumption variables must be unique")
        occupied.add(variable)
        extras.append(literal)
    if not extras:
        return plan

    assumptions = plan.assumptions + tuple(extras)
    certificate["assumption_sha256"] = _digest(
        _canonical_json(
            {
                "schema": ASSUMPTION_SCHEMA,
                "formula_sha256": plan.formula_sha256,
                "literals": assumptions,
            }
        )
    )
    certificate["certificate_sha256"] = _digest(_canonical_json(certificate))
    return replace(plan, assumptions=assumptions, certificate=certificate)


def parse_dimacs_assignment(output: str) -> tuple[str, dict[int, bool]]:
    """Parse a complete SAT assignment while rejecting ambiguous literals."""
    status = ""
    assignment: dict[int, bool] = {}
    terminated = False
    for raw in output.splitlines():
        line = raw.strip()
        if not line or line.startswith("c"):
            continue
        if line in {"SAT", "s SATISFIABLE"}:
            if status and status != "sat":
                raise QfbvBitBlastError("conflicting DIMACS solver statuses")
            status = "sat"
            continue
        if line in {"UNSAT", "s UNSATISFIABLE"}:
            if status and status != "unsat":
                raise QfbvBitBlastError("conflicting DIMACS solver statuses")
            status = "unsat"
            continue
        if line in {"UNKNOWN", "s UNKNOWN"}:
            if status and status != "unknown":
                raise QfbvBitBlastError("conflicting DIMACS solver statuses")
            status = "unknown"
            continue
        if line.startswith("v ") or (status == "sat" and line[0] in "-0123456789"):
            tokens = line[1:].split() if line.startswith("v ") else line.split()
            for token in tokens:
                try:
                    literal = int(token)
                except ValueError as error:
                    raise QfbvBitBlastError("invalid DIMACS model literal") from error
                if literal == 0:
                    terminated = True
                    continue
                if terminated:
                    raise QfbvBitBlastError(
                        "DIMACS model contains literals after its terminator"
                    )
                variable = abs(literal)
                value = literal > 0
                previous = assignment.setdefault(variable, value)
                if previous != value:
                    raise QfbvBitBlastError(
                        "DIMACS model assigns a variable inconsistently"
                    )
    if status == "sat" and not terminated:
        raise QfbvBitBlastError("SAT result has no terminated DIMACS model")
    if not status:
        raise QfbvBitBlastError("DIMACS solver returned no status")
    return status, assignment


def parse_dimacs_model(output: str) -> tuple[str, set[int]]:
    """Parse SAT competition output without trusting unrelated diagnostics."""
    status, assignment = parse_dimacs_assignment(output)
    return status, {variable for variable, value in assignment.items() if value}
