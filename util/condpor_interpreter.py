#!/usr/bin/env python3
"""Bounded interpreter-level ConDPOR for a closed concurrent QF_BV IR.

The native scheduler explores traces emitted by already executed programs.
This module fills a different role: it owns a small executable concurrent IR,
reconstructs program state from an execution graph, and therefore regenerates
control-dependent events after a backward revisit.  The supported model is
deliberately explicit and finite: scalar bit-vector memory, SC, no dynamic
thread creation, and caller-supplied exploration bounds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import copy
import hashlib
import json
import re
from typing import Any, Iterable, Mapping

from schedule_exploration import _SystemZ3Solver


CONDPOR_PROGRAM_SCHEMA = "symcc-condpor-program-v1"
CONDPOR_INTERPRETER_SCHEMA = "symcc-interpreter-condpor-v1"
CONDPOR_INTERPRETER_SEMANTICS = "bounded-interpreter-sc-qfbv-condpor-v1"

_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_VISIBLE_OPS = frozenset(
    {
        "symbol",
        "read",
        "write",
        "branch",
        "assume",
        "assert",
    }
)
_HIDDEN_OPS = frozenset({"label", "nop", "set", "jump", "halt"})
_ALL_OPS = _VISIBLE_OPS | _HIDDEN_OPS
_EXPR_OPS = frozenset(
    {
        "const",
        "var",
        "not",
        "and",
        "or",
        "xor",
        "eq",
        "ne",
        "add",
        "sub",
        "mul",
        "udiv",
        "urem",
        "shl",
        "lshr",
        "ashr",
        "bvnot",
        "bvand",
        "bvor",
        "bvxor",
        "ult",
        "ule",
        "ugt",
        "uge",
        "slt",
        "sle",
        "sgt",
        "sge",
        "ite",
        "concat",
        "extract",
        "zext",
        "sext",
    }
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _certificate_digest(certificate: Mapping[str, Any]) -> str:
    return _digest(
        {
            key: value
            for key, value in certificate.items()
            if key != "certificate_sha256"
        }
    )


def _require_name(value: Any, context: str) -> str:
    if not isinstance(value, str) or not _NAME_RE.fullmatch(value):
        raise ValueError(f"{context} must be an ASCII identifier")
    return value


def _require_exact_keys(
    value: Mapping[str, Any],
    *,
    required: Iterable[str],
    optional: Iterable[str] = (),
    context: str,
) -> None:
    required_set = set(required)
    allowed = required_set | set(optional)
    missing = sorted(required_set - set(value))
    unknown = sorted(set(value) - allowed)
    if missing:
        raise ValueError(f"{context} is missing keys: {', '.join(missing)}")
    if unknown:
        raise ValueError(f"{context} has unknown keys: {', '.join(unknown)}")


def _validate_expression(
    expression: Any,
    *,
    depth: int = 0,
    counter: list[int] | None = None,
) -> None:
    if counter is None:
        counter = [0]
    counter[0] += 1
    if depth > 64 or counter[0] > 2048:
        raise ValueError("expression exceeds the structural bound")
    if isinstance(expression, bool) or isinstance(expression, int):
        return
    if isinstance(expression, str):
        _require_name(expression, "expression variable")
        return
    if not isinstance(expression, Mapping):
        raise ValueError("expression must be a scalar, identifier, or object")
    op = expression.get("op")
    if op not in _EXPR_OPS:
        raise ValueError(f"unsupported expression operator: {op!r}")
    if op == "const":
        _require_exact_keys(
            expression,
            required=("op", "value"),
            optional=("bits",),
            context="const expression",
        )
        if not isinstance(expression["value"], int):
            raise ValueError("const value must be an integer")
        bits = expression.get("bits")
        if bits is not None and (
            not isinstance(bits, int) or isinstance(bits, bool) or bits < 1 or bits > 64
        ):
            raise ValueError("const bits must be in [1, 64]")
        return
    if op == "var":
        _require_exact_keys(
            expression,
            required=("op", "name"),
            context="var expression",
        )
        _require_name(expression["name"], "var name")
        return
    if op == "extract":
        _require_exact_keys(
            expression,
            required=("op", "value", "high", "low"),
            context="extract expression",
        )
        high, low = expression["high"], expression["low"]
        if (
            not isinstance(high, int)
            or isinstance(high, bool)
            or not isinstance(low, int)
            or isinstance(low, bool)
            or low < 0
            or high < low
            or high >= 64
        ):
            raise ValueError("extract indices are invalid")
        _validate_expression(expression["value"], depth=depth + 1, counter=counter)
        return
    if op in {"zext", "sext"}:
        _require_exact_keys(
            expression,
            required=("op", "value", "bits"),
            context=f"{op} expression",
        )
        bits = expression["bits"]
        if not isinstance(bits, int) or isinstance(bits, bool) or bits < 1 or bits > 64:
            raise ValueError(f"{op} bits must be in [1, 64]")
        _validate_expression(expression["value"], depth=depth + 1, counter=counter)
        return
    if op == "ite":
        _require_exact_keys(
            expression,
            required=("op", "condition", "then", "else"),
            context="ite expression",
        )
        children = (expression["condition"], expression["then"], expression["else"])
    else:
        _require_exact_keys(
            expression,
            required=("op", "args"),
            context=f"{op} expression",
        )
        children = expression["args"]
        if not isinstance(children, list):
            raise ValueError(f"{op} args must be a list")
        arity = len(children)
        if op in {"not", "bvnot"} and arity != 1:
            raise ValueError(f"{op} requires one argument")
        if op not in {"not", "bvnot", "and", "or", "xor", "concat"}:
            if arity != 2:
                raise ValueError(f"{op} requires two arguments")
        if op in {"and", "or", "xor", "concat"} and arity < 2:
            raise ValueError(f"{op} requires at least two arguments")
    for child in children:
        _validate_expression(child, depth=depth + 1, counter=counter)


def _resolve_target(
    target: Any,
    *,
    labels: Mapping[str, int],
    length: int,
    context: str,
) -> int:
    if isinstance(target, str):
        if target not in labels:
            raise ValueError(f"{context} references unknown label {target!r}")
        return int(labels[target])
    if (
        not isinstance(target, int)
        or isinstance(target, bool)
        or target < 0
        or target >= length
    ):
        raise ValueError(f"{context} target must name an instruction")
    return target


def validate_condpor_program(program: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize a closed concurrent program."""
    if not isinstance(program, Mapping):
        raise ValueError("program must be a JSON object")
    _require_exact_keys(
        program,
        required=("schema", "bit_width", "memory", "threads"),
        optional=("name",),
        context="program",
    )
    if program["schema"] != CONDPOR_PROGRAM_SCHEMA:
        raise ValueError("unsupported ConDPOR program schema")
    width = program["bit_width"]
    if not isinstance(width, int) or isinstance(width, bool) or width < 1 or width > 64:
        raise ValueError("bit_width must be in [1, 64]")
    raw_memory = program["memory"]
    if not isinstance(raw_memory, Mapping):
        raise ValueError("memory must be an object")
    memory: dict[str, int] = {}
    mask = (1 << width) - 1
    for raw_name, raw_value in raw_memory.items():
        name = _require_name(raw_name, "memory object")
        if not isinstance(raw_value, int) or isinstance(raw_value, bool):
            raise ValueError(f"initial value for {name} must be an integer")
        memory[name] = raw_value & mask
    raw_threads = program["threads"]
    if not isinstance(raw_threads, Mapping) or not raw_threads:
        raise ValueError("threads must be a non-empty object")
    if len(raw_threads) > 256:
        raise ValueError("program has more than 256 threads")
    threads: dict[str, list[dict[str, Any]]] = {}
    for raw_tid, raw_instructions in raw_threads.items():
        if (
            not isinstance(raw_tid, str)
            or not raw_tid.isascii()
            or not raw_tid.isdigit()
            or str(int(raw_tid)) != raw_tid
        ):
            raise ValueError("thread ids must be canonical non-negative decimals")
        if int(raw_tid) > 1_000_000:
            raise ValueError("thread id exceeds the supported bound")
        if not isinstance(raw_instructions, list) or not raw_instructions:
            raise ValueError(f"thread {raw_tid} must contain instructions")
        if len(raw_instructions) > 100_000:
            raise ValueError(f"thread {raw_tid} exceeds the instruction bound")
        labels: dict[str, int] = {}
        for pc, raw_instruction in enumerate(raw_instructions):
            if not isinstance(raw_instruction, Mapping):
                raise ValueError(f"thread {raw_tid} instruction {pc} is not an object")
            if raw_instruction.get("op") == "label":
                _require_exact_keys(
                    raw_instruction,
                    required=("op", "name"),
                    context=f"thread {raw_tid} instruction {pc}",
                )
                label = _require_name(raw_instruction["name"], "label")
                if label in labels:
                    raise ValueError(f"thread {raw_tid} duplicates label {label}")
                labels[label] = pc
        normalized: list[dict[str, Any]] = []
        for pc, raw_instruction in enumerate(raw_instructions):
            op = raw_instruction.get("op")
            context = f"thread {raw_tid} instruction {pc}"
            if op not in _ALL_OPS:
                raise ValueError(f"{context} has unsupported op {op!r}")
            instruction = dict(raw_instruction)
            if op == "label":
                pass
            elif op in {"nop", "halt"}:
                _require_exact_keys(raw_instruction, required=("op",), context=context)
            elif op == "symbol":
                _require_exact_keys(
                    raw_instruction,
                    required=("op", "dst"),
                    optional=("bits",),
                    context=context,
                )
                _require_name(raw_instruction["dst"], "symbol destination")
                bits = raw_instruction.get("bits", width)
                if (
                    not isinstance(bits, int)
                    or isinstance(bits, bool)
                    or bits < 1
                    or bits > 64
                ):
                    raise ValueError(f"{context} bits must be in [1, 64]")
                instruction["bits"] = bits
            elif op == "read":
                _require_exact_keys(
                    raw_instruction,
                    required=("op", "object", "dst"),
                    context=context,
                )
                obj = _require_name(raw_instruction["object"], "read object")
                if obj not in memory:
                    raise ValueError(f"{context} reads undeclared object {obj}")
                _require_name(raw_instruction["dst"], "read destination")
            elif op == "write":
                _require_exact_keys(
                    raw_instruction,
                    required=("op", "object", "value"),
                    context=context,
                )
                obj = _require_name(raw_instruction["object"], "write object")
                if obj not in memory:
                    raise ValueError(f"{context} writes undeclared object {obj}")
                _validate_expression(raw_instruction["value"])
            elif op == "set":
                _require_exact_keys(
                    raw_instruction,
                    required=("op", "dst", "value"),
                    context=context,
                )
                _require_name(raw_instruction["dst"], "set destination")
                _validate_expression(raw_instruction["value"])
            elif op == "jump":
                _require_exact_keys(
                    raw_instruction, required=("op", "target"), context=context
                )
                instruction["target"] = _resolve_target(
                    raw_instruction["target"],
                    labels=labels,
                    length=len(raw_instructions),
                    context=context,
                )
            elif op == "branch":
                _require_exact_keys(
                    raw_instruction,
                    required=("op", "condition", "then", "else"),
                    context=context,
                )
                _validate_expression(raw_instruction["condition"])
                instruction["then"] = _resolve_target(
                    raw_instruction["then"],
                    labels=labels,
                    length=len(raw_instructions),
                    context=context,
                )
                instruction["else"] = _resolve_target(
                    raw_instruction["else"],
                    labels=labels,
                    length=len(raw_instructions),
                    context=context,
                )
            else:
                _require_exact_keys(
                    raw_instruction,
                    required=("op", "condition"),
                    context=context,
                )
                _validate_expression(raw_instruction["condition"])
            normalized.append(instruction)
        threads[raw_tid] = normalized
    result: dict[str, Any] = {
        "schema": CONDPOR_PROGRAM_SCHEMA,
        "bit_width": width,
        "memory": {name: memory[name] for name in sorted(memory)},
        "threads": {tid: threads[tid] for tid in sorted(threads, key=int)},
    }
    if "name" in program:
        if not isinstance(program["name"], str) or len(program["name"]) > 256:
            raise ValueError("program name must be a string of at most 256 characters")
        result["name"] = program["name"]
    return result


@dataclass(frozen=True)
class _Term:
    smt: str
    sort: str
    bits: int | None = None


def _bv_literal(value: int, bits: int) -> str:
    return f"(_ bv{value & ((1 << bits) - 1)} {bits})"


def _coerce_same_bv(left: _Term, right: _Term, op: str) -> int:
    if left.sort != "bv" or right.sort != "bv" or left.bits != right.bits:
        raise ValueError(f"{op} requires equal-width bit-vector operands")
    assert left.bits is not None
    return left.bits


def _compile_expression(
    expression: Any,
    environment: Mapping[str, _Term],
    default_bits: int,
) -> _Term:
    if isinstance(expression, bool):
        return _Term("true" if expression else "false", "bool")
    if isinstance(expression, int):
        return _Term(_bv_literal(expression, default_bits), "bv", default_bits)
    if isinstance(expression, str):
        if expression not in environment:
            raise ValueError(f"use of undefined local {expression}")
        return environment[expression]
    op = str(expression["op"])
    if op == "const":
        bits = int(expression.get("bits", default_bits))
        return _Term(_bv_literal(int(expression["value"]), bits), "bv", bits)
    if op == "var":
        name = str(expression["name"])
        if name not in environment:
            raise ValueError(f"use of undefined local {name}")
        return environment[name]
    if op == "ite":
        condition = _compile_expression(
            expression["condition"], environment, default_bits
        )
        yes = _compile_expression(expression["then"], environment, default_bits)
        no = _compile_expression(expression["else"], environment, default_bits)
        if condition.sort != "bool" or (yes.sort, yes.bits) != (no.sort, no.bits):
            raise ValueError("ite requires a Boolean condition and equal branch sorts")
        return _Term(f"(ite {condition.smt} {yes.smt} {no.smt})", yes.sort, yes.bits)
    if op == "extract":
        value = _compile_expression(expression["value"], environment, default_bits)
        high, low = int(expression["high"]), int(expression["low"])
        if value.sort != "bv" or value.bits is None or high >= value.bits:
            raise ValueError("extract source width is too small")
        return _Term(f"((_ extract {high} {low}) {value.smt})", "bv", high - low + 1)
    if op in {"zext", "sext"}:
        value = _compile_expression(expression["value"], environment, default_bits)
        target = int(expression["bits"])
        if value.sort != "bv" or value.bits is None or target < value.bits:
            raise ValueError(f"{op} target width is smaller than the source")
        extension = target - value.bits
        primitive = "zero_extend" if op == "zext" else "sign_extend"
        return _Term(f"((_ {primitive} {extension}) {value.smt})", "bv", target)
    args = [
        _compile_expression(child, environment, default_bits)
        for child in expression["args"]
    ]
    if op == "not":
        if args[0].sort != "bool":
            raise ValueError("not requires a Boolean operand")
        return _Term(f"(not {args[0].smt})", "bool")
    if op in {"and", "or", "xor"}:
        if any(arg.sort != "bool" for arg in args):
            raise ValueError(f"{op} requires Boolean operands")
        if op == "xor":
            folded = args[0].smt
            for argument in args[1:]:
                folded = f"(xor {folded} {argument.smt})"
            return _Term(folded, "bool")
        return _Term(f"({op} {' '.join(arg.smt for arg in args)})", "bool")
    if op == "bvnot":
        if args[0].sort != "bv":
            raise ValueError("bvnot requires a bit-vector operand")
        return _Term(f"(bvnot {args[0].smt})", "bv", args[0].bits)
    if op == "concat":
        if any(arg.sort != "bv" for arg in args):
            raise ValueError("concat requires bit-vector operands")
        bits = sum(int(arg.bits) for arg in args)
        if bits > 64:
            raise ValueError("concat result exceeds 64 bits")
        folded = args[0].smt
        for argument in args[1:]:
            folded = f"(concat {folded} {argument.smt})"
        return _Term(folded, "bv", bits)
    left, right = args
    if op in {"eq", "ne"}:
        if (left.sort, left.bits) != (right.sort, right.bits):
            raise ValueError(f"{op} requires operands of the same sort")
        equality = f"(= {left.smt} {right.smt})"
        return _Term(equality if op == "eq" else f"(not {equality})", "bool")
    bits = _coerce_same_bv(left, right, op)
    arithmetic = {
        "add": "bvadd",
        "sub": "bvsub",
        "mul": "bvmul",
        "udiv": "bvudiv",
        "urem": "bvurem",
        "shl": "bvshl",
        "lshr": "bvlshr",
        "ashr": "bvashr",
        "bvand": "bvand",
        "bvor": "bvor",
        "bvxor": "bvxor",
    }
    comparison = {
        "ult": "bvult",
        "ule": "bvule",
        "ugt": "bvugt",
        "uge": "bvuge",
        "slt": "bvslt",
        "sle": "bvsle",
        "sgt": "bvsgt",
        "sge": "bvsge",
    }
    if op in arithmetic:
        return _Term(f"({arithmetic[op]} {left.smt} {right.smt})", "bv", bits)
    return _Term(f"({comparison[op]} {left.smt} {right.smt})", "bool")


@dataclass
class _ThreadState:
    tid: str
    instructions: list[dict[str, Any]]
    pc: int = 0
    event_index: int = 0
    locals: dict[str, _Term] = field(default_factory=dict)
    status: str = "running"
    internal_steps: int = 0


@dataclass
class _Replay:
    valid: bool
    reason: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    states: dict[str, _ThreadState] = field(default_factory=dict)
    next_tid: str | None = None
    next_instruction: dict[str, Any] | None = None
    path_constraints: list[str] = field(default_factory=list)
    write_definitions: dict[str, _Term] = field(default_factory=dict)
    symbols: dict[str, int] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    terminal_kind: str | None = None
    internal_bound_hit: bool = False


def _event_id(tid: str, event_index: int) -> str:
    return f"t{tid}:e{event_index}"


def _smt_event_name(prefix: str, event_id: str) -> str:
    return f"{prefix}_{event_id.replace(':', '_')}"


def _event_spec(state: _ThreadState, instruction: Mapping[str, Any]) -> dict[str, Any]:
    op = str(instruction["op"])
    kind = (
        "A"
        if op == "symbol"
        else ("R" if op == "read" else "W" if op == "write" else "C")
    )
    obj = str(instruction.get("object", instruction.get("dst", op)))
    return {
        "id": _event_id(state.tid, state.event_index),
        "tid": int(state.tid),
        "thread_index": state.event_index,
        "pc": state.pc,
        "kind": kind,
        "op": op,
        "object": obj,
    }


def _advance_hidden(
    state: _ThreadState,
    *,
    default_bits: int,
    max_internal_steps: int,
) -> dict[str, Any] | None:
    while state.status == "running":
        if state.pc < 0 or state.pc >= len(state.instructions):
            state.status = "halted"
            return None
        instruction = state.instructions[state.pc]
        op = str(instruction["op"])
        if op in _VISIBLE_OPS:
            return instruction
        state.internal_steps += 1
        if state.internal_steps > max_internal_steps:
            state.status = "internal_bound"
            return None
        if op in {"label", "nop"}:
            state.pc += 1
        elif op == "set":
            state.locals[str(instruction["dst"])] = _compile_expression(
                instruction["value"], state.locals, default_bits
            )
            state.pc += 1
        elif op == "jump":
            state.pc = int(instruction["target"])
        else:
            state.status = "halted"
    return None


def _graph_relations(
    program: Mapping[str, Any], graph: Mapping[str, Any]
) -> dict[str, Any]:
    events = list(graph["events"])
    by_id = {str(event["id"]): event for event in events}
    nodes = set(by_id)
    init_nodes = {f"init:{obj}" for obj in program["memory"]}
    nodes.update(init_nodes)
    by_thread: dict[int, list[dict[str, Any]]] = {}
    for event in events:
        by_thread.setdefault(int(event["tid"]), []).append(event)
    po: list[tuple[str, str]] = []
    for thread_events in by_thread.values():
        ordered = sorted(thread_events, key=lambda row: int(row["thread_index"]))
        po.extend(
            (str(left["id"]), str(right["id"]))
            for left, right in zip(ordered, ordered[1:])
        )
    rf = [(str(source), str(read)) for read, source in sorted(graph["rf"].items())]
    co: list[tuple[str, str]] = []
    fr: list[tuple[str, str]] = []
    for obj, raw_order in sorted(graph["co"].items()):
        order = [str(node) for node in raw_order]
        co.extend(zip(order, order[1:]))
        positions = {node: index for index, node in enumerate(order)}
        for read, source in graph["rf"].items():
            event = by_id.get(str(read))
            if event is None or str(event["object"]) != obj or source not in positions:
                continue
            fr.extend(
                (str(read), later) for later in order[positions[str(source)] + 1 :]
            )
    return {
        "nodes": sorted(nodes),
        "po": sorted(set(po)),
        "rf": sorted(set(rf)),
        "co": sorted(set(co)),
        "fr": sorted(set(fr)),
    }


def _acyclic(nodes: Iterable[str], edges: Iterable[tuple[str, str]]) -> bool:
    successors = {node: set() for node in nodes}
    indegree = {node: 0 for node in nodes}
    for source, target in edges:
        if source not in successors or target not in successors or source == target:
            return False
        if target not in successors[source]:
            successors[source].add(target)
            indegree[target] += 1
    ready = sorted(node for node, degree in indegree.items() if degree == 0)
    visited = 0
    while ready:
        node = ready.pop(0)
        visited += 1
        for target in sorted(successors[node]):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
                ready.sort()
    return visited == len(successors)


def _causal_successors(relations: Mapping[str, Any]) -> dict[str, set[str]]:
    successors = {node: set() for node in relations["nodes"]}
    for source, target in list(relations["po"]) + list(relations["rf"]):
        successors[source].add(target)
    closure: dict[str, set[str]] = {}
    for node in successors:
        reached: set[str] = set()
        pending = list(successors[node])
        while pending:
            target = pending.pop()
            if target in reached:
                continue
            reached.add(target)
            pending.extend(successors[target] - reached)
        closure[node] = reached
    return closure


def _validate_graph_structure(
    program: Mapping[str, Any], graph: Mapping[str, Any]
) -> tuple[bool, str, dict[str, Any]]:
    events = list(graph["events"])
    ids = [str(event["id"]) for event in events]
    if len(ids) != len(set(ids)):
        return False, "duplicate_event_id", {}
    by_id = {str(event["id"]): event for event in events}
    reads = {event_id for event_id, event in by_id.items() if event["kind"] == "R"}
    writes = {event_id for event_id, event in by_id.items() if event["kind"] == "W"}
    constraints = {
        event_id for event_id, event in by_id.items() if event["kind"] == "C"
    }
    if set(graph["rf"]) != reads:
        return False, "read_from_not_total", {}
    if set(graph["outcomes"]) != constraints:
        return False, "constraint_outcomes_not_total", {}
    covered_writes: set[str] = set()
    for obj in program["memory"]:
        order = list(graph["co"].get(obj, ()))
        if not order or order[0] != f"init:{obj}" or len(order) != len(set(order)):
            return False, "invalid_coherence_order", {}
        expected = {
            event_id for event_id in writes if str(by_id[event_id]["object"]) == obj
        }
        if set(order[1:]) != expected:
            return False, "coherence_order_not_total", {}
        covered_writes.update(expected)
    if covered_writes != writes or set(graph["co"]) != set(program["memory"]):
        return False, "coherence_object_mismatch", {}
    for read, source in graph["rf"].items():
        read_event = by_id[read]
        obj = str(read_event["object"])
        if source not in graph["co"][obj]:
            return False, "read_from_location_mismatch", {}
    relations = _graph_relations(program, graph)
    edges = (
        list(relations["po"])
        + list(relations["rf"])
        + list(relations["co"])
        + list(relations["fr"])
    )
    if not _acyclic(relations["nodes"], edges):
        return False, "sc_cycle", relations
    return True, "", relations


def _replay_graph(
    program: Mapping[str, Any],
    graph: Mapping[str, Any],
    *,
    max_internal_steps: int,
) -> _Replay:
    width = int(program["bit_width"])
    states = {
        tid: _ThreadState(tid=tid, instructions=list(instructions))
        for tid, instructions in program["threads"].items()
    }
    result = _Replay(valid=True, states=states)
    for position, expected in enumerate(graph["events"]):
        tid = str(expected["tid"])
        state = states.get(tid)
        if state is None:
            return _Replay(valid=False, reason="event_has_unknown_thread")
        try:
            instruction = _advance_hidden(
                state, default_bits=width, max_internal_steps=max_internal_steps
            )
        except (KeyError, TypeError, ValueError) as exc:
            return _Replay(valid=False, reason=f"hidden_execution_error:{exc}")
        if instruction is None:
            reason = (
                "internal_step_bound"
                if state.status == "internal_bound"
                else "event_after_thread_termination"
            )
            return _Replay(
                valid=False,
                reason=reason,
                internal_bound_hit=(state.status == "internal_bound"),
            )
        actual = _event_spec(state, instruction)
        if actual != expected:
            return _Replay(valid=False, reason="event_not_regenerated_by_program")
        op = str(instruction["op"])
        detail = dict(actual)
        try:
            if op == "symbol":
                bits = int(instruction["bits"])
                name = _smt_event_name("sym", str(actual["id"]))
                state.locals[str(instruction["dst"])] = _Term(name, "bv", bits)
                result.symbols[name] = bits
                detail.update({"symbol": name, "bits": bits})
                state.pc += 1
            elif op == "read":
                source = str(graph["rf"][actual["id"]])
                obj = str(instruction["object"])
                if source.startswith("init:"):
                    value = _Term(
                        _bv_literal(int(program["memory"][obj]), width), "bv", width
                    )
                else:
                    value = _Term(_smt_event_name("write", source), "bv", width)
                state.locals[str(instruction["dst"])] = value
                detail.update({"read_from": source, "value_smt2": value.smt})
                state.pc += 1
            elif op == "write":
                value = _compile_expression(instruction["value"], state.locals, width)
                if value.sort != "bv" or value.bits != width:
                    raise ValueError("write value width differs from memory width")
                result.write_definitions[str(actual["id"])] = value
                detail["value_smt2"] = value.smt
                state.pc += 1
            else:
                condition = _compile_expression(
                    instruction["condition"], state.locals, width
                )
                if condition.sort != "bool":
                    raise ValueError(f"{op} condition is not Boolean")
                outcome = graph["outcomes"][actual["id"]]
                if not isinstance(outcome, bool):
                    raise ValueError("constraint outcome is not Boolean")
                assertion = condition.smt if outcome else f"(not {condition.smt})"
                result.path_constraints.append(assertion)
                detail.update({"condition_smt2": condition.smt, "outcome": outcome})
                if op == "branch":
                    state.pc = int(
                        instruction["then"] if outcome else instruction["else"]
                    )
                elif op == "assume":
                    state.pc += 1
                    if not outcome:
                        state.status = "blocked"
                else:
                    state.pc += 1
                    if not outcome:
                        state.status = "error"
                        result.error = {
                            "kind": "assertion_failure",
                            "event": str(actual["id"]),
                            "tid": int(tid),
                            "pc": int(actual["pc"]),
                            "condition_smt2": condition.smt,
                        }
            state.event_index += 1
            result.events.append(detail)
        except (KeyError, TypeError, ValueError) as exc:
            return _Replay(valid=False, reason=f"visible_execution_error:{exc}")
        if result.error is not None:
            if position + 1 != len(graph["events"]):
                return _Replay(valid=False, reason="events_after_assertion_failure")
            break
    if result.error is not None:
        result.terminal_kind = "error"
        return result
    prepared: list[tuple[int, str, dict[str, Any]]] = []
    for tid, state in states.items():
        try:
            instruction = _advance_hidden(
                state, default_bits=width, max_internal_steps=max_internal_steps
            )
        except (KeyError, TypeError, ValueError) as exc:
            return _Replay(valid=False, reason=f"hidden_execution_error:{exc}")
        if state.status == "internal_bound":
            result.internal_bound_hit = True
        if instruction is not None:
            prepared.append((int(tid), tid, instruction))
    if result.internal_bound_hit:
        result.terminal_kind = "internal_bound"
    elif prepared:
        _, result.next_tid, result.next_instruction = min(prepared)
    elif any(state.status == "blocked" for state in states.values()):
        result.terminal_kind = "blocked"
    else:
        result.terminal_kind = "complete"
    return result


def _solver_text(replay: _Replay) -> str:
    lines = ["(set-logic QF_BV)"]
    for name, bits in sorted(replay.symbols.items()):
        lines.append(f"(declare-fun {name} () (_ BitVec {bits}))")
    for event_id in sorted(replay.write_definitions):
        term = replay.write_definitions[event_id]
        lines.append(
            f"(declare-fun {_smt_event_name('write', event_id)} () (_ BitVec {term.bits}))"
        )
    for event_id, term in sorted(replay.write_definitions.items()):
        lines.append(f"(assert (= {_smt_event_name('write', event_id)} {term.smt}))")
    lines.extend(f"(assert {constraint})" for constraint in replay.path_constraints)
    return "\n".join(lines) + "\n"


class _SolverLimit(RuntimeError):
    pass


@dataclass
class _SolverBudget:
    maximum: int
    checks: int = 0

    def check(self, smt2: str) -> str:
        if self.checks >= self.maximum:
            raise _SolverLimit("solver_check_bound")
        self.checks += 1
        with _SystemZ3Solver(smt2) as solver:
            status = solver.check(())
            if status == "unknown":
                reason = solver.reason_unknown()
                raise RuntimeError(f"Z3 returned unknown: {reason}")
            return status


def _lexicographic_model(
    smt2: str,
    symbols: Mapping[str, int],
    budget: _SolverBudget,
) -> dict[str, int]:
    fixed: list[str] = []
    model: dict[str, int] = {}
    for name, bits in sorted(symbols.items()):
        low, high = 0, (1 << bits) - 1
        while low < high:
            middle = (low + high) // 2
            query = smt2 + "".join(f"(assert {row})\n" for row in fixed)
            query += f"(assert (bvule {name} {_bv_literal(middle, bits)}))\n"
            if budget.check(query) == "sat":
                high = middle
            else:
                low = middle + 1
        equality = f"(= {name} {_bv_literal(low, bits)})"
        fixed.append(equality)
        model[name] = low
    if fixed:
        final = smt2 + "".join(f"(assert {row})\n" for row in fixed)
        if budget.check(final) != "sat":
            raise RuntimeError("canonical model construction became unsatisfiable")
    return model


def _initial_graph(program: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "events": [],
        "rf": {},
        "co": {obj: [f"init:{obj}"] for obj in sorted(program["memory"])},
        "outcomes": {},
    }


def _graph_key(graph: Mapping[str, Any]) -> str:
    return _digest(graph)


def _append_event(graph: Mapping[str, Any], event: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(graph)
    result["events"].append(dict(event))
    return result


def _relation_rows(relations: Mapping[str, Any]) -> dict[str, list[dict[str, str]]]:
    return {
        name: [
            {"source": source, "target": target} for source, target in relations[name]
        ]
        for name in ("po", "rf", "co", "fr")
    }


def _execution_row(
    graph: Mapping[str, Any],
    replay: _Replay,
    relations: Mapping[str, Any],
    *,
    model: Mapping[str, int],
    terminal_kind: str,
) -> dict[str, Any]:
    row = {
        "graph_sha256": _graph_key(graph),
        "terminal_kind": terminal_kind,
        "event_count": len(graph["events"]),
        "events": replay.events,
        "relations": _relation_rows(relations),
        "path_constraints_smt2": list(replay.path_constraints),
        "canonical_unsigned_model": dict(sorted(model.items())),
        "model_rule": "lexicographically_minimal_unsigned_symbol_values",
        "graph": copy.deepcopy(graph),
    }
    if replay.error is not None:
        row["error"] = replay.error
    return row


def _restrict_for_backward_revisit(
    program: Mapping[str, Any],
    graph: Mapping[str, Any],
    *,
    read_id: str,
    write_id: str,
) -> tuple[dict[str, Any] | None, list[str], str]:
    relations = _graph_relations(program, graph)
    causal = _causal_successors(relations)
    if write_id in causal.get(read_id, set()):
        return None, [], "read_causally_precedes_write"
    ids = [str(event["id"]) for event in graph["events"]]
    read_position = ids.index(read_id)
    deleted = {
        event_id
        for event_id in ids[read_position + 1 :]
        if event_id != write_id and write_id not in causal.get(event_id, set())
    }
    retained = set(ids) - deleted
    result = copy.deepcopy(graph)
    result["events"] = [event for event in result["events"] if event["id"] in retained]
    result["rf"] = {
        read: source
        for read, source in result["rf"].items()
        if read in retained and (source.startswith("init:") or source in retained)
    }
    result["rf"][read_id] = write_id
    result["outcomes"] = {
        event: outcome
        for event, outcome in result["outcomes"].items()
        if event in retained
    }
    result["co"] = {
        obj: [
            event for event in order if event.startswith("init:") or event in retained
        ]
        for obj, order in result["co"].items()
    }
    valid, reason, _ = _validate_graph_structure(program, result)
    if not valid:
        return None, sorted(deleted), reason
    return result, sorted(deleted), "accepted"


def explore_condpor_program(
    program: Mapping[str, Any],
    *,
    max_graphs: int = 10_000,
    max_events: int = 64,
    max_revisits: int = 10_000,
    max_solver_checks: int = 100_000,
    max_internal_steps: int = 10_000,
) -> dict[str, Any]:
    """Explore all supported executions until completion or an explicit bound."""
    normalized = validate_condpor_program(program)
    bounds = {
        "max_graphs": int(max_graphs),
        "max_events": int(max_events),
        "max_revisits": int(max_revisits),
        "max_solver_checks": int(max_solver_checks),
        "max_internal_steps": int(max_internal_steps),
    }
    if any(value < 1 for value in bounds.values()):
        raise ValueError("all ConDPOR exploration bounds must be positive")
    pending = [_initial_graph(normalized)]
    seen: set[str] = set()
    executions: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    revisits: list[dict[str, Any]] = []
    pruned: dict[str, int] = {}
    bound_reasons: set[str] = set()
    solver = _SolverBudget(bounds["max_solver_checks"])
    generated = 1
    duplicate_graphs = 0
    while pending:
        graph = pending.pop()
        key = _graph_key(graph)
        if key in seen:
            duplicate_graphs += 1
            continue
        if len(seen) >= bounds["max_graphs"]:
            bound_reasons.add("max_graphs")
            break
        seen.add(key)
        valid, reason, relations = _validate_graph_structure(normalized, graph)
        if not valid:
            pruned[reason] = pruned.get(reason, 0) + 1
            continue
        replay = _replay_graph(
            normalized,
            graph,
            max_internal_steps=bounds["max_internal_steps"],
        )
        if not replay.valid:
            if replay.reason.startswith(
                ("hidden_execution_error:", "visible_execution_error:")
            ):
                raise ValueError(f"invalid ConDPOR program semantics: {replay.reason}")
            pruned[replay.reason] = pruned.get(replay.reason, 0) + 1
            if replay.internal_bound_hit:
                bound_reasons.add("max_internal_steps")
            continue
        smt2 = _solver_text(replay)
        try:
            status = solver.check(smt2)
        except _SolverLimit:
            bound_reasons.add("max_solver_checks")
            break
        if status == "unsat":
            pruned["path_unsat"] = pruned.get("path_unsat", 0) + 1
            continue
        if replay.error is not None or replay.next_tid is None:
            try:
                model = _lexicographic_model(smt2, replay.symbols, solver)
            except _SolverLimit:
                bound_reasons.add("max_solver_checks")
                break
            terminal = replay.terminal_kind or "complete"
            row = _execution_row(
                graph, replay, relations, model=model, terminal_kind=terminal
            )
            if terminal == "error":
                errors.append(row)
            else:
                executions.append(row)
            if terminal == "internal_bound":
                bound_reasons.add("max_internal_steps")
            continue
        if len(graph["events"]) >= bounds["max_events"]:
            bound_reasons.add("max_events")
            continue
        state = replay.states[replay.next_tid]
        assert replay.next_instruction is not None
        event = _event_spec(state, replay.next_instruction)
        base = _append_event(graph, event)
        children: list[dict[str, Any]] = []
        if event["kind"] == "R":
            obj = str(event["object"])
            for source in base["co"][obj]:
                child = copy.deepcopy(base)
                child["rf"][event["id"]] = source
                children.append(child)
        elif event["kind"] == "C":
            outcomes = [True] if event["op"] == "assume" else [False, True]
            for outcome in outcomes:
                child = copy.deepcopy(base)
                child["outcomes"][event["id"]] = outcome
                children.append(child)
        elif event["kind"] == "W":
            obj = str(event["object"])
            old_order = list(base["co"][obj])
            for position in range(1, len(old_order) + 1):
                child = copy.deepcopy(base)
                child["co"][obj].insert(position, event["id"])
                children.append(child)
                prior_reads = [
                    row
                    for row in child["events"][:-1]
                    if row["kind"] == "R"
                    and row["object"] == obj
                    and child["rf"].get(row["id"]) != event["id"]
                ]
                for read in prior_reads:
                    if len(revisits) >= bounds["max_revisits"]:
                        bound_reasons.add("max_revisits")
                        continue
                    revisit, deleted, revisit_reason = _restrict_for_backward_revisit(
                        normalized,
                        child,
                        read_id=str(read["id"]),
                        write_id=str(event["id"]),
                    )
                    audit = {
                        "source_graph_sha256": _graph_key(child),
                        "read": str(read["id"]),
                        "write": str(event["id"]),
                        "deleted_events": deleted,
                        "status": revisit_reason,
                    }
                    if revisit is not None:
                        audit["revisit_graph_sha256"] = _graph_key(revisit)
                        children.append(revisit)
                    revisits.append(audit)
        else:
            children.append(base)
        generated += len(children)
        for child in reversed(children):
            pending.append(child)
    executions.sort(key=lambda row: row["graph_sha256"])
    errors.sort(key=lambda row: row["graph_sha256"])
    revisits.sort(
        key=lambda row: (
            row["source_graph_sha256"],
            row["read"],
            row["write"],
            row["status"],
        )
    )
    complete = not pending and not bound_reasons
    certificate: dict[str, Any] = {
        "schema": CONDPOR_INTERPRETER_SCHEMA,
        "semantics": CONDPOR_INTERPRETER_SEMANTICS,
        "program": normalized,
        "program_sha256": _digest(normalized),
        "memory_model": "SC",
        "bounds": bounds,
        "status": "complete" if complete else "truncated",
        "bounded_exhaustive": complete,
        "executions": executions,
        "errors": errors,
        "backward_revisits": revisits,
        "bound_reasons": sorted(bound_reasons),
        "statistics": {
            "unique_graphs_visited": len(seen),
            "graphs_generated": generated,
            "duplicate_graphs_suppressed": duplicate_graphs,
            "solver_checks": solver.checks,
            "satisfiable_terminal_executions": len(executions),
            "satisfiable_assertion_failures": len(errors),
            "accepted_backward_revisits": sum(
                row["status"] == "accepted" for row in revisits
            ),
            "pruned_by_reason": dict(sorted(pruned.items())),
        },
        "checked_invariants": [
            "closed_ir_schema_and_target_validation",
            "event_identity_regenerated_by_interpreter_replay",
            "total_same_location_read_from",
            "per_location_total_coherence_with_initial_write",
            "sc_acyclicity_of_po_union_rf_union_co_union_fr",
            "qf_bv_path_feasibility",
            "lexicographically_minimal_unsigned_terminal_models",
            "content_addressed_duplicate_suppression",
        ],
        "claim_scope": {
            "bounded_soundness_for_supported_ir": True,
            "bounded_completeness_when_status_complete": complete,
            "native_program_equivalence_claimed": False,
            "unbounded_condpor_optimality_claimed": False,
        },
        "not_proved": [
            "native_pthread_or_llvm_event_extraction_equivalence",
            "dynamic_thread_creation_or_synchronization_semantics",
            "weak_memory_models_beyond_sc",
            "unbounded_program_soundness_completeness_or_optimality",
            "paper_level_unique_maximal_extension_for_arbitrary_languages",
        ],
    }
    certificate["certificate_sha256"] = _certificate_digest(certificate)
    return certificate


def verify_condpor_interpreter_certificate(
    certificate: Mapping[str, Any],
) -> bool:
    """Verify integrity and deterministically recompute the bounded search."""
    try:
        if certificate.get("schema") != CONDPOR_INTERPRETER_SCHEMA:
            return False
        if certificate.get("certificate_sha256") != _certificate_digest(certificate):
            return False
        bounds = certificate.get("bounds")
        program = certificate.get("program")
        if not isinstance(bounds, Mapping) or not isinstance(program, Mapping):
            return False
        expected = explore_condpor_program(
            program,
            max_graphs=int(bounds["max_graphs"]),
            max_events=int(bounds["max_events"]),
            max_revisits=int(bounds["max_revisits"]),
            max_solver_checks=int(bounds["max_solver_checks"]),
            max_internal_steps=int(bounds["max_internal_steps"]),
        )
        return dict(certificate) == expected
    except (KeyError, OSError, RuntimeError, TypeError, ValueError, OverflowError):
        return False
