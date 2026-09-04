#!/usr/bin/env python3
# RUN: python3 %s

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from research_evidence import (  # noqa: E402
    build_research_evidence,
    evidence_digest,
    validate_smt_backends,
    verify_research_evidence,
)


class ResearchEvidenceTests(unittest.TestCase):
    def test_backend_validator_records_expected_oracle_and_capability(self):
        result = validate_smt_backends(
            "(set-logic QF_LIA)\n(assert false)\n(check-sat)\n",
            expected="unsat",
        )
        self.assertTrue(result["expected_oracle_passed"])
        self.assertFalse(result["backend_disagreement"])
        self.assertGreaterEqual(result["available_backend_count"], 1)
        self.assertIn("z3", result["available_solver_families"])
        if len(result["available_solver_families"]) < 2:
            self.assertFalse(result["independent_family_consensus"])

    def test_evidence_manifest_enforces_claim_scope_and_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = build_research_evidence(tmp)
            self.assertTrue(verify_research_evidence(evidence))
            self.assertTrue(evidence["passed"])
            self.assertTrue(all(
                obligation["passed"]
                for obligation in evidence["obligations"]
            ))
            claims = {
                claim["feature"]: claim
                for claim in evidence["claim_scope"]
            }
            self.assertIn(
                "optimal DPOR",
                claims["F56"]["excluded_claims"],
            )
            self.assertIn(
                "native arbitrary-instruction process resume",
                claims["F59"]["excluded_claims"],
            )
            self.assertIn(
                "unbounded Optimal-DPOR",
                claims["F61"]["excluded_claims"],
            )
            self.assertIn(
                "path-dependent event existence",
                claims["F62"]["excluded_claims"],
            )

            tampered = json.loads(json.dumps(evidence))
            tampered["claim_scope"][0]["excluded_claims"] = []
            tampered["evidence_sha256"] = evidence_digest(tampered)
            self.assertFalse(verify_research_evidence(tampered))

    def test_evidence_cli_writes_and_verifies_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "evidence.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "util" / "research_evidence.py"),
                    "--work-root", str(Path(tmp) / "work"),
                    "--output", str(output),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            emitted = json.loads(completed.stdout)
            persisted = json.loads(output.read_text(encoding="ascii"))
            self.assertEqual(
                emitted["evidence_sha256"],
                persisted["evidence_sha256"],
            )
            verified = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "util" / "research_evidence.py"),
                    "--verify", str(output),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                json.loads(verified.stdout),
                {"verified": True},
            )


if __name__ == "__main__":
    unittest.main()
