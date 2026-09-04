# RUN: python3 %s

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest


UTIL = os.path.join(os.path.dirname(os.path.dirname(__file__)), "util")
sys.path.insert(0, UTIL)

from tree_sitter_incremental_parser import (  # noqa: E402
    IncrementalTreeSitterEngine,
    MANIFEST_SCHEMA,
    ParserFailure,
    RPC_SCHEMA,
    _recv_message,
    _send_message,
    load_cache_manifest,
    load_tree_sitter_engine,
)
from verified_proposals import VerifiedProposalManager  # noqa: E402


class _FakeNode:
    def __init__(
        self,
        symbol,
        start,
        end,
        node_id,
        children=(),
        *,
        has_error=False,
    ):
        self.type = symbol
        self.start_byte = start
        self.end_byte = end
        self.id = node_id
        self.children = tuple(children)
        self.grammar_id = 1
        self.parse_state = 2
        self.next_parse_state = 3
        self.has_error = has_error


class _FakeTree:
    def __init__(self, source, root):
        self.source = source
        self.root_node = root
        self.edit_arguments = None

    def copy(self):
        return _FakeTree(self.source, self.root_node)

    def edit(self, **arguments):
        self.edit_arguments = arguments


class _FakeParser:
    def __init__(self):
        self.calls = []
        self.next_id = 1000

    def parse(self, source, old_tree=None):
        source = bytes(source)
        self.calls.append((source, old_tree))
        self.next_id += 1
        old_ids = {}
        edit = None
        if old_tree is not None:
            edit = old_tree.edit_arguments
            for child in old_tree.root_node.children:
                old_ids[(child.start_byte, child.end_byte)] = child.id
        leaves = []
        for index in range(len(source)):
            node_id = self.next_id * 100 + index
            if edit is not None:
                start = edit["start_byte"]
                old_end = edit["old_end_byte"]
                new_end = edit["new_end_byte"]
                if index < start:
                    node_id = old_ids[(index, index + 1)]
                elif index >= new_end:
                    old_index = index - new_end + old_end
                    node_id = old_ids[(old_index, old_index + 1)]
            leaves.append(_FakeNode(
                chr(source[index]), index, index + 1, node_id))
        root = _FakeNode(
            "document",
            0,
            len(source),
            self.next_id,
            leaves,
            has_error=b"!" in source,
        )
        return _FakeTree(source, root)


def _manifest(base, candidate, *, digest="a" * 64):
    offset = 1
    raw = {
        "schema": MANIFEST_SCHEMA,
        "mode": "incremental",
        "candidate_input_sha256": hashlib.sha256(candidate).hexdigest(),
        "base_input_sha256": hashlib.sha256(base).hexdigest(),
        "edit": {
            "offset": offset,
            "delete": 1,
            "insert_hex": candidate[offset:offset + 1].hex(),
        },
        "reusable_nodes": [
            {"base_index": 1},
            {"base_index": 3},
        ],
        "invalidated_nodes": [0, 2],
        "manifest_sha256": digest,
    }
    return raw


class IncrementalTreeSitterEngineTests(unittest.TestCase):
    def test_native_edit_and_node_identity_receipt(self):
        parser = _FakeParser()
        engine = IncrementalTreeSitterEngine(
            parser, parser_name="fake-tree-sitter")
        base = b"abc"
        candidate = b"aXc"

        cold = engine.parse(base)
        self.assertTrue(cold["accepted"])
        self.assertNotIn("incremental_cache", cold)
        incremental = engine.parse(
            candidate, _manifest(base, candidate))

        receipt = incremental["incremental_cache"]
        self.assertEqual(receipt["manifest_sha256"], "a" * 64)
        self.assertEqual(receipt["reused_nodes"], [
            {"base_index": 1, "candidate_index": 1},
            {"base_index": 3, "candidate_index": 3},
        ])
        old_tree = parser.calls[-1][1]
        self.assertIsNotNone(old_tree)
        self.assertEqual(old_tree.edit_arguments, {
            "start_byte": 1,
            "old_end_byte": 2,
            "new_end_byte": 2,
            "start_point": (0, 1),
            "old_end_point": (0, 2),
            "new_end_point": (0, 2),
        })
        self.assertEqual(
            incremental["incremental_telemetry"]["mode"], "incremental")
        self.assertEqual(engine.snapshot()["reused_nodes"], 2)

    def test_invalid_edit_falls_back_to_cold_parse(self):
        parser = _FakeParser()
        engine = IncrementalTreeSitterEngine(
            parser, parser_name="fake-tree-sitter")
        base = b"abc"
        candidate = b"aXc"
        engine.parse(base)
        manifest = _manifest(base, candidate)
        manifest["edit"]["insert_hex"] = "59"

        trace = engine.parse(candidate, manifest)

        self.assertNotIn("incremental_cache", trace)
        self.assertIsNone(parser.calls[-1][1])
        self.assertEqual(engine.snapshot()["cold_parses"], 2)

    def test_rejected_tree_is_not_cached(self):
        engine = IncrementalTreeSitterEngine(
            _FakeParser(), parser_name="fake-tree-sitter")
        trace = engine.parse(b"!")
        self.assertFalse(trace["accepted"])
        self.assertEqual(engine.cache_entries, 0)

    def test_node_bound_fails_closed(self):
        engine = IncrementalTreeSitterEngine(
            _FakeParser(), parser_name="fake-tree-sitter", max_nodes=2)
        with self.assertRaises(ParserFailure):
            engine.parse(b"abc")

    def test_cache_eviction_is_bounded(self):
        engine = IncrementalTreeSitterEngine(
            _FakeParser(), parser_name="fake-tree-sitter", max_trees=2)
        for content in (b"a", b"b", b"c"):
            engine.parse(content)
        self.assertEqual(engine.cache_entries, 2)
        self.assertNotIn(hashlib.sha256(b"a").hexdigest(), engine._cache)


class IncrementalTreeSitterProtocolTests(unittest.TestCase):
    def test_manifest_digest_is_derived_from_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "cache.json")
            raw = {
                "schema": MANIFEST_SCHEMA,
                "mode": "cold",
                "candidate_input_sha256": "0" * 64,
                "reusable_nodes": [],
                "invalidated_nodes": [],
            }
            encoded = json.dumps(
                raw, sort_keys=True, separators=(",", ":")).encode()
            path.write_bytes(encoded)

            manifest = load_cache_manifest(str(path))

            self.assertEqual(
                manifest["manifest_sha256"],
                hashlib.sha256(encoded).hexdigest(),
            )

    def test_bounded_rpc_frame_round_trip(self):
        left, right = socket.socketpair()
        try:
            payload = {
                "schema": RPC_SCHEMA,
                "command": "ping",
            }
            _send_message(left, payload)
            self.assertEqual(_recv_message(right), payload)
        finally:
            left.close()
            right.close()


@unittest.skipUnless(
    importlib.util.find_spec("tree_sitter")
    and importlib.util.find_spec("tree_sitter_json"),
    "official tree-sitter and tree-sitter-json wheels are not installed",
)
class RealTreeSitterIntegrationTests(unittest.TestCase):
    def test_json_incremental_parse_reuses_native_node_identity(self):
        engine = load_tree_sitter_engine(
            "tree_sitter_json",
            parser_name="tree-sitter-json-real",
        )
        base = b'{"stable":[1,2,3],"changed":10}'
        candidate = b'{"stable":[1,2,3],"changed":11}'
        offset = base.rindex(b"10")
        cold = engine.parse(base)
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "mode": "incremental",
            "candidate_input_sha256": hashlib.sha256(candidate).hexdigest(),
            "base_input_sha256": hashlib.sha256(base).hexdigest(),
            "edit": {
                "offset": offset,
                "delete": 2,
                "insert_hex": b"11".hex(),
            },
            "reusable_nodes": [
                {"base_index": index}
                for index in range(len(cold["nodes"]))
            ],
            "invalidated_nodes": [],
            "manifest_sha256": "b" * 64,
        }

        trace = engine.parse(candidate, manifest)

        self.assertTrue(trace["accepted"])
        self.assertEqual(
            trace["incremental_telemetry"]["mode"], "incremental")
        self.assertGreater(
            len(trace["incremental_cache"]["reused_nodes"]), 0)

    def test_unix_service_cli_emits_verified_incremental_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            socket_path = os.path.join(tmp, "parser.sock")
            script = os.path.join(
                UTIL, "tree_sitter_incremental_parser.py")
            server = subprocess.Popen(
                [
                    sys.executable,
                    script,
                    "serve",
                    "--socket",
                    socket_path,
                    "--language-module",
                    "tree_sitter_json",
                    "--parser-name",
                    "tree-sitter-json-service",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 5.0
                while not os.path.exists(socket_path):
                    if server.poll() is not None:
                        self.fail(server.stderr.read())
                    if time.monotonic() >= deadline:
                        self.fail("Tree-sitter service did not create socket")
                    time.sleep(0.01)

                base = b'{"stable":[1,2,3],"changed":10}'
                candidate = b'{"stable":[1,2,3],"changed":11}'
                base_path = Path(tmp, "base.json")
                base_trace_path = Path(tmp, "base.trace.json")
                base_manifest_path = Path(tmp, "base.cache.json")
                base_path.write_bytes(base)
                base_manifest_path.write_text(json.dumps({
                    "schema": MANIFEST_SCHEMA,
                    "mode": "cold",
                    "candidate_input_sha256": hashlib.sha256(
                        base).hexdigest(),
                    "reusable_nodes": [],
                    "invalidated_nodes": [],
                }))
                cold = subprocess.run(
                    [
                        sys.executable,
                        script,
                        "parse",
                        "--socket",
                        socket_path,
                        "--input",
                        str(base_path),
                        "--trace",
                        str(base_trace_path),
                        "--cache",
                        str(base_manifest_path),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5.0,
                )
                self.assertEqual(cold.returncode, 0, cold.stderr)
                base_trace = json.loads(base_trace_path.read_text())

                candidate_path = Path(tmp, "candidate.json")
                trace_path = Path(tmp, "candidate.trace.json")
                manifest_path = Path(tmp, "candidate.cache.json")
                candidate_path.write_bytes(candidate)
                offset = base.rindex(b"10")
                manifest_path.write_text(json.dumps({
                    "schema": MANIFEST_SCHEMA,
                    "mode": "incremental",
                    "candidate_input_sha256": hashlib.sha256(
                        candidate).hexdigest(),
                    "base_input_sha256": hashlib.sha256(base).hexdigest(),
                    "edit": {
                        "offset": offset,
                        "delete": 2,
                        "insert_hex": b"11".hex(),
                    },
                    "reusable_nodes": [
                        {"base_index": index}
                        for index in range(len(base_trace["nodes"]))
                    ],
                    "invalidated_nodes": [],
                }))
                incremental = subprocess.run(
                    [
                        sys.executable,
                        script,
                        "parse",
                        "--socket",
                        socket_path,
                        "--input",
                        str(candidate_path),
                        "--trace",
                        str(trace_path),
                        "--cache",
                        str(manifest_path),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5.0,
                )
                self.assertEqual(
                    incremental.returncode, 0, incremental.stderr)
                trace = json.loads(trace_path.read_text())
                self.assertTrue(trace["accepted"])
                self.assertGreater(
                    len(trace["incremental_cache"]["reused_nodes"]), 0)
                self.assertEqual(
                    trace["incremental_telemetry"]["mode"], "incremental")
            finally:
                if server.poll() is None:
                    subprocess.run(
                        [
                            sys.executable,
                            script,
                            "shutdown",
                            "--socket",
                            socket_path,
                        ],
                        check=False,
                        capture_output=True,
                        timeout=5.0,
                    )
                server.wait(timeout=5.0)
                if server.stderr is not None:
                    server.stderr.close()

    def test_verified_proposal_manager_accepts_native_reuse_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            socket_path = os.path.join(tmp, "manager-parser.sock")
            script = os.path.join(
                UTIL, "tree_sitter_incremental_parser.py")
            server = subprocess.Popen(
                [
                    sys.executable,
                    script,
                    "serve",
                    "--socket",
                    socket_path,
                    "--language-module",
                    "tree_sitter_json",
                    "--parser-name",
                    "tree-sitter-json-manager",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 5.0
                while not os.path.exists(socket_path):
                    if server.poll() is not None:
                        self.fail(server.stderr.read())
                    if time.monotonic() >= deadline:
                        self.fail("Tree-sitter service did not create socket")
                    time.sleep(0.01)

                manager = VerifiedProposalManager(
                    "",
                    os.path.join(tmp, "manager"),
                    parser_command=(
                        sys.executable,
                        script,
                        "parse",
                        "--socket",
                        socket_path,
                        "--input",
                        "{input}",
                        "--trace",
                        "{trace}",
                        "--cache",
                        "{cache}",
                    ),
                    parser_timeout=5.0,
                )
                base = (
                    b'{"prefix":[0,1,2,3,4,5,6,7,8,9],'
                    b'"changed":10,'
                    b'"suffix":[9,8,7,6,5,4,3,2,1,0]}'
                )
                candidate = base.replace(b'"changed":10', b'"changed":11')
                base_id = manager.ingest({
                    "kind": "semantic",
                    "candidate": {"hex": base.hex()},
                    "target_branch": 0,
                })
                self.assertIsNotNone(base_id)
                assert base_id is not None
                self.assertTrue(manager.validate(
                    base_id, None, retcode=0, killed=False))
                base_record = manager.records[base_id]

                candidate_id = manager.ingest({
                    "kind": "semantic",
                    "source_path": base_record.candidate_path,
                    "candidate": {"hex": candidate.hex()},
                    "target_branch": 0,
                })
                self.assertIsNotNone(candidate_id)
                assert candidate_id is not None
                self.assertTrue(manager.validate(
                    candidate_id, None, retcode=0, killed=False))
                record = manager.records[candidate_id]
                self.assertTrue(record.parser_cache_hit)
                self.assertGreater(record.parser_cache_reused_nodes, 0)
                self.assertGreater(record.parser_cache_invalidated_nodes, 0)
                self.assertEqual(record.last_reason, "parser-accepted")
                snapshot = manager.snapshot()
                self.assertEqual(snapshot["schema"], 15)
                self.assertEqual(snapshot["parser_cache_hits"], 1)
                self.assertEqual(
                    snapshot["parser_cache_incremental_offers"], 1)
                self.assertEqual(
                    snapshot["parser_incremental_receipts"], 1)
                self.assertEqual(snapshot["parser_node_id_proofs"], 1)
                self.assertGreater(snapshot["parser_wall_time_us"], 0)
                self.assertGreater(
                    snapshot["parser_reported_parse_time_us"], 0)
                self.assertGreater(snapshot["parser_trace_bytes"], 0)

                artifact = manager.research_artifact({
                    "experiment_id": "f228-test",
                    "run_id": "run-0",
                    "configuration": "parser-incremental",
                })
                self.assertTrue(
                    manager.verify_research_artifact(artifact))
                artifact_path = os.path.join(
                    tmp, "parser_research_artifact.json")
                self.assertTrue(manager.write_research_artifact(
                    artifact_path,
                    {
                        "experiment_id": "f228-test",
                        "run_id": "run-0",
                        "configuration": "parser-incremental",
                    },
                ))
                self.assertTrue(manager.verify_research_artifact(
                    json.loads(Path(artifact_path).read_text())))
                artifact["metrics"][
                    "proposal_parser_node_id_proofs"] += 1
                self.assertFalse(
                    manager.verify_research_artifact(artifact))

                manager.save()
                restored = VerifiedProposalManager(
                    "",
                    os.path.join(tmp, "manager"),
                    parser_command=manager.parser_command,
                    parser_timeout=5.0,
                )
                self.assertEqual(
                    restored.snapshot()["parser_node_id_proofs"], 1)
                state_path = Path(manager.state_path)
                state = json.loads(state_path.read_text())
                target_record = next(
                    item for item in state["records"]
                    if item["proposal_id"] == candidate_id
                )
                target_record["parser_node_id_proofs"] = 2
                state_path.write_text(json.dumps(state))
                corrupted = VerifiedProposalManager(
                    "",
                    os.path.join(tmp, "manager"),
                    parser_command=manager.parser_command,
                    parser_timeout=5.0,
                )
                self.assertNotIn(candidate_id, corrupted.records)
            finally:
                if server.poll() is None:
                    subprocess.run(
                        [
                            sys.executable,
                            script,
                            "shutdown",
                            "--socket",
                            socket_path,
                        ],
                        check=False,
                        capture_output=True,
                        timeout=5.0,
                    )
                server.wait(timeout=5.0)
                if server.stderr is not None:
                    server.stderr.close()


if __name__ == "__main__":
    unittest.main()
