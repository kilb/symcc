#!/usr/bin/env python3
# RUN: python3 %s

import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from qf_bv_backend import QF_BV_OPERATORS  # noqa: E402
from qf_bv_conformance import (  # noqa: E402
    BITWUZLA_VERSION,
    CONFORMANCE_SCHEMA,
    BackendSpec,
    artifact_digest,
    build_operator_matrix_envelope,
    load_backend_specs,
    replay_qfbv_conformance,
    run_qfbv_conformance,
    semantic_digest,
    verify_qfbv_conformance,
)


class QfBvConformanceTest(unittest.TestCase):
    def test_operator_matrix_has_exact_supported_surface(self):
        envelope = build_operator_matrix_envelope()
        operators = {node["op"] for node in envelope["nodes"]}
        self.assertEqual(operators, set(QF_BV_OPERATORS))
        self.assertEqual(envelope["input_hex"], "0000")

    def test_backend_configuration_is_exact_and_bounded(self):
        configuration = json.dumps({
            "backends": [{
                "name": "solver-1.2.3",
                "command": ["solver", "{query}"],
                "version_command": ["solver", "--version"],
                "expected_version": "1.2.3",
            }],
        })
        specs = load_backend_specs(configuration)
        self.assertEqual(specs[0].expected_version, "1.2.3")
        with self.assertRaisesRegex(ValueError, "semantic version"):
            load_backend_specs(json.dumps([{
                "name": "solver",
                "command": ["solver"],
                "version_command": ["solver", "--version"],
                "expected_version": "latest",
            }]))

    @unittest.skipUnless(
        shutil.which("bitwuzla"),
        "Bitwuzla is not installed",
    )
    def test_fixed_bitwuzla_artifact_verifies_and_detects_tampering(self):
        spec = BackendSpec(
            name="bitwuzla-0.9.1",
            command=(
                "bitwuzla",
                "--lang",
                "smt2",
                "--produce-models",
                "--time-limit",
                "{timeout_ms}",
                "{query}",
            ),
            version_command=("bitwuzla", "--version"),
            expected_version=BITWUZLA_VERSION,
        )
        artifact = run_qfbv_conformance((spec,))
        self.assertEqual(artifact["schema"], CONFORMANCE_SCHEMA)
        self.assertTrue(verify_qfbv_conformance(artifact))
        backend = artifact["backends"][0]
        self.assertEqual(backend["actual_version"], BITWUZLA_VERSION)
        self.assertEqual(
            set(backend["operator_matrix"]["lowering_certificate"][
                "operator_counts"
            ]),
            set(QF_BV_OPERATORS),
        )
        self.assertEqual(
            backend["operator_matrix"]["assignments"],
            {"0": 0x42, "1": 0x03},
        )
        self.assertTrue(backend["operator_matrix"]["backend_model_verified"])
        self.assertEqual(backend["unsat_rejected"]["status"], "unknown")
        self.assertEqual(
            backend["unsat_rejected"]["backend_status"],
            "unsat",
        )
        self.assertEqual(backend["unsat_authorized"]["status"], "unsat")

        tampered = copy.deepcopy(artifact)
        tampered["backends"][0]["operator_matrix"]["assignments"]["0"] = 0
        tampered["semantic_sha256"] = semantic_digest(tampered)
        tampered["artifact_sha256"] = artifact_digest(tampered)
        self.assertFalse(verify_qfbv_conformance(tampered))

    @unittest.skipUnless(
        shutil.which("bitwuzla"),
        "Bitwuzla is not installed",
    )
    def test_fixed_bitwuzla_artifact_replays_semantically(self):
        spec = BackendSpec(
            name="bitwuzla-0.9.1",
            command=(
                "bitwuzla",
                "--lang",
                "smt2",
                "--produce-models",
                "--time-limit",
                "{timeout_ms}",
                "{query}",
            ),
            version_command=("bitwuzla", "--version"),
            expected_version=BITWUZLA_VERSION,
        )
        original = run_qfbv_conformance((spec,))
        replay = replay_qfbv_conformance(original)
        self.assertTrue(replay["semantic_match"])
        self.assertTrue(verify_qfbv_conformance(replay["replay"]))

    @unittest.skipUnless(
        shutil.which("bitwuzla"),
        "Bitwuzla is not installed",
    )
    def test_cli_generates_and_offline_verifies_artifact(self):
        script = (
            Path(__file__).resolve().parents[1]
            / "util"
            / "qf_bv_conformance.py"
        )
        configuration = [{
            "name": "bitwuzla-0.9.1",
            "command": [
                "bitwuzla",
                "--lang",
                "smt2",
                "--produce-models",
                "--time-limit",
                "{timeout_ms}",
                "{query}",
            ],
            "version_command": ["bitwuzla", "--version"],
            "expected_version": BITWUZLA_VERSION,
        }]
        with tempfile.TemporaryDirectory() as root:
            config_path = Path(root) / "backends.json"
            artifact_path = Path(root) / "evidence.json"
            config_path.write_text(
                json.dumps(configuration),
                encoding="ascii",
            )
            generated = __import__("subprocess").run(
                [
                    sys.executable,
                    str(script),
                    "--backend-config",
                    str(config_path),
                    "--output",
                    str(artifact_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            verified = __import__("subprocess").run(
                [sys.executable, str(script), "--verify", str(artifact_path)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(verified.returncode, 0, verified.stderr)
            self.assertEqual(
                json.loads(verified.stdout),
                {"verified": True},
            )


if __name__ == "__main__":
    unittest.main()
