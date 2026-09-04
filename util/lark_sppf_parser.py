#!/usr/bin/env python3
"""Persistent generalized Earley/SPPF oracle for verified parser proposals.

Lark's Earley parser returns its shared packed parse forest directly when
configured with ``ambiguity="forest"``.  This adapter preserves symbol,
intermediate, packed-production, and token nodes in structural trace v3.
Unsupported cyclic or oversized forests fail closed instead of being silently
truncated or expanded into an incomplete set of parse trees.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import struct
import sys
import time
from typing import Any, Mapping


TRACE_SCHEMA = "symcc-parser-structural-trace-v3"
NULLABLE_TRACE_SCHEMA = "symcc-parser-structural-trace-v4"
RPC_SCHEMA = "symcc-lark-sppf-rpc-v1"
TELEMETRY_SCHEMA = "symcc-lark-sppf-telemetry-v1"
FOREST_PROOF = "lark-earley-complete-sppf-v1"
MAX_RPC_BYTES = 64 * 1024
MAX_TRACE_BYTES = 1024 * 1024
MAX_GRAMMAR_BYTES = 1024 * 1024


class ParserFailure(RuntimeError):
    """Fail-closed grammar, parser, forest, or protocol error."""


@dataclass
class _TraceNode:
    symbol: str
    state: str
    start: int
    end: int
    alternatives: list[list[int]] = field(default_factory=list)

    @property
    def epsilon(self) -> bool:
        return self.start == self.end


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _bounded_label(value: Any, prefix: str) -> str:
    text = str(value)
    if (
        1 <= len(text) <= 128
        and text.isascii()
        and all(32 <= ord(character) < 127 for character in text)
    ):
        return text
    return f"{prefix}:{_sha256(text.encode('utf-8', errors='replace'))}"


def _symbol_name(value: Any) -> str:
    return str(getattr(value, "name", value))


def _rule_descriptor(rule: Any) -> dict[str, Any]:
    try:
        origin = _symbol_name(rule.origin)
        expansion = [_symbol_name(item) for item in rule.expansion]
        alias = "" if rule.alias is None else str(rule.alias)
    except (AttributeError, TypeError) as error:
        raise ParserFailure("SPPF packed node has an invalid rule") from error
    return {
        "origin": origin,
        "expansion": expansion,
        "alias": alias,
    }


def _rule_state(rule: Any, kind: str, pointer: int | None = None) -> str:
    descriptor = _rule_descriptor(rule)
    digest = _sha256(json.dumps(
        descriptor, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8"))
    detail = descriptor["alias"] or descriptor["origin"]
    suffix = "" if pointer is None else f":{pointer}"
    return _bounded_label(
        f"{kind}:{detail}{suffix}:{digest}", "earley-rule")


def _atomic_write_json(path: str, value: Mapping[str, Any]) -> None:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_TRACE_BYTES:
        raise ParserFailure("structural trace exceeds the size bound")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(
        f".{target.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


class SPPFTraceEncoder:
    """Convert one complete acyclic Lark SPPF to structural trace v3."""

    def __init__(
        self,
        *,
        input_size: int,
        parser_name: str,
        symbol_type: type,
        packed_type: type,
        token_type: type,
        max_nodes: int = 4096,
        max_edges: int = 16384,
        max_alternatives: int = 8,
        max_children: int = 64,
    ) -> None:
        self.input_size = int(input_size)
        self.parser_name = _bounded_label(parser_name, "lark-earley")
        self.symbol_type = symbol_type
        self.packed_type = packed_type
        self.token_type = token_type
        self.max_nodes = max(1, min(int(max_nodes), 4096))
        self.max_edges = max(1, min(int(max_edges), 16384))
        self.max_alternatives = max(
            1, min(int(max_alternatives), 8))
        self.max_children = max(1, min(int(max_children), 64))
        self.nodes: list[_TraceNode] = []
        self._object_nodes: dict[int, int] = {}
        self.raw_nodes = 0
        self.cloned_primary_nodes = 0

    def _span(self, start_raw: Any, end_raw: Any) -> tuple[int, int]:
        if (
            isinstance(start_raw, bool)
            or isinstance(end_raw, bool)
            or not isinstance(start_raw, int)
            or not isinstance(end_raw, int)
            or not 0 <= start_raw <= end_raw <= self.input_size
        ):
            raise ParserFailure("SPPF node has an invalid byte span")
        return start_raw, end_raw

    def _add_node(
        self,
        symbol: str,
        state: str,
        start: int,
        end: int,
    ) -> int:
        if len(self.nodes) >= self.max_nodes:
            raise ParserFailure("SPPF exceeds the node bound")
        index = len(self.nodes)
        self.nodes.append(_TraceNode(
            symbol=_bounded_label(symbol, "symbol"),
            state=_bounded_label(state, "state"),
            start=start,
            end=end,
        ))
        return index

    def _intern_token(self, token_node: Any) -> int:
        key = id(token_node)
        previous = self._object_nodes.get(key)
        if previous is not None:
            return previous
        try:
            token = token_node.token
            start, end = self._span(token.start_pos, token.end_pos)
            token_type = str(token.type)
        except AttributeError as error:
            raise ParserFailure("SPPF token has no byte span") from error
        index = self._add_node(token_type, "terminal", start, end)
        self._object_nodes[key] = index
        self.nodes[index].alternatives = [[]]
        return index

    def _intern_symbol(self, symbol: Any) -> int:
        key = id(symbol)
        previous = self._object_nodes.get(key)
        if previous is not None:
            return previous
        try:
            start, end = self._span(symbol.start, symbol.end)
            identity = symbol.s
            packed_children = tuple(symbol.children)
        except (AttributeError, TypeError) as error:
            raise ParserFailure("SPPF symbol node is invalid") from error
        if not 1 <= len(packed_children) <= self.max_alternatives:
            raise ParserFailure("SPPF symbol exceeds the alternative bound")
        if isinstance(identity, tuple):
            if len(identity) != 2:
                raise ParserFailure("SPPF intermediate identity is invalid")
            rule, pointer_raw = identity
            if (
                isinstance(pointer_raw, bool)
                or not isinstance(pointer_raw, int)
                or pointer_raw < 0
            ):
                raise ParserFailure("SPPF item pointer is invalid")
            symbol_name = f"@item:{_symbol_name(rule.origin)}"
            state = _rule_state(rule, "item", pointer_raw)
        else:
            symbol_name = _symbol_name(identity)
            state = "complete"
        index = self._add_node(symbol_name, state, start, end)
        self._object_nodes[key] = index
        alternatives: list[list[int]] = []
        for packed in packed_children:
            if not isinstance(packed, self.packed_type):
                raise ParserFailure("SPPF symbol child is not packed")
            try:
                rule = packed.rule
                children = tuple(packed.children)
            except (AttributeError, TypeError) as error:
                raise ParserFailure("SPPF packed node is invalid") from error
            if len(children) > self.max_children:
                raise ParserFailure("SPPF production exceeds the child bound")
            wrapper = self._add_node(
                "@packed", _rule_state(rule, "packed"), start, end)
            alternatives.append([wrapper])
            child_indexes = [self._intern(child) for child in children]
            self.nodes[wrapper].alternatives = [child_indexes]
        self.nodes[index].alternatives = alternatives
        return index

    def _intern(self, node: Any) -> int:
        if isinstance(node, self.token_type):
            return self._intern_token(node)
        if isinstance(node, self.symbol_type):
            return self._intern_symbol(node)
        raise ParserFailure("SPPF contains an unsupported node type")

    def _check_acyclic_and_reachable(self, root: int) -> None:
        colors = [0] * len(self.nodes)
        visited = 0

        def visit(index: int) -> None:
            nonlocal visited
            if colors[index] == 1:
                raise ParserFailure(
                    "cyclic SPPF cannot be represented by trace v3")
            if colors[index] == 2:
                return
            colors[index] = 1
            visited += 1
            for alternative in self.nodes[index].alternatives:
                for child in alternative:
                    visit(child)
            colors[index] = 2

        visit(root)
        if visited != len(self.nodes):
            raise ParserFailure("SPPF contains unreachable nodes")

    def _clone_node(self, index: int) -> int:
        source = self.nodes[index]
        clone = self._add_node(
            source.symbol, source.state, source.start, source.end)
        self.nodes[clone].alternatives = [
            list(alternative) for alternative in source.alternatives
        ]
        self.cloned_primary_nodes += 1
        return clone

    def _collapse_nullable_subforests(
        self,
        root: int,
    ) -> tuple[int, list[dict[str, Any]]]:
        needs_certificate = any(
            node.epsilon and any(node.alternatives)
            for node in self.nodes
        )
        if not needs_certificate:
            return root, []
        rules_by_core: dict[str, dict[str, Any]] = {}
        for node in self.nodes:
            if not node.epsilon:
                continue
            for alternative in node.alternatives:
                if any(not self.nodes[child].epsilon
                       for child in alternative):
                    raise ParserFailure(
                        "nullable SPPF node has a non-nullable child")
                rule = {
                    "lhs": [node.symbol, node.state],
                    "rhs": [
                        [
                            self.nodes[child].symbol,
                            self.nodes[child].state,
                        ]
                        for child in alternative
                    ],
                }
                core = json.dumps(
                    rule, sort_keys=True, separators=(",", ":"))
                rules_by_core[core] = rule
            node.alternatives = [[]]
        rules = [rules_by_core[key] for key in sorted(rules_by_core)]
        if not 1 <= len(rules) <= 32:
            raise ParserFailure(
                "nullable SPPF exceeds the certificate rule bound")

        reachable: set[int] = set()
        pending = [root]
        while pending:
            current = pending.pop()
            if current in reachable:
                continue
            reachable.add(current)
            for alternative in self.nodes[current].alternatives:
                pending.extend(alternative)
        order = sorted(reachable)
        remap = {old: new for new, old in enumerate(order)}
        self.nodes = [
            _TraceNode(
                symbol=self.nodes[old].symbol,
                state=self.nodes[old].state,
                start=self.nodes[old].start,
                end=self.nodes[old].end,
                alternatives=[
                    [remap[child] for child in alternative]
                    for alternative in self.nodes[old].alternatives
                ],
            )
            for old in order
        ]
        return remap[root], rules

    def _make_primary_derivation_a_tree(self, root: int) -> None:
        owners: dict[int, int] = {root: -1}

        def claim(index: int, parent: int) -> int:
            if index in owners:
                index = self._clone_node(index)
            owners[index] = parent
            primary = self.nodes[index].alternatives[0]
            for slot, child in enumerate(tuple(primary)):
                primary[slot] = claim(child, index)
            return index

        primary = self.nodes[root].alternatives[0]
        for slot, child in enumerate(tuple(primary)):
            primary[slot] = claim(child, root)

    def _topological_order(self, root: int) -> list[int]:
        incoming = [0] * len(self.nodes)
        for node in self.nodes:
            for alternative in node.alternatives:
                for child in alternative:
                    incoming[child] += 1
        if incoming[root] != 0:
            raise ParserFailure("SPPF root has an incoming edge")
        ready = [index for index, degree in enumerate(incoming) if degree == 0]
        ready.sort(reverse=True)
        order: list[int] = []
        while ready:
            index = ready.pop()
            order.append(index)
            for alternative in self.nodes[index].alternatives:
                for child in alternative:
                    incoming[child] -= 1
                    if incoming[child] == 0:
                        ready.append(child)
                        ready.sort(reverse=True)
        if len(order) != len(self.nodes):
            raise ParserFailure("SPPF topological ordering failed")
        if order[0] != root:
            raise ParserFailure("SPPF has more than one graph root")
        return order

    def encode(self, root_node: Any, elapsed_us: int) -> dict[str, Any]:
        root = self._intern(root_node)
        self.raw_nodes = len(self.nodes)
        self._check_acyclic_and_reachable(root)
        root, nullable_rules = self._collapse_nullable_subforests(root)
        self._make_primary_derivation_a_tree(root)
        self._check_acyclic_and_reachable(root)
        order = self._topological_order(root)
        remap = {old: new for new, old in enumerate(order)}
        encoded_nodes: list[dict[str, Any]] = []
        edge_count = 0
        alternative_count = 0
        for old in order:
            node = self.nodes[old]
            alternatives: list[list[int]] = []
            for raw_alternative in node.alternatives:
                alternative = [remap[child] for child in raw_alternative]
                alternative.sort(key=lambda child: (
                    self.nodes[order[child]].start,
                    self.nodes[order[child]].end,
                    child,
                ))
                if len(set(alternative)) != len(alternative):
                    raise ParserFailure(
                        "SPPF production repeats one packed child")
                alternatives.append(alternative)
                edge_count += len(alternative)
            if len({tuple(item) for item in alternatives}) != len(
                    alternatives):
                raise ParserFailure(
                    "SPPF alternatives collapse under trace v3")
            alternative_count += len(alternatives)
            encoded_nodes.append({
                "symbol": node.symbol,
                "state": node.state,
                "start": node.start,
                "end": node.end,
                "epsilon": node.epsilon,
                "alternatives": alternatives,
            })
        if edge_count > self.max_edges:
            raise ParserFailure("SPPF exceeds the edge bound")
        trace = {
            "schema": (
                NULLABLE_TRACE_SCHEMA if nullable_rules else TRACE_SCHEMA),
            "parser": self.parser_name,
            "accepted": True,
            "roots": [0],
            "nodes": encoded_nodes,
            "forest_telemetry": {
                "schema": TELEMETRY_SCHEMA,
                "proof": FOREST_PROOF,
                "complete": True,
                "elapsed_us": max(0, int(elapsed_us)),
                "raw_nodes": self.raw_nodes,
                "encoded_nodes": len(encoded_nodes),
                "primary_clones": self.cloned_primary_nodes,
                "packed_alternatives": alternative_count,
                "edges": edge_count,
                "nullable_rules": len(nullable_rules),
            },
        }
        if nullable_rules:
            trace["nullable_rules"] = nullable_rules
        return trace


class LarkSPPFEngine:
    """One immutable grammar with a generalized Earley parser."""

    def __init__(
        self,
        parser: Any,
        *,
        parser_name: str,
        grammar_sha256: str,
        lark_version: str,
        symbol_type: type,
        packed_type: type,
        token_type: type,
        unexpected_input_type: type[BaseException],
        max_nodes: int = 4096,
        max_edges: int = 16384,
        max_input_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        self.parser = parser
        self.parser_name = _bounded_label(parser_name, "lark-earley")
        self.grammar_sha256 = grammar_sha256
        self.lark_version = str(lark_version)
        self.symbol_type = symbol_type
        self.packed_type = packed_type
        self.token_type = token_type
        self.unexpected_input_type = unexpected_input_type
        self.max_nodes = max(1, min(int(max_nodes), 4096))
        self.max_edges = max(1, min(int(max_edges), 16384))
        self.max_input_bytes = max(
            1, min(int(max_input_bytes), 1024 * 1024 * 1024))
        self.requests = 0
        self.accepted = 0
        self.rejected = 0
        self.failures = 0
        self.elapsed_us = 0

    def _rejected_trace(
        self,
        candidate: bytes,
        elapsed_us: int,
        error: BaseException,
    ) -> dict[str, Any]:
        size = len(candidate)
        return {
            "schema": TRACE_SCHEMA,
            "parser": self.parser_name,
            "accepted": False,
            "roots": [0],
            "nodes": [{
                "symbol": "@parse-error",
                "state": _bounded_label(
                    type(error).__name__, "parse-error"),
                "start": 0,
                "end": size,
                "epsilon": size == 0,
                "alternatives": [[]],
            }],
            "forest_telemetry": {
                "schema": TELEMETRY_SCHEMA,
                "proof": FOREST_PROOF,
                "grammar_sha256": self.grammar_sha256,
                "lark_version": self.lark_version,
                "complete": False,
                "elapsed_us": elapsed_us,
                "raw_nodes": 0,
                "encoded_nodes": 1,
                "primary_clones": 0,
                "packed_alternatives": 1,
                "edges": 0,
                "nullable_rules": 0,
            },
        }

    def parse(self, candidate: bytes) -> dict[str, Any]:
        if len(candidate) > self.max_input_bytes:
            raise ParserFailure("candidate input exceeds the size bound")
        self.requests += 1
        started = time.monotonic_ns()
        try:
            root = self.parser.parse(candidate)
        except self.unexpected_input_type as error:
            elapsed_us = (time.monotonic_ns() - started) // 1000
            self.elapsed_us += elapsed_us
            self.rejected += 1
            return self._rejected_trace(candidate, elapsed_us, error)
        elapsed_us = (time.monotonic_ns() - started) // 1000
        encoder = SPPFTraceEncoder(
            input_size=len(candidate),
            parser_name=self.parser_name,
            symbol_type=self.symbol_type,
            packed_type=self.packed_type,
            token_type=self.token_type,
            max_nodes=self.max_nodes,
            max_edges=self.max_edges,
        )
        try:
            trace = encoder.encode(root, elapsed_us)
        except ParserFailure:
            self.failures += 1
            raise
        trace["forest_telemetry"].update({
            "grammar_sha256": self.grammar_sha256,
            "lark_version": self.lark_version,
        })
        self.elapsed_us += elapsed_us
        self.accepted += 1
        return trace

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema": "symcc-lark-sppf-snapshot-v1",
            "parser": self.parser_name,
            "grammar_sha256": self.grammar_sha256,
            "lark_version": self.lark_version,
            "requests": self.requests,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "failures": self.failures,
            "elapsed_us": self.elapsed_us,
            "max_nodes": self.max_nodes,
            "max_edges": self.max_edges,
            "max_input_bytes": self.max_input_bytes,
        }


def load_lark_engine(
    grammar_path: str,
    *,
    start: str = "start",
    parser_name: str = "",
    max_nodes: int = 4096,
    max_edges: int = 16384,
    max_input_bytes: int = 16 * 1024 * 1024,
) -> LarkSPPFEngine:
    try:
        encoded = Path(grammar_path).read_bytes()
    except OSError as error:
        raise ParserFailure("grammar is unreadable") from error
    if not 1 <= len(encoded) <= MAX_GRAMMAR_BYTES:
        raise ParserFailure("grammar exceeds the size bound")
    try:
        grammar = encoded.decode("utf-8")
    except UnicodeError as error:
        raise ParserFailure("grammar is not UTF-8") from error
    try:
        import lark
        from lark import Lark
        from lark.exceptions import LarkError, UnexpectedInput
        from lark.parsers.earley_forest import (
            PackedNode,
            SymbolNode,
            TokenNode,
        )
    except ImportError as error:
        raise ParserFailure("Lark Earley parser is unavailable") from error
    try:
        parser = Lark(
            grammar,
            parser="earley",
            lexer="dynamic_complete",
            ambiguity="forest",
            start=start,
            use_bytes=True,
            keep_all_tokens=True,
            propagate_positions=True,
            ordered_sets=True,
        )
    except (LarkError, TypeError, ValueError) as error:
        raise ParserFailure(
            "Lark Earley grammar could not be loaded") from error
    digest = _sha256(encoded)
    label = parser_name or (
        f"lark-earley-{lark.__version__}:{digest[:16]}")
    return LarkSPPFEngine(
        parser,
        parser_name=label,
        grammar_sha256=digest,
        lark_version=lark.__version__,
        symbol_type=SymbolNode,
        packed_type=PackedNode,
        token_type=TokenNode,
        unexpected_input_type=UnexpectedInput,
        max_nodes=max_nodes,
        max_edges=max_edges,
        max_input_bytes=max_input_bytes,
    )


def _recv_exact(connection: socket.socket, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        chunk = connection.recv(length - len(chunks))
        if not chunk:
            raise ParserFailure("truncated RPC frame")
        chunks.extend(chunk)
    return bytes(chunks)


def _recv_message(connection: socket.socket) -> dict[str, Any]:
    length = struct.unpack("!I", _recv_exact(connection, 4))[0]
    if not 1 <= length <= MAX_RPC_BYTES:
        raise ParserFailure("RPC frame exceeds the size bound")
    try:
        raw = json.loads(_recv_exact(connection, length).decode("utf-8"))
    except (UnicodeError, ValueError, TypeError) as error:
        raise ParserFailure("RPC payload is invalid") from error
    if not isinstance(raw, dict) or raw.get("schema") != RPC_SCHEMA:
        raise ParserFailure("RPC schema is invalid")
    return raw


def _send_message(
    connection: socket.socket,
    value: Mapping[str, Any],
) -> None:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    if not 1 <= len(encoded) <= MAX_RPC_BYTES:
        raise ParserFailure("RPC response exceeds the size bound")
    connection.sendall(struct.pack("!I", len(encoded)) + encoded)


def rpc_request(
    socket_path: str,
    request: Mapping[str, Any],
    *,
    timeout: float = 5.0,
) -> dict[str, Any]:
    payload = dict(request)
    payload["schema"] = RPC_SCHEMA
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(max(0.01, min(float(timeout), 60.0)))
        connection.connect(socket_path)
        _send_message(connection, payload)
        return _recv_message(connection)


def _handle_request(
    engine: LarkSPPFEngine,
    request: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    command = request.get("command")
    if command == "ping":
        return {
            "schema": RPC_SCHEMA,
            "ok": True,
            "snapshot": engine.snapshot(),
        }, False
    if command == "shutdown":
        return {
            "schema": RPC_SCHEMA,
            "ok": True,
            "snapshot": engine.snapshot(),
        }, True
    if command != "parse":
        raise ParserFailure("RPC command is unsupported")
    input_path = request.get("input_path")
    trace_path = request.get("trace_path")
    if (
        not isinstance(input_path, str) or not input_path
        or not isinstance(trace_path, str) or not trace_path
    ):
        raise ParserFailure("RPC paths are invalid")
    try:
        candidate = Path(input_path).read_bytes()
    except OSError as error:
        raise ParserFailure("candidate input is unreadable") from error
    trace = engine.parse(candidate)
    _atomic_write_json(trace_path, trace)
    return {
        "schema": RPC_SCHEMA,
        "ok": True,
        "accepted": bool(trace["accepted"]),
        "snapshot": engine.snapshot(),
    }, False


def serve(socket_path: str, engine: LarkSPPFEngine) -> int:
    target = Path(socket_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise ParserFailure("refusing to replace an existing socket path")
    stop = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    previous_term = signal.signal(signal.SIGTERM, request_stop)
    previous_int = signal.signal(signal.SIGINT, request_stop)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(socket_path)
        os.chmod(socket_path, 0o600)
        server.listen(16)
        server.settimeout(0.25)
        while not stop:
            try:
                connection, _address = server.accept()
            except TimeoutError:
                continue
            with connection:
                shutdown = False
                try:
                    request = _recv_message(connection)
                    response, shutdown = _handle_request(engine, request)
                except (OSError, ParserFailure) as error:
                    response = {
                        "schema": RPC_SCHEMA,
                        "ok": False,
                        "error": str(error)[:512],
                    }
                try:
                    _send_message(connection, response)
                except (OSError, ParserFailure):
                    pass
                stop = stop or shutdown
    finally:
        server.close()
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
        try:
            target.unlink()
        except OSError:
            pass
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)

    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--socket", required=True)
    serve_parser.add_argument("--grammar", required=True)
    serve_parser.add_argument("--start", default="start")
    serve_parser.add_argument("--parser-name", default="")
    serve_parser.add_argument("--max-nodes", type=int, default=4096)
    serve_parser.add_argument("--max-edges", type=int, default=16384)
    serve_parser.add_argument(
        "--max-input-bytes", type=int, default=16 * 1024 * 1024)

    parse_parser = subparsers.add_parser("parse")
    parse_parser.add_argument("--socket", required=True)
    parse_parser.add_argument("--input", required=True)
    parse_parser.add_argument("--trace", required=True)
    parse_parser.add_argument("--cache", default="")
    parse_parser.add_argument("--timeout", type=float, default=5.0)

    for name in ("ping", "shutdown"):
        command_parser = subparsers.add_parser(name)
        command_parser.add_argument("--socket", required=True)
        command_parser.add_argument("--timeout", type=float, default=5.0)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.operation == "serve":
            engine = load_lark_engine(
                args.grammar,
                start=args.start,
                parser_name=args.parser_name,
                max_nodes=args.max_nodes,
                max_edges=args.max_edges,
                max_input_bytes=args.max_input_bytes,
            )
            return serve(args.socket, engine)
        if args.operation == "parse":
            response = rpc_request(
                args.socket,
                {
                    "command": "parse",
                    "input_path": args.input,
                    "trace_path": args.trace,
                },
                timeout=args.timeout,
            )
            if not response.get("ok"):
                print(str(response.get("error", "parser RPC failed")),
                      file=sys.stderr)
                return 2
            return 0 if response.get("accepted") else 1
        response = rpc_request(
            args.socket,
            {"command": args.operation},
            timeout=args.timeout,
        )
        if not response.get("ok"):
            print(str(response.get("error", "parser RPC failed")),
                  file=sys.stderr)
            return 2
        print(json.dumps(
            response.get("snapshot", {}),
            sort_keys=True,
            separators=(",", ":"),
        ))
        return 0
    except (OSError, ParserFailure) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
