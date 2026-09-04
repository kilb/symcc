# RUN: python3 %s

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


UTIL = os.path.join(os.path.dirname(os.path.dirname(__file__)), "util")
sys.path.insert(0, UTIL)

from parser_equivalence_audit import (  # noqa: E402
    AUDIT_PROOF,
    AUDIT_SCHEMA,
    audit,
    verify_artifact,
)


def _write_parser(directory):
    path = Path(directory, "parser.py")
    path.write_text(
        """\
import json
from pathlib import Path
import sys

input_path, trace_path, parser_name, policy = sys.argv[1:5]
data = Path(input_path).read_bytes()
accepted = policy == "all" or (
    policy == "a-plus" and bool(data) and set(data) == {ord("a")})
nodes = [{
    "symbol": "root",
    "state": policy,
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
Path(trace_path).write_text(json.dumps({
    "schema": "symcc-parser-structural-trace-v2",
    "parser": parser_name,
    "accepted": accepted,
    "nodes": nodes,
}))
raise SystemExit(0 if accepted else 1)
""",
        encoding="utf-8",
    )
    return str(path)


def _command(script, parser_name, policy):
    return " ".join((
        sys.executable,
        script,
        "{input}",
        "{trace}",
        parser_name,
        policy,
    ))


class ParserEquivalenceAuditTests(unittest.TestCase):
    def test_exhaustive_equal_domain_and_artifact_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            parser = _write_parser(tmp)
            artifact = audit(
                primary_command=_command(parser, "primary-v1", "a-plus"),
                secondary_command=_command(
                    parser, "secondary-v1", "a-plus"),
                alphabet_hex="6261",
                max_length=2,
                timeout=5.0,
            )
            self.assertEqual(artifact["schema"], AUDIT_SCHEMA)
            self.assertEqual(artifact["proof"], AUDIT_PROOF)
            self.assertEqual(artifact["alphabet_hex"], "6162")
            self.assertEqual(artifact["cases"], 7)
            self.assertEqual(artifact["both_accept"], 2)
            self.assertEqual(artifact["both_reject"], 5)
            self.assertTrue(artifact["equivalent"])
            self.assertEqual(artifact["mismatches"], 0)
            self.assertIsNone(artifact["minimal_counterexample"])
            self.assertTrue(verify_artifact(artifact))

    def test_shortlex_minimal_counterexample_and_cli_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            parser = _write_parser(tmp)
            primary = _command(parser, "primary-v1", "a-plus")
            secondary = _command(parser, "secondary-v1", "all")
            artifact = audit(
                primary_command=primary,
                secondary_command=secondary,
                alphabet_hex="6162",
                max_length=2,
                counterexample_limit=2,
                timeout=5.0,
            )
            self.assertFalse(artifact["equivalent"])
            self.assertEqual(artifact["both_accept"], 2)
            self.assertEqual(artifact["secondary_only"], 5)
            self.assertEqual(artifact["mismatches"], 5)
            self.assertTrue(artifact["counterexamples_truncated"])
            self.assertEqual(
                artifact["minimal_counterexample"]["input_hex"], "")
            self.assertEqual(
                artifact["counterexamples"][1]["input_hex"], "62")
            self.assertTrue(verify_artifact(artifact))

            output = os.path.join(tmp, "audit.json")
            command = [
                sys.executable,
                os.path.join(UTIL, "parser_equivalence_audit.py"),
                "--primary-command",
                primary,
                "--secondary-command",
                secondary,
                "--alphabet-hex",
                "6162",
                "--max-length",
                "1",
                "--output",
                output,
                "--require-equivalent",
            ]
            completed = subprocess.run(
                command, check=False, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 1, completed.stderr)
            self.assertTrue(Path(output).is_file())
            verified = subprocess.run(
                [
                    sys.executable,
                    os.path.join(UTIL, "parser_equivalence_audit.py"),
                    "--verify",
                    output,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(verified.returncode, 0, verified.stderr)
            self.assertEqual(
                json.loads(verified.stdout), {"verified": True})

    def test_tamper_and_domain_bounds_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            parser = _write_parser(tmp)
            command_a = _command(parser, "primary-v1", "a-plus")
            command_b = _command(parser, "secondary-v1", "a-plus")
            artifact = audit(
                primary_command=command_a,
                secondary_command=command_b,
                alphabet_hex="61",
                max_length=2,
            )
            tampered = json.loads(json.dumps(artifact))
            tampered["both_accept"] += 1
            core = dict(tampered)
            core.pop("artifact_sha256")
            tampered["artifact_sha256"] = hashlib.sha256(json.dumps(
                core,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("ascii")).hexdigest()
            self.assertFalse(verify_artifact(tampered))
            typed = json.loads(json.dumps(artifact))
            typed["cases"] = True
            core = dict(typed)
            core.pop("artifact_sha256")
            typed["artifact_sha256"] = hashlib.sha256(json.dumps(
                core,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("ascii")).hexdigest()
            self.assertFalse(verify_artifact(typed))
            with self.assertRaisesRegex(ValueError, "65536"):
                audit(
                    primary_command=command_a,
                    secondary_command=command_b,
                    alphabet_hex="000102030405060708090a0b0c0d0e0f",
                    max_length=5,
                )
            with self.assertRaisesRegex(ValueError, "unique"):
                audit(
                    primary_command=command_a,
                    secondary_command=command_b,
                    alphabet_hex="6161",
                    max_length=1,
                )


if __name__ == "__main__":
    unittest.main()
