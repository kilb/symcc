# RUN: python3 %s

import hashlib
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

from cross_parser_oracle import (  # noqa: E402
    CROSS_PROOF,
    CROSS_SCHEMA,
    compare,
)
from verified_proposals import VerifiedProposalManager  # noqa: E402


def _write_selected_parser(directory):
    path = Path(directory, "selected_parser.py")
    path.write_text(
        """\
import json
from pathlib import Path
import sys

input_path, trace_path, policy = sys.argv[1:4]
data = Path(input_path).read_bytes()
accepted = policy == "accept" or (
    policy == "a-only" and data and set(data) == {ord("a")})
nodes = [{
    "symbol": "root",
    "state": "selected",
    "start": 0,
    "end": len(data),
    "parent": -1,
    "epsilon": not data,
}]
nodes.extend({
    "symbol": "byte",
    "state": "token",
    "start": offset,
    "end": offset + 1,
    "parent": 0,
    "epsilon": False,
} for offset in range(len(data)))
trace = {
    "schema": "symcc-parser-structural-trace-v2",
    "parser": "selected-fixture-v1",
    "accepted": accepted,
    "nodes": nodes,
}
Path(trace_path).write_text(json.dumps(trace))
raise SystemExit(0 if accepted else 1)
""",
        encoding="utf-8",
    )
    return str(path)


def _start_lark(directory):
    grammar = Path(directory, "grammar.lark")
    grammar.write_text('start: "a"+\n', encoding="utf-8")
    socket_path = os.path.join(directory, "lark.sock")
    script = os.path.join(UTIL, "lark_sppf_parser.py")
    server = subprocess.Popen(
        [
            sys.executable,
            script,
            "serve",
            "--socket",
            socket_path,
            "--grammar",
            str(grammar),
            "--parser-name",
            "complete-fixture-v1",
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
            raise AssertionError("Lark service did not create its socket")
        time.sleep(0.01)
    return server, socket_path, script, str(grammar)


def _stop_lark(server, socket_path, script):
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


class CrossParserOracleTests(unittest.TestCase):
    def test_pair_envelope_and_manager_confusion_matrix(self):
        with tempfile.TemporaryDirectory() as tmp:
            server, socket_path, lark_script, grammar = _start_lark(tmp)
            selected_script = _write_selected_parser(tmp)
            try:
                primary_command = " ".join((
                    sys.executable,
                    lark_script,
                    "parse",
                    "--socket",
                    socket_path,
                    "--input",
                    "{input}",
                    "--trace",
                    "{trace}",
                ))
                secondary_command = " ".join((
                    sys.executable,
                    selected_script,
                    "{input}",
                    "{trace}",
                    "accept",
                ))
                cross_script = os.path.join(
                    UTIL, "cross_parser_oracle.py")
                command = (
                    sys.executable,
                    cross_script,
                    "--input",
                    "{input}",
                    "--trace",
                    "{trace}",
                    "--primary-command",
                    primary_command,
                    "--secondary-command",
                    secondary_command,
                    "--timeout",
                    "5",
                )
                manager_root = os.path.join(tmp, "manager")
                manager = VerifiedProposalManager(
                    "",
                    manager_root,
                    parser_command=command,
                    parser_timeout=10.0,
                )
                accepted_id = manager.ingest({
                    "kind": "semantic",
                    "candidate": {"text": "aaa"},
                    "target_branch": 0,
                })
                self.assertIsNotNone(accepted_id)
                self.assertTrue(manager.validate(
                    str(accepted_id), None, retcode=0, killed=False))

                rejected_id = manager.ingest({
                    "kind": "semantic",
                    "candidate": {"text": "bbb"},
                    "target_branch": 0,
                })
                self.assertIsNotNone(rejected_id)
                self.assertFalse(manager.validate(
                    str(rejected_id), None, retcode=0, killed=False))

                snapshot = manager.snapshot()
                self.assertEqual(snapshot["schema"], 15)
                self.assertEqual(snapshot["parser_cross_traces"], 2)
                self.assertEqual(snapshot["parser_cross_agreements"], 1)
                self.assertEqual(snapshot["parser_cross_both_accept"], 1)
                self.assertEqual(snapshot["parser_cross_primary_only"], 0)
                self.assertEqual(
                    snapshot["parser_cross_secondary_only"], 1)
                self.assertEqual(snapshot["parser_cross_both_reject"], 0)
                self.assertEqual(snapshot["parser_cross_command_pairs"], 1)
                self.assertEqual(
                    snapshot["parser_cross_structural_pairs"], 1)
                self.assertGreater(
                    snapshot["parser_cross_primary_selected_spans"], 0)
                self.assertGreaterEqual(
                    snapshot["parser_cross_primary_forest_spans"],
                    snapshot["parser_cross_primary_selected_spans"],
                )
                self.assertGreater(
                    snapshot["parser_cross_selected_shared_spans"], 0)
                self.assertGreaterEqual(
                    snapshot["parser_cross_forest_shared_spans"],
                    snapshot["parser_cross_selected_shared_spans"],
                )
                self.assertEqual(
                    snapshot["parser_cross_selected_union_spans"],
                    snapshot["parser_cross_primary_selected_spans"] +
                    snapshot["parser_cross_secondary_spans"] -
                    snapshot["parser_cross_selected_shared_spans"],
                )
                self.assertGreater(
                    snapshot["parser_cross_symbol_correspondences"], 0)
                self.assertLessEqual(
                    snapshot["parser_cross_symbol_correspondences"],
                    snapshot["parser_cross_primary_symbols"],
                )
                self.assertLessEqual(
                    snapshot["parser_cross_symbol_correspondences"],
                    snapshot["parser_cross_secondary_symbols"],
                )
                self.assertGreater(
                    snapshot["parser_cross_symbol_mappings"], 0)
                self.assertEqual(snapshot["parser_forest_traces"], 2)
                self.assertGreater(
                    snapshot["parser_cross_primary_time_us"], 0)
                self.assertGreater(
                    snapshot["parser_cross_secondary_time_us"], 0)
                artifact = manager.research_artifact({
                    "experiment_id": "f231-test",
                    "parser_forest_mode": "complete",
                    "parser_cross_mode": "paired",
                })
                self.assertTrue(
                    manager.verify_research_artifact(artifact))
                self.assertEqual(
                    len(artifact["parser_cross_command_pairs"]), 1)
                self.assertEqual(
                    len(artifact[
                        "parser_cross_symbol_correspondence"]),
                    snapshot["parser_cross_symbol_mappings"],
                )
                tampered = json.loads(json.dumps(artifact))
                tampered[
                    "parser_cross_symbol_correspondence"][0][
                        "observations"] += 1
                core = dict(tampered)
                core.pop("artifact_sha256")
                tampered["artifact_sha256"] = hashlib.sha256(json.dumps(
                    core,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode("ascii")).hexdigest()
                self.assertFalse(
                    manager.verify_research_artifact(tampered))

                manager.save()
                restored = VerifiedProposalManager(
                    "", manager_root, parser_command=command)
                self.assertEqual(
                    restored.snapshot()["parser_cross_traces"], 2)
                self.assertEqual(
                    restored.snapshot()[
                        "parser_cross_symbol_correspondences"],
                    snapshot["parser_cross_symbol_correspondences"],
                )
                self.assertTrue(restored.verify_research_artifact(
                    restored.research_artifact()))

                grammar_digest = hashlib.sha256(
                    Path(grammar).read_bytes()).hexdigest()
                self.assertEqual(
                    artifact["parser_forest_grammar_sha256s"],
                    [grammar_digest],
                )
                state_path = Path(manager_root, "proposal_state.json")
                state = json.loads(state_path.read_text())
                legacy = json.loads(json.dumps(state))
                legacy["schema"] = 14
                legacy_path = Path(manager_root, "legacy-v14.json")
                legacy_path.write_text(json.dumps(legacy))
                migrated = VerifiedProposalManager(
                    "",
                    manager_root,
                    state_path=str(legacy_path),
                    parser_command=command,
                )
                self.assertEqual(
                    migrated.snapshot()["parser_cross_both_accept"], 1)
                self.assertEqual(
                    migrated.snapshot()[
                        "parser_cross_symbol_correspondences"], 0)
                accepted_state = next(
                    item for item in state["records"]
                    if item["proposal_id"] == accepted_id
                )
                accepted_state[
                    "parser_cross_correspondence_json"] += " "
                state_path.write_text(json.dumps(state))
                rejected_restore = VerifiedProposalManager(
                    "", manager_root, parser_command=command)
                self.assertNotIn(
                    str(accepted_id), rejected_restore.records)
            finally:
                _stop_lark(server, socket_path, lark_script)

    def test_secondary_trace_tamper_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            selected_script = _write_selected_parser(tmp)
            input_path = os.path.join(tmp, "candidate")
            trace_path = os.path.join(tmp, "trace.json")
            Path(input_path).write_bytes(b"a")
            primary = " ".join((
                sys.executable,
                selected_script,
                "{input}",
                "{trace}",
                "a-only",
            ))
            secondary = " ".join((
                sys.executable,
                selected_script,
                "{input}",
                "{trace}",
                "accept",
            ))
            self.assertTrue(compare(
                input_path=input_path,
                trace_path=trace_path,
                primary_command=primary,
                secondary_command=secondary,
                timeout=5.0,
            ))
            trace = json.loads(Path(trace_path).read_text())
            self.assertEqual(
                trace["cross_parser_telemetry"]["schema"], CROSS_SCHEMA)
            self.assertEqual(
                trace["cross_parser_telemetry"]["proof"], CROSS_PROOF)
            manager = VerifiedProposalManager(
                "", os.path.join(tmp, "manager"))
            self.assertIsNotNone(manager._load_parser_trace(
                trace_path,
                candidate_size=1,
                returncode=0,
                candidate=b"a",
            ))

            trace["cross_parser_trace"]["nodes"][0]["state"] = "tampered"
            Path(trace_path).write_text(json.dumps(trace))
            self.assertIsNone(manager._load_parser_trace(
                trace_path,
                candidate_size=1,
                returncode=0,
                candidate=b"a",
            ))

    def test_identical_child_commands_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            selected_script = _write_selected_parser(tmp)
            input_path = os.path.join(tmp, "candidate")
            trace_path = os.path.join(tmp, "trace.json")
            Path(input_path).write_bytes(b"a")
            command = " ".join((
                sys.executable,
                selected_script,
                "{input}",
                "{trace}",
                "accept",
            ))
            with self.assertRaisesRegex(
                    RuntimeError, "must be independent"):
                compare(
                    input_path=input_path,
                    trace_path=trace_path,
                    primary_command=command,
                    secondary_command=command,
                    timeout=5.0,
                )

    def test_ambiguous_span_does_not_guess_a_symbol_mapping(self):
        primary = {
            "schema": "symcc-parser-structural-trace-v2",
            "parser": "primary-v1",
            "nodes": [
                {
                    "symbol": "A",
                    "state": "q",
                    "start": 0,
                    "end": 1,
                    "parent": -1,
                    "epsilon": False,
                },
                {
                    "symbol": "B",
                    "state": "q",
                    "start": 0,
                    "end": 1,
                    "parent": -1,
                    "epsilon": False,
                },
            ],
        }
        secondary = {
            "schema": "symcc-parser-structural-trace-v2",
            "parser": "secondary-v1",
            "nodes": [{
                "symbol": "X",
                "state": "q",
                "start": 0,
                "end": 1,
                "parent": -1,
                "epsilon": False,
            }],
        }
        result = VerifiedProposalManager._cross_correspondence(
            primary,
            VerifiedProposalManager._selected_trace_nodes(primary),
            secondary,
            VerifiedProposalManager._selected_trace_nodes(secondary),
        )
        self.assertEqual(result["ambiguous_symbol_spans"], 1)
        self.assertEqual(result["symbol_observations"], 0)
        self.assertEqual(result["production_observations"], 0)

    def test_deep_correspondence_uses_an_explicit_stack(self):
        depth = 1500

        def trace(parser, symbol):
            return {
                "schema": "symcc-parser-structural-trace-v2",
                "parser": parser,
                "nodes": [
                    {
                        "symbol": symbol,
                        "state": "q",
                        "start": 0,
                        "end": 1,
                        "parent": index - 1,
                        "epsilon": False,
                    }
                    for index in range(depth)
                ],
            }

        primary = trace("deep-primary-v1", "P")
        secondary = trace("deep-secondary-v1", "S")
        result = VerifiedProposalManager._cross_correspondence(
            primary,
            VerifiedProposalManager._selected_trace_nodes(primary),
            secondary,
            VerifiedProposalManager._selected_trace_nodes(secondary),
        )
        self.assertEqual(result["symbol_observations"], depth)
        self.assertEqual(result["production_observations"], depth - 1)
        self.assertEqual(result["symbols"], [["P", "S", depth]])


if __name__ == "__main__":
    unittest.main()
