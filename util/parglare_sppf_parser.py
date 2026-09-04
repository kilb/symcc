#!/usr/bin/env python3
"""Persistent independent GLR/SPPF oracle based on parglare.

Parglare exposes its shared packed forest as Parent ambiguity nodes containing
NodeNonTerm/NodeTerm possibilities.  This adapter encodes that graph directly
into SymCC structural trace v3/v4 without enumerating parse trees.  It shares
the established bounded DAG/nullable encoder with the Lark Earley provider so
the two generalized algorithms can be compared under one manager contract.
"""

from __future__ import annotations

import argparse
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

from lark_sppf_parser import (
    MAX_GRAMMAR_BYTES,
    ParserFailure,
    SPPFTraceEncoder,
    _atomic_write_json,
    _bounded_label,
    _sha256,
)


RPC_SCHEMA = "symcc-parglare-sppf-rpc-v1"
TELEMETRY_SCHEMA = "symcc-parglare-glr-sppf-telemetry-v1"
FOREST_PROOF = "parglare-glr-complete-sppf-v1"
MAX_RPC_BYTES = 64 * 1024


def _symbol_name(value: Any) -> str:
    return str(getattr(value, "name", value))


def _production_state(production: Any) -> str:
    try:
        descriptor = {
            "id": int(production.prod_id),
            "lhs": _symbol_name(production.symbol),
            "rhs": [
                _symbol_name(symbol) for symbol in production.rhs
            ],
        }
    except (AttributeError, TypeError, ValueError) as error:
        raise ParserFailure("GLR production metadata is invalid") from error
    digest = hashlib.sha256(json.dumps(
        descriptor,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return _bounded_label(
        f"packed:{descriptor['lhs']}:{descriptor['id']}:{digest}",
        "parglare-production",
    )


class ParglareSPPFTraceEncoder(SPPFTraceEncoder):
    """Convert one complete acyclic parglare SPPF to trace v3/v4."""

    def __init__(
        self,
        *,
        input_size: int,
        parser_name: str,
        parent_type: type,
        nonterminal_type: type,
        terminal_type: type,
        max_nodes: int = 4096,
        max_edges: int = 16384,
    ) -> None:
        super().__init__(
            input_size=input_size,
            parser_name=parser_name,
            symbol_type=parent_type,
            packed_type=nonterminal_type,
            token_type=terminal_type,
            max_nodes=max_nodes,
            max_edges=max_edges,
        )
        self.parent_type = parent_type
        self.nonterminal_type = nonterminal_type
        self.terminal_type = terminal_type

    def _intern_parent(self, parent: Any) -> int:
        key = id(parent)
        previous = self._object_nodes.get(key)
        if previous is not None:
            return previous
        try:
            start, end = self._span(
                parent.start_position, parent.end_position)
            possibilities = tuple(parent.possibilities)
        except (AttributeError, TypeError) as error:
            raise ParserFailure("GLR parent node is invalid") from error
        if not 1 <= len(possibilities) <= self.max_alternatives:
            raise ParserFailure("GLR node exceeds the alternative bound")
        try:
            symbols = {
                _symbol_name(possibility.symbol)
                for possibility in possibilities
            }
        except AttributeError as error:
            raise ParserFailure(
                "GLR possibility has no grammar symbol") from error
        if len(symbols) != 1:
            raise ParserFailure(
                "GLR ambiguity node mixes grammar symbols")
        index = self._add_node(next(iter(symbols)), "complete", start, end)
        self._object_nodes[key] = index
        alternatives: list[list[int]] = []
        for possibility in possibilities:
            if isinstance(possibility, self.nonterminal_type):
                try:
                    children = tuple(possibility.children)
                    production = possibility.production
                except (AttributeError, TypeError) as error:
                    raise ParserFailure(
                        "GLR nonterminal possibility is invalid") from error
                if len(children) > self.max_children:
                    raise ParserFailure(
                        "GLR production exceeds the child bound")
                wrapper = self._add_node(
                    "@packed",
                    _production_state(production),
                    start,
                    end,
                )
                child_indexes = []
                for child in children:
                    if not isinstance(child, self.parent_type):
                        raise ParserFailure(
                            "GLR production child is not a packed parent")
                    child_indexes.append(self._intern_parent(child))
                self.nodes[wrapper].alternatives = [child_indexes]
            elif isinstance(possibility, self.terminal_type):
                try:
                    token_symbol = _symbol_name(
                        possibility.token.symbol)
                except AttributeError as error:
                    raise ParserFailure(
                        "GLR terminal possibility is invalid") from error
                wrapper = self._add_node(
                    "@token",
                    _bounded_label(
                        f"terminal:{token_symbol}",
                        "parglare-terminal",
                    ),
                    start,
                    end,
                )
                self.nodes[wrapper].alternatives = [[]]
            else:
                raise ParserFailure(
                    "GLR forest contains an unsupported possibility")
            alternatives.append([wrapper])
        self.nodes[index].alternatives = alternatives
        return index

    def _intern(self, node: Any) -> int:
        if not isinstance(node, self.parent_type):
            raise ParserFailure("GLR forest root is not a packed parent")
        return self._intern_parent(node)


class ParglareSPPFEngine:
    """One immutable grammar with a generalized LR parser."""

    def __init__(
        self,
        parser: Any,
        *,
        parser_name: str,
        grammar_sha256: str,
        parglare_version: str,
        parent_type: type,
        nonterminal_type: type,
        terminal_type: type,
        parse_error_type: type[BaseException],
        max_nodes: int = 4096,
        max_edges: int = 16384,
        max_input_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        self.parser = parser
        self.parser_name = _bounded_label(parser_name, "parglare-glr")
        self.grammar_sha256 = grammar_sha256
        self.parglare_version = str(parglare_version)
        self.parent_type = parent_type
        self.nonterminal_type = nonterminal_type
        self.terminal_type = terminal_type
        self.parse_error_type = parse_error_type
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
            "schema": "symcc-parser-structural-trace-v3",
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
                "parglare_version": self.parglare_version,
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
        text = candidate.decode("latin-1")
        started = time.monotonic_ns()
        try:
            forest = self.parser.parse(text)
        except self.parse_error_type as error:
            elapsed_us = (time.monotonic_ns() - started) // 1000
            self.elapsed_us += elapsed_us
            self.rejected += 1
            return self._rejected_trace(candidate, elapsed_us, error)
        except IndexError as error:
            # Parglare 0.21.1 indexes an empty line list while constructing
            # the diagnostic for a rejected empty input.
            if candidate or str(error) != "list index out of range":
                self.failures += 1
                raise ParserFailure("GLR parser failed internally") from error
            elapsed_us = (time.monotonic_ns() - started) // 1000
            self.elapsed_us += elapsed_us
            self.rejected += 1
            return self._rejected_trace(candidate, elapsed_us, error)
        elapsed_us = (time.monotonic_ns() - started) // 1000
        try:
            root = forest.result
        except AttributeError as error:
            self.failures += 1
            raise ParserFailure("GLR parser returned no SPPF") from error
        encoder = ParglareSPPFTraceEncoder(
            input_size=len(candidate),
            parser_name=self.parser_name,
            parent_type=self.parent_type,
            nonterminal_type=self.nonterminal_type,
            terminal_type=self.terminal_type,
            max_nodes=self.max_nodes,
            max_edges=self.max_edges,
        )
        try:
            trace = encoder.encode(root, elapsed_us)
        except ParserFailure:
            self.failures += 1
            raise
        trace["forest_telemetry"].update({
            "schema": TELEMETRY_SCHEMA,
            "proof": FOREST_PROOF,
            "grammar_sha256": self.grammar_sha256,
            "parglare_version": self.parglare_version,
        })
        self.elapsed_us += elapsed_us
        self.accepted += 1
        return trace

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema": "symcc-parglare-sppf-snapshot-v1",
            "parser": self.parser_name,
            "grammar_sha256": self.grammar_sha256,
            "parglare_version": self.parglare_version,
            "requests": self.requests,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "failures": self.failures,
            "elapsed_us": self.elapsed_us,
            "max_nodes": self.max_nodes,
            "max_edges": self.max_edges,
            "max_input_bytes": self.max_input_bytes,
        }


def load_parglare_engine(
    grammar_path: str,
    *,
    parser_name: str = "",
    max_nodes: int = 4096,
    max_edges: int = 16384,
    max_input_bytes: int = 16 * 1024 * 1024,
) -> ParglareSPPFEngine:
    try:
        encoded = Path(grammar_path).read_bytes()
    except OSError as error:
        raise ParserFailure("grammar is unreadable") from error
    if not 1 <= len(encoded) <= MAX_GRAMMAR_BYTES:
        raise ParserFailure("grammar exceeds the size bound")
    try:
        grammar_text = encoded.decode("utf-8")
    except UnicodeError as error:
        raise ParserFailure("grammar is not UTF-8") from error
    try:
        import parglare
        from parglare import GLRParser, Grammar, SyntaxError
        from parglare.exceptions import ParglareError
        from parglare.glr import Parent
        from parglare.trees import NodeNonTerm, NodeTerm
    except ImportError as error:
        raise ParserFailure("parglare GLR parser is unavailable") from error
    try:
        grammar = Grammar.from_string(grammar_text)
        parser = GLRParser(
            grammar,
            build_tree=True,
            lexical_disambiguation=False,
            prefer_shifts=False,
            prefer_shifts_over_empty=False,
        )
    except (ParglareError, TypeError, ValueError, RuntimeError) as error:
        raise ParserFailure(
            "parglare grammar could not be loaded") from error
    digest = _sha256(encoded)
    label = parser_name or (
        f"parglare-glr-{parglare.__version__}:{digest[:16]}")
    return ParglareSPPFEngine(
        parser,
        parser_name=label,
        grammar_sha256=digest,
        parglare_version=parglare.__version__,
        parent_type=Parent,
        nonterminal_type=NodeNonTerm,
        terminal_type=NodeTerm,
        parse_error_type=SyntaxError,
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
    engine: ParglareSPPFEngine,
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
        not isinstance(input_path, str) or not input_path or
        not isinstance(trace_path, str) or not trace_path
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


def serve(socket_path: str, engine: ParglareSPPFEngine) -> int:
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
            return serve(
                args.socket,
                load_parglare_engine(
                    args.grammar,
                    parser_name=args.parser_name,
                    max_nodes=args.max_nodes,
                    max_edges=args.max_edges,
                    max_input_bytes=args.max_input_bytes,
                ),
            )
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
