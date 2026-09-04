# RUN: python3 %s

from collections import Counter
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


UTIL = os.path.join(os.path.dirname(os.path.dirname(__file__)), "util")
sys.path.insert(0, UTIL)

from lark_sppf_parser import (  # noqa: E402
    FOREST_PROOF,
    ParserFailure,
    TELEMETRY_SCHEMA,
    load_lark_engine,
    rpc_request,
)
from verified_proposals import VerifiedProposalManager  # noqa: E402


AMBIGUOUS_GRAMMAR = """\
start: expr
?expr: expr expr -> pair
     | "a"       -> atom
"""


def _write_grammar(directory, grammar=AMBIGUOUS_GRAMMAR):
    path = Path(directory, "grammar.lark")
    path.write_text(grammar)
    return str(path)


def _assert_manager_accepts_trace(test, directory, trace, candidate, retcode):
    trace_path = Path(directory, "trace.json")
    trace_path.write_text(json.dumps(trace))
    manager = VerifiedProposalManager(
        "", os.path.join(directory, "proposal-state"))
    result = manager._load_parser_trace(
        str(trace_path),
        candidate_size=len(candidate),
        returncode=retcode,
        candidate=candidate,
    )
    test.assertIsNotNone(result)


class LarkSPPFEngineTests(unittest.TestCase):
    def test_complete_ambiguous_forest_preserves_sharing(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = load_lark_engine(_write_grammar(tmp))

            trace = engine.parse(b"aaa")

            self.assertTrue(trace["accepted"])
            self.assertEqual(trace["roots"], [0])
            telemetry = trace["forest_telemetry"]
            self.assertEqual(telemetry["schema"], TELEMETRY_SCHEMA)
            self.assertEqual(telemetry["proof"], FOREST_PROOF)
            self.assertEqual(
                telemetry["grammar_sha256"], engine.grammar_sha256)
            self.assertEqual(telemetry["lark_version"], "1.3.1")
            self.assertTrue(telemetry["complete"])
            self.assertEqual(telemetry["raw_nodes"], len(trace["nodes"]))
            self.assertTrue(any(
                node["symbol"] == "expr"
                and len(node["alternatives"]) == 2
                for node in trace["nodes"]
            ))
            incoming = Counter(
                child
                for node in trace["nodes"]
                for alternative in node["alternatives"]
                for child in alternative
            )
            self.assertTrue(any(count > 1 for count in incoming.values()))
            _assert_manager_accepts_trace(
                self, tmp, trace, b"aaa", retcode=0)

    def test_epsilon_subforest_becomes_v4_nullable_certificate(self):
        with tempfile.TemporaryDirectory() as tmp:
            grammar = _write_grammar(tmp, "start: empty empty\nempty:\n")
            trace = load_lark_engine(grammar).parse(b"")

            self.assertEqual(
                trace["schema"], "symcc-parser-structural-trace-v4")
            self.assertGreater(len(trace["nullable_rules"]), 1)
            self.assertEqual(
                trace["forest_telemetry"]["nullable_rules"],
                len(trace["nullable_rules"]),
            )
            primary_parents = Counter()
            primary = {0}
            for index, node in enumerate(trace["nodes"]):
                if index not in primary:
                    continue
                for child in node["alternatives"][0]:
                    primary.add(child)
                    primary_parents[child] += 1
            self.assertTrue(all(
                count == 1 for count in primary_parents.values()))
            _assert_manager_accepts_trace(
                self, tmp, trace, b"", retcode=0)

    def test_syntax_rejection_emits_valid_negative_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            grammar = _write_grammar(tmp, 'start: "a"\n')
            trace = load_lark_engine(grammar).parse(b"b")

            self.assertFalse(trace["accepted"])
            self.assertFalse(trace["forest_telemetry"]["complete"])
            self.assertEqual(trace["nodes"][0]["symbol"], "@parse-error")
            _assert_manager_accepts_trace(
                self, tmp, trace, b"b", retcode=1)

    def test_node_bound_fails_closed_without_partial_forest(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = load_lark_engine(
                _write_grammar(tmp), max_nodes=8)
            with self.assertRaises(ParserFailure):
                engine.parse(b"aaa")
            self.assertEqual(engine.snapshot()["failures"], 1)

    def test_cyclic_infinite_ambiguity_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = load_lark_engine(
                _write_grammar(tmp, "start: start |\n"))
            with self.assertRaisesRegex(
                    ParserFailure, "cyclic SPPF"):
                engine.parse(b"")

    def test_invalid_grammar_is_reported_as_parser_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            grammar = _write_grammar(tmp, "start: [\n")
            with self.assertRaisesRegex(
                    ParserFailure, "could not be loaded"):
                load_lark_engine(grammar)

    def test_inconsistent_forest_telemetry_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace = load_lark_engine(
                _write_grammar(tmp)).parse(b"aaa")
            trace["forest_telemetry"]["edges"] += 1
            trace_path = Path(tmp, "bad-trace.json")
            trace_path.write_text(json.dumps(trace))
            manager = VerifiedProposalManager(
                "", os.path.join(tmp, "manager"))
            self.assertIsNone(manager._load_parser_trace(
                str(trace_path),
                candidate_size=3,
                returncode=0,
                candidate=b"aaa",
            ))

    def test_byte_mode_preserves_non_ascii_offsets(self):
        with tempfile.TemporaryDirectory() as tmp:
            grammar = _write_grammar(
                tmp, 'start: UTF8\nUTF8: /[\\x80-\\xff]+/\n')
            trace = load_lark_engine(grammar).parse(b"\xc3\xa9")

            terminal = next(
                node for node in trace["nodes"]
                if node["symbol"] == "UTF8")
            self.assertEqual((terminal["start"], terminal["end"]), (0, 2))
            _assert_manager_accepts_trace(
                self, tmp, trace, b"\xc3\xa9", retcode=0)


class LarkSPPFServiceTests(unittest.TestCase):
    def test_persistent_rpc_and_verified_manager(self):
        with tempfile.TemporaryDirectory() as tmp:
            grammar = _write_grammar(tmp)
            socket_path = os.path.join(tmp, "lark.sock")
            script = os.path.join(UTIL, "lark_sppf_parser.py")
            server = subprocess.Popen(
                [
                    sys.executable,
                    script,
                    "serve",
                    "--socket",
                    socket_path,
                    "--grammar",
                    grammar,
                    "--parser-name",
                    "lark-sppf-test",
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
                        self.fail("Lark SPPF service did not create socket")
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
                    ),
                    parser_timeout=5.0,
                )
                proposal_id = manager.ingest({
                    "kind": "semantic",
                    "candidate": {"text": "aaa"},
                    "target_branch": 0,
                })
                self.assertIsNotNone(proposal_id)
                assert proposal_id is not None
                self.assertTrue(manager.validate(
                    proposal_id, None, retcode=0, killed=False))
                record = manager.records[proposal_id]
                self.assertEqual(record.last_reason, "parser-accepted")
                self.assertGreater(record.parser_trace_nodes, 1)
                snapshot = manager.snapshot()
                self.assertEqual(snapshot["schema"], 15)
                self.assertEqual(snapshot["parser_forest_traces"], 1)
                self.assertEqual(
                    snapshot["parser_forest_complete_traces"], 1)
                self.assertEqual(snapshot["parser_forest_proofs"], 1)
                self.assertGreater(
                    snapshot["parser_forest_raw_nodes"], 1)
                self.assertEqual(
                    snapshot["parser_forest_encoded_nodes"],
                    record.parser_trace_nodes,
                )
                self.assertGreater(
                    snapshot["parser_forest_packed_alternatives"], 1)
                self.assertGreater(snapshot["parser_forest_edges"], 1)
                self.assertEqual(
                    snapshot["parser_forest_grammars"], 1)
                self.assertGreaterEqual(
                    snapshot["parser_forest_parse_time_us"], 0)
                artifact = manager.research_artifact({
                    "experiment_id": "f230-test",
                    "run_id": "run-0",
                    "configuration": "complete-sppf",
                })
                self.assertTrue(
                    manager.verify_research_artifact(artifact))
                self.assertEqual(
                    artifact["parser_forest_grammar_sha256s"],
                    [load_lark_engine(grammar).grammar_sha256],
                )
                self.assertEqual(
                    len(artifact["parser_command_sha256"]), 64)
                artifact["metrics"][
                    "proposal_parser_forest_proofs"] = 2
                self.assertFalse(
                    manager.verify_research_artifact(artifact))

                service_snapshot = rpc_request(
                    socket_path, {"command": "ping"})["snapshot"]
                self.assertEqual(service_snapshot["requests"], 1)
                self.assertEqual(service_snapshot["accepted"], 1)
                self.assertEqual(service_snapshot["grammar_sha256"], (
                    load_lark_engine(grammar).grammar_sha256))

                manager.save()
                restored = VerifiedProposalManager(
                    "",
                    os.path.join(tmp, "manager"),
                    parser_command=manager.parser_command,
                    parser_timeout=5.0,
                )
                self.assertEqual(
                    restored.snapshot()["parser_forest_proofs"], 1)
                state_path = Path(manager.state_path)
                state = json.loads(state_path.read_text())
                state["records"][0]["parser_forest_proofs"] = 2
                state_path.write_text(json.dumps(state))
                corrupted = VerifiedProposalManager(
                    "",
                    os.path.join(tmp, "manager"),
                    parser_command=manager.parser_command,
                    parser_timeout=5.0,
                )
                self.assertNotIn(proposal_id, corrupted.records)
                manager.save()
                state = json.loads(state_path.read_text())
                state["parser_forest_grammar_sha256s"] = []
                state_path.write_text(json.dumps(state))
                missing_identity = VerifiedProposalManager(
                    "",
                    os.path.join(tmp, "manager"),
                    parser_command=manager.parser_command,
                    parser_timeout=5.0,
                )
                self.assertNotIn(proposal_id, missing_identity.records)
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
