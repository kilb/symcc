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

from lark_sppf_parser import ParserFailure  # noqa: E402
from parser_equivalence_audit import audit  # noqa: E402
from parglare_sppf_parser import (  # noqa: E402
    FOREST_PROOF,
    TELEMETRY_SCHEMA,
    load_parglare_engine,
)
from verified_proposals import VerifiedProposalManager  # noqa: E402


AMBIGUOUS_GRAMMAR = "S: S S | 'a';"


def _write_grammar(directory, grammar=AMBIGUOUS_GRAMMAR, name="grammar.pg"):
    path = Path(directory, name)
    path.write_text(grammar, encoding="utf-8")
    return str(path)


def _assert_manager_accepts(test, directory, trace, candidate, returncode):
    path = Path(directory, "trace.json")
    path.write_text(json.dumps(trace), encoding="utf-8")
    manager = VerifiedProposalManager(
        "", os.path.join(directory, "manager-check"))
    result = manager._load_parser_trace(
        str(path),
        candidate_size=len(candidate),
        returncode=returncode,
        candidate=candidate,
    )
    test.assertIsNotNone(result)


def _start_service(script, socket_path, grammar, parser_name):
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
            parser_name,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 5.0
    while not os.path.exists(socket_path):
        if server.poll() is not None:
            raise AssertionError(server.stderr.read())
        if time.monotonic() >= deadline:
            raise AssertionError("parser service did not create its socket")
        time.sleep(0.01)
    return server


def _stop_service(server, script, socket_path):
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


class ParglareSPPFEngineTests(unittest.TestCase):
    def test_glr_forest_preserves_ambiguity_and_sharing(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = load_parglare_engine(_write_grammar(tmp))
            trace = engine.parse(b"aaa")

            self.assertTrue(trace["accepted"])
            telemetry = trace["forest_telemetry"]
            self.assertEqual(telemetry["schema"], TELEMETRY_SCHEMA)
            self.assertEqual(telemetry["proof"], FOREST_PROOF)
            self.assertEqual(telemetry["parglare_version"], "0.21.1")
            self.assertTrue(telemetry["complete"])
            self.assertTrue(any(
                node["symbol"] == "S" and
                len(node["alternatives"]) == 2
                for node in trace["nodes"]
            ))
            incoming = Counter(
                child
                for node in trace["nodes"]
                for alternative in node["alternatives"]
                for child in alternative
            )
            self.assertTrue(any(count > 1 for count in incoming.values()))
            _assert_manager_accepts(self, tmp, trace, b"aaa", 0)

    def test_nullable_forest_uses_v4_certificate(self):
        with tempfile.TemporaryDirectory() as tmp:
            grammar = _write_grammar(
                tmp, "S: E E; E: EMPTY;")
            trace = load_parglare_engine(grammar).parse(b"")

            self.assertEqual(
                trace["schema"], "symcc-parser-structural-trace-v4")
            self.assertGreater(len(trace["nullable_rules"]), 1)
            self.assertEqual(
                trace["forest_telemetry"]["nullable_rules"],
                len(trace["nullable_rules"]),
            )
            _assert_manager_accepts(self, tmp, trace, b"", 0)

    def test_syntax_rejection_is_a_proof_carrying_negative_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = load_parglare_engine(_write_grammar(tmp, "S: 'a';"))
            trace = engine.parse(b"b")

            self.assertFalse(trace["accepted"])
            self.assertFalse(trace["forest_telemetry"]["complete"])
            _assert_manager_accepts(self, tmp, trace, b"b", 1)
            empty = engine.parse(b"")
            self.assertFalse(empty["accepted"])
            self.assertTrue(empty["nodes"][0]["epsilon"])
            _assert_manager_accepts(self, tmp, empty, b"", 1)

    def test_cycle_and_node_bound_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            cycle = load_parglare_engine(
                _write_grammar(tmp, "S: S | EMPTY;", "cycle.pg"))
            with self.assertRaisesRegex(ParserFailure, "cyclic SPPF"):
                cycle.parse(b"")
            bounded = load_parglare_engine(
                _write_grammar(tmp, name="bounded.pg"),
                max_nodes=8,
            )
            with self.assertRaisesRegex(ParserFailure, "node bound"):
                bounded.parse(b"aaa")

    def test_invalid_grammar_and_telemetry_tamper_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(
                    ParserFailure, "could not be loaded"):
                load_parglare_engine(
                    _write_grammar(tmp, "S: [;", "invalid.pg"))
            trace = load_parglare_engine(
                _write_grammar(tmp, name="valid.pg")).parse(b"aaa")
            trace["forest_telemetry"]["proof"] = "forged"
            path = Path(tmp, "tampered.json")
            path.write_text(json.dumps(trace), encoding="utf-8")
            manager = VerifiedProposalManager(
                "", os.path.join(tmp, "tamper-manager"))
            self.assertIsNone(manager._load_parser_trace(
                str(path),
                candidate_size=3,
                returncode=0,
                candidate=b"aaa",
            ))


class ParglareSPPFServiceTests(unittest.TestCase):
    def test_persistent_service_and_verified_manager(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = os.path.join(UTIL, "parglare_sppf_parser.py")
            socket_path = os.path.join(tmp, "parglare.sock")
            server = _start_service(
                script,
                socket_path,
                _write_grammar(tmp),
                "parglare-service-test",
            )
            try:
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
                self.assertTrue(manager.validate(
                    str(proposal_id), None, retcode=0, killed=False))
                snapshot = manager.snapshot()
                self.assertEqual(snapshot["parser_forest_traces"], 1)
                self.assertEqual(
                    snapshot["parser_forest_complete_traces"], 1)
                self.assertTrue(manager.verify_research_artifact(
                    manager.research_artifact()))
            finally:
                _stop_service(server, script, socket_path)

    def test_earley_and_glr_are_candidate_paired_differential_oracles(self):
        with tempfile.TemporaryDirectory() as tmp:
            lark_script = os.path.join(UTIL, "lark_sppf_parser.py")
            parglare_script = os.path.join(
                UTIL, "parglare_sppf_parser.py")
            cross_script = os.path.join(UTIL, "cross_parser_oracle.py")
            lark_grammar = Path(tmp, "grammar.lark")
            lark_grammar.write_text(
                'start: expr\n?expr: expr expr | "a"\n',
                encoding="utf-8",
            )
            parglare_grammar = _write_grammar(tmp)
            lark_socket = os.path.join(tmp, "lark.sock")
            parglare_socket = os.path.join(tmp, "parglare.sock")
            lark_server = _start_service(
                lark_script,
                lark_socket,
                str(lark_grammar),
                "earley-primary",
            )
            parglare_server = _start_service(
                parglare_script,
                parglare_socket,
                parglare_grammar,
                "glr-secondary",
            )
            try:
                primary = " ".join((
                    sys.executable,
                    lark_script,
                    "parse",
                    "--socket",
                    lark_socket,
                    "--input",
                    "{input}",
                    "--trace",
                    "{trace}",
                ))
                secondary = " ".join((
                    sys.executable,
                    parglare_script,
                    "parse",
                    "--socket",
                    parglare_socket,
                    "--input",
                    "{input}",
                    "--trace",
                    "{trace}",
                ))
                manager = VerifiedProposalManager(
                    "",
                    os.path.join(tmp, "cross-manager"),
                    parser_command=(
                        sys.executable,
                        cross_script,
                        "--input",
                        "{input}",
                        "--trace",
                        "{trace}",
                        "--primary-command",
                        primary,
                        "--secondary-command",
                        secondary,
                    ),
                    parser_timeout=10.0,
                )
                proposal_id = manager.ingest({
                    "kind": "semantic",
                    "candidate": {"text": "aaa"},
                    "target_branch": 0,
                })
                self.assertIsNotNone(proposal_id)
                self.assertTrue(manager.validate(
                    str(proposal_id), None, retcode=0, killed=False))
                snapshot = manager.snapshot()
                self.assertEqual(snapshot["parser_cross_both_accept"], 1)
                self.assertEqual(snapshot["parser_cross_structural_pairs"], 1)
                self.assertGreater(
                    snapshot["parser_cross_forest_shared_spans"], 0)
                self.assertGreater(
                    snapshot["parser_cross_symbol_correspondences"], 0)
                self.assertGreater(
                    snapshot[
                        "parser_cross_production_correspondences"], 0)
                artifact = manager.research_artifact()
                self.assertTrue(manager.verify_research_artifact(artifact))
                self.assertTrue(
                    artifact["parser_cross_symbol_correspondence"])
                self.assertTrue(
                    artifact["parser_cross_production_correspondence"])
                bounded = audit(
                    primary_command=primary,
                    secondary_command=secondary,
                    alphabet_hex="61",
                    max_length=3,
                    timeout=5.0,
                )
                self.assertTrue(bounded["equivalent"])
                self.assertEqual(bounded["cases"], 4)
                self.assertEqual(bounded["both_accept"], 3)
                self.assertEqual(bounded["both_reject"], 1)
            finally:
                _stop_service(
                    parglare_server, parglare_script, parglare_socket)
                _stop_service(lark_server, lark_script, lark_socket)


if __name__ == "__main__":
    unittest.main()
