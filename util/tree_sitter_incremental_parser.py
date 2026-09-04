#!/usr/bin/env python3
"""Persistent Tree-sitter oracle for verified incremental parser reuse.

The server keeps native TSTree objects in memory.  A client invocation follows
the existing SYMCC_PROPOSAL_PARSER {input}/{trace}/{cache} contract, so the
proposal manager remains the authority that validates the resulting trace and
reuse receipt.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import importlib
import json
import os
from pathlib import Path
import signal
import socket
import struct
import sys
import time
from typing import Any, Mapping, Sequence


TRACE_SCHEMA = "symcc-parser-structural-trace-v2"
MANIFEST_SCHEMA = "symcc-parser-incremental-cache-v1"
REUSE_SCHEMA = "symcc-parser-incremental-reuse-v1"
RPC_SCHEMA = "symcc-tree-sitter-incremental-rpc-v1"
MAX_RPC_BYTES = 64 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_TRACE_BYTES = 1024 * 1024


class ParserFailure(RuntimeError):
    """Fail-closed parser or protocol error."""


@dataclass
class _CachedTree:
    source: bytes
    tree: Any
    node_ids: tuple[int, ...]
    last_used: int


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
    digest = _sha256(text.encode("utf-8", errors="replace"))
    return f"{prefix}:{digest}"


def _point(content: bytes, offset: int) -> tuple[int, int]:
    if not 0 <= offset <= len(content):
        raise ParserFailure("edit offset is outside the source")
    row = content.count(b"\n", 0, offset)
    line = content.rfind(b"\n", 0, offset)
    return row, offset if line < 0 else offset - line - 1


def load_cache_manifest(path: str) -> dict[str, Any] | None:
    if not path:
        return None
    try:
        encoded = Path(path).read_bytes()
        if len(encoded) > MAX_MANIFEST_BYTES:
            raise ParserFailure("cache manifest exceeds the size bound")
        raw = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        raise ParserFailure("cache manifest is unreadable") from error
    if not isinstance(raw, dict) or raw.get("schema") != MANIFEST_SCHEMA:
        raise ParserFailure("cache manifest schema is invalid")
    raw = dict(raw)
    raw["manifest_sha256"] = _sha256(encoded)
    return raw


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


class IncrementalTreeSitterEngine:
    """Serialized native parser with a bounded content-addressed TSTree cache."""

    def __init__(
        self,
        parser: Any,
        *,
        parser_name: str,
        max_trees: int = 256,
        max_nodes: int = 4096,
        max_input_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        self.parser = parser
        self.parser_name = _bounded_label(parser_name, "tree-sitter")
        self.max_trees = max(1, min(int(max_trees), 4096))
        self.max_nodes = max(1, min(int(max_nodes), 4096))
        self.max_input_bytes = max(
            1, min(int(max_input_bytes), 1024 * 1024 * 1024))
        self._cache: dict[str, _CachedTree] = {}
        self._clock = 0
        self.requests = 0
        self.cold_parses = 0
        self.incremental_parses = 0
        self.reused_nodes = 0

    @property
    def cache_entries(self) -> int:
        return len(self._cache)

    def _trace(
        self,
        tree: Any,
        *,
        accepted: bool,
        receipt: Mapping[str, Any] | None = None,
        telemetry: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], tuple[int, ...]]:
        try:
            root = tree.root_node
        except AttributeError as error:
            raise ParserFailure("parser returned no syntax tree") from error
        pending: list[tuple[Any, int]] = [(root, -1)]
        nodes: list[dict[str, Any]] = []
        node_ids: list[int] = []
        while pending:
            node, parent = pending.pop()
            if len(nodes) >= self.max_nodes:
                raise ParserFailure("syntax tree exceeds the node bound")
            index = len(nodes)
            try:
                start = int(node.start_byte)
                end = int(node.end_byte)
                node_id = int(node.id)
                children = tuple(node.children)
                grammar_id = int(getattr(node, "grammar_id", 0))
                parse_state = int(getattr(node, "parse_state", 0))
                next_state = int(getattr(node, "next_parse_state", 0))
            except (AttributeError, TypeError, ValueError) as error:
                raise ParserFailure("syntax node metadata is invalid") from error
            nodes.append({
                "symbol": _bounded_label(node.type, "symbol"),
                "state": f"g{grammar_id}:p{parse_state}:n{next_state}",
                "start": start,
                "end": end,
                "parent": parent,
                "epsilon": start == end,
            })
            node_ids.append(node_id)
            for child in reversed(children):
                pending.append((child, index))
        trace: dict[str, Any] = {
            "schema": TRACE_SCHEMA,
            "parser": self.parser_name,
            "accepted": bool(accepted),
            "nodes": nodes,
        }
        if receipt is not None:
            trace["incremental_cache"] = dict(receipt)
        if telemetry is not None:
            trace["incremental_telemetry"] = dict(telemetry)
        return trace, tuple(node_ids)

    @staticmethod
    def _validated_edit(
        manifest: Mapping[str, Any] | None,
        candidate: bytes,
        cache: Mapping[str, _CachedTree],
    ) -> tuple[_CachedTree, int, int, bytes] | None:
        if (
            manifest is None
            or manifest.get("mode") != "incremental"
            or manifest.get("candidate_input_sha256") != _sha256(candidate)
        ):
            return None
        base_digest = manifest.get("base_input_sha256")
        edit = manifest.get("edit")
        if not isinstance(base_digest, str) or not isinstance(edit, Mapping):
            return None
        base = cache.get(base_digest)
        if base is None or _sha256(base.source) != base_digest:
            return None
        try:
            offset = int(edit["offset"])
            delete = int(edit["delete"])
            insert = bytes.fromhex(str(edit["insert_hex"]))
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        if (
            isinstance(edit.get("offset"), bool)
            or isinstance(edit.get("delete"), bool)
            or not 0 <= offset <= len(base.source)
            or not 0 <= delete <= len(base.source) - offset
            or base.source[:offset] + insert + base.source[
                offset + delete:] != candidate
        ):
            return None
        return base, offset, delete, insert

    @staticmethod
    def _reuse_receipt(
        manifest: Mapping[str, Any],
        base: _CachedTree,
        candidate_node_ids: Sequence[int],
    ) -> dict[str, Any]:
        indexes_by_id = {
            int(node_id): index
            for index, node_id in enumerate(candidate_node_ids)
        }
        reused: list[dict[str, int]] = []
        seen_candidate: set[int] = set()
        offered = manifest.get("reusable_nodes", ())
        if not isinstance(offered, Sequence) or isinstance(
                offered, (str, bytes)):
            offered = ()
        for item in offered[:512]:
            if not isinstance(item, Mapping):
                continue
            base_index = item.get("base_index")
            if (
                not isinstance(base_index, int)
                or isinstance(base_index, bool)
                or not 0 <= base_index < len(base.node_ids)
            ):
                continue
            candidate_index = indexes_by_id.get(base.node_ids[base_index])
            if candidate_index is None or candidate_index in seen_candidate:
                continue
            seen_candidate.add(candidate_index)
            reused.append({
                "base_index": base_index,
                "candidate_index": candidate_index,
            })
        return {
            "schema": REUSE_SCHEMA,
            "manifest_sha256": str(manifest.get("manifest_sha256", "")),
            "proof": "tree-sitter-node-id-v1",
            "reused_nodes": reused,
        }

    def _store(
        self,
        digest: str,
        source: bytes,
        tree: Any,
        node_ids: tuple[int, ...],
    ) -> None:
        self._clock += 1
        while digest not in self._cache and len(self._cache) >= self.max_trees:
            victim = min(
                self._cache,
                key=lambda item: (
                    self._cache[item].last_used,
                    item,
                ),
            )
            del self._cache[victim]
        self._cache[digest] = _CachedTree(
            source=source,
            tree=tree,
            node_ids=node_ids,
            last_used=self._clock,
        )

    def parse(
        self,
        candidate: bytes,
        manifest: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if len(candidate) > self.max_input_bytes:
            raise ParserFailure("candidate input exceeds the size bound")
        self.requests += 1
        started = time.monotonic_ns()
        incremental = self._validated_edit(
            manifest, candidate, self._cache)
        base: _CachedTree | None = None
        if incremental is None:
            self.cold_parses += 1
            tree = self.parser.parse(candidate)
            mode = "cold"
        else:
            base, offset, delete, insert = incremental
            try:
                old_tree = base.tree.copy()
                old_tree.edit(
                    start_byte=offset,
                    old_end_byte=offset + delete,
                    new_end_byte=offset + len(insert),
                    start_point=_point(base.source, offset),
                    old_end_point=_point(base.source, offset + delete),
                    new_end_point=_point(candidate, offset + len(insert)),
                )
                tree = self.parser.parse(candidate, old_tree)
            except (AttributeError, TypeError, ValueError) as error:
                raise ParserFailure(
                    "native incremental parse failed") from error
            self.incremental_parses += 1
            self._clock += 1
            base.last_used = self._clock
            mode = "incremental"
        if tree is None:
            raise ParserFailure("parser returned no syntax tree")
        try:
            accepted = not bool(tree.root_node.has_error)
        except AttributeError as error:
            raise ParserFailure("syntax root has no error status") from error
        elapsed_us = (time.monotonic_ns() - started) // 1000
        provisional, node_ids = self._trace(tree, accepted=accepted)
        receipt = (
            self._reuse_receipt(manifest or {}, base, node_ids)
            if base is not None else None
        )
        reused = (
            len(receipt["reused_nodes"]) if receipt is not None else 0)
        self.reused_nodes += reused
        telemetry = {
            "schema": "symcc-tree-sitter-incremental-telemetry-v1",
            "mode": mode,
            "elapsed_us": elapsed_us,
            "nodes": len(provisional["nodes"]),
            "reused_node_ids": reused,
            "cache_entries_before_store": len(self._cache),
        }
        trace, node_ids = self._trace(
            tree,
            accepted=accepted,
            receipt=receipt,
            telemetry=telemetry,
        )
        if accepted:
            self._store(_sha256(candidate), candidate, tree, node_ids)
        return trace

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema": "symcc-tree-sitter-incremental-snapshot-v1",
            "parser": self.parser_name,
            "requests": self.requests,
            "cold_parses": self.cold_parses,
            "incremental_parses": self.incremental_parses,
            "reused_nodes": self.reused_nodes,
            "cache_entries": len(self._cache),
            "max_trees": self.max_trees,
            "max_nodes": self.max_nodes,
            "max_input_bytes": self.max_input_bytes,
        }


def load_tree_sitter_engine(
    language_module: str,
    *,
    language_function: str = "language",
    parser_name: str = "",
    max_trees: int = 256,
    max_nodes: int = 4096,
    max_input_bytes: int = 16 * 1024 * 1024,
) -> IncrementalTreeSitterEngine:
    try:
        tree_sitter = importlib.import_module("tree_sitter")
        provider = importlib.import_module(language_module)
        factory = getattr(provider, language_function)
        language_raw = factory()
        language = (
            language_raw
            if isinstance(language_raw, tree_sitter.Language)
            else tree_sitter.Language(language_raw)
        )
        try:
            parser = tree_sitter.Parser(language)
        except TypeError:
            parser = tree_sitter.Parser()
            parser.language = language
    except (ImportError, AttributeError, TypeError, ValueError) as error:
        raise ParserFailure(
            "Tree-sitter language provider could not be loaded") from error
    label = parser_name or f"tree-sitter-{language_module}"
    return IncrementalTreeSitterEngine(
        parser,
        parser_name=label,
        max_trees=max_trees,
        max_nodes=max_nodes,
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
    engine: IncrementalTreeSitterEngine,
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
    cache_path = request.get("cache_path", "")
    if (
        not isinstance(input_path, str) or not input_path
        or not isinstance(trace_path, str) or not trace_path
        or not isinstance(cache_path, str)
    ):
        raise ParserFailure("RPC paths are invalid")
    try:
        candidate = Path(input_path).read_bytes()
    except OSError as error:
        raise ParserFailure("candidate input is unreadable") from error
    manifest = load_cache_manifest(cache_path) if cache_path else None
    trace = engine.parse(candidate, manifest)
    _atomic_write_json(trace_path, trace)
    return {
        "schema": RPC_SCHEMA,
        "ok": True,
        "accepted": bool(trace["accepted"]),
        "snapshot": engine.snapshot(),
    }, False


def serve(
    socket_path: str,
    engine: IncrementalTreeSitterEngine,
) -> int:
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
    serve_parser.add_argument("--language-module", required=True)
    serve_parser.add_argument("--language-function", default="language")
    serve_parser.add_argument("--parser-name", default="")
    serve_parser.add_argument("--max-trees", type=int, default=256)
    serve_parser.add_argument("--max-nodes", type=int, default=4096)
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
            engine = load_tree_sitter_engine(
                args.language_module,
                language_function=args.language_function,
                parser_name=args.parser_name,
                max_trees=args.max_trees,
                max_nodes=args.max_nodes,
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
                    "cache_path": args.cache,
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
        print(json.dumps(response, sort_keys=True))
        return 0 if response.get("ok") else 2
    except (OSError, ParserFailure, TimeoutError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
