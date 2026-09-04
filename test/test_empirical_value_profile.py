#!/usr/bin/env python3
# RUN: python3 %s

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from empirical_value_profile import (  # noqa: E402
    MAX_PROFILES,
    LEGACY_ONLINE_ADMISSION_SCHEMA,
    LEGACY_ONLINE_PROFILE_SCHEMA,
    ONLINE_PROFILE_SCHEMA,
    PROFILE_SCHEMA,
    PROFILE_POLICY,
    RUNTIME_SCHEMA,
    aggregate_value_profiles,
    apply_online_admission_policy,
    materialize_runtime_profile,
    verify_value_profile,
)


class EmpiricalValueProfileTests(unittest.TestCase):
    CONTEXT_A = "a" * 64
    CONTEXT_B = "b" * 64

    def test_aggregates_limited_domain_with_sound_fallback_policy(self):
        artifact = aggregate_value_profiles([
            {
                "empirical_value_profile_context": self.CONTEXT_A,
                "empirical_value_profiles": [[11, 8, 4, 0, [[1, 3], [2, 1]]]],
            },
            {
                "empirical_value_profile_context": self.CONTEXT_A,
                "empirical_value_profiles": [[11, 8, 5, 0, [[1, 4], [2, 1]]]],
            },
        ], min_observations=8, max_distinct_values=2)
        self.assertEqual(artifact["schema"], PROFILE_SCHEMA)
        self.assertEqual(
            artifact["policy"],
            PROFILE_POLICY,
        )
        self.assertEqual(artifact["limited_domain_count"], 1)
        self.assertEqual(artifact["contexts"], [self.CONTEXT_A])
        profile = artifact["profiles"][0]
        self.assertEqual(profile["context_sha256"], self.CONTEXT_A)
        self.assertEqual(profile["observations"], 9)
        self.assertEqual(
            profile["values"],
            [{"value": 1, "count": 7}, {"value": 2, "count": 2}],
        )
        self.assertTrue(profile["limited_domain"])
        self.assertTrue(verify_value_profile(artifact))

        runtime = materialize_runtime_profile(artifact).decode("ascii")
        self.assertTrue(runtime.startswith(f"{RUNTIME_SCHEMA}\n"))
        self.assertIn(f"policy {PROFILE_POLICY}\n", runtime)
        self.assertIn(
            f"profile {self.CONTEXT_A} 11 8 2 1 2\n", runtime)

    def test_saturation_and_excess_values_fail_closed(self):
        artifact = aggregate_value_profiles([
            {
                "empirical_value_profile_context": self.CONTEXT_A,
                "empirical_value_profiles": [[7, 16, 9, 1, [[1, 8]]]],
            },
            {
                "empirical_value_profile_context": self.CONTEXT_A,
                "empirical_value_profiles": [
                    [8, 8, 9, 0, [[1, 3], [2, 3], [3, 3]]]],
            },
        ], min_observations=8, max_distinct_values=2)
        self.assertEqual(artifact["limited_domain_count"], 0)
        self.assertTrue(artifact["profiles"][0]["saturated"])
        self.assertIsNone(artifact["profiles"][0]["entropy_bits"])
        self.assertFalse(artifact["profiles"][1]["limited_domain"])

    def test_digest_tampering_and_malformed_counts_are_rejected(self):
        self.assertFalse(verify_value_profile(None))
        self.assertFalse(verify_value_profile([]))
        artifact = aggregate_value_profiles([
            {
                "empirical_value_profile_context": self.CONTEXT_A,
                "empirical_value_profiles": [[3, 8, 8, 0, [[4, 8]]]],
            },
            {
                "empirical_value_profile_context": self.CONTEXT_A,
                "empirical_value_profiles": [[4, 8, 2, 0, [[1, 3]]]],
            },
            {
                "empirical_value_profile_context": self.CONTEXT_A,
                "empirical_value_profiles": [[
                    5, 8, 65, 0, [[value, 1] for value in range(65)]],
                ],
            },
            {
                "empirical_value_profile_context": self.CONTEXT_A,
                "empirical_value_profiles": [[6, 8, 8.5, 0, [[1, 8]]]],
            },
        ])
        self.assertEqual(artifact["profile_count"], 1)
        tampered = copy.deepcopy(artifact)
        tampered["profiles"][0]["values"][0]["count"] = 7
        self.assertFalse(verify_value_profile(tampered))

    def test_different_executable_contexts_are_never_merged(self):
        artifact = aggregate_value_profiles([
            {
                "empirical_value_profile_context": self.CONTEXT_A,
                "empirical_value_profiles": [[9, 8, 8, 0, [[1, 8]]]],
            },
            {
                "empirical_value_profile_context": self.CONTEXT_B,
                "empirical_value_profiles": [[9, 8, 8, 0, [[2, 8]]]],
            },
            {"empirical_value_profiles": [[9, 8, 8, 0, [[3, 8]]]]},
        ])
        self.assertEqual(artifact["input_records"], 2)
        self.assertEqual(artifact["contexts"], [self.CONTEXT_A, self.CONTEXT_B])
        self.assertEqual(artifact["profile_count"], 2)
        self.assertEqual(artifact["limited_domain_count"], 2)
        self.assertEqual(
            [profile["observations"] for profile in artifact["profiles"]],
            [8, 8],
        )
        self.assertTrue(verify_value_profile(artifact))

    def test_cli_generates_and_verifies_artifact(self):
        script = ROOT / "util" / "empirical_value_profile.py"
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "telemetry.json"
            output = Path(tmp) / "profile.json"
            runtime = Path(tmp) / "profile.runtime"
            source.write_text(json.dumps({
                "empirical_value_profile_context": self.CONTEXT_A,
                "empirical_value_profiles": [[5, 8, 8, 0, [[0, 8]]]],
            }), encoding="ascii")
            generated = subprocess.run([
                sys.executable, str(script), str(source),
                "--output", str(output),
                "--runtime-output", str(runtime),
            ], capture_output=True, text=True, check=False)
            self.assertEqual(generated.returncode, 0, generated.stderr)
            self.assertEqual(
                json.loads(output.read_text(encoding="ascii"))["profiles"][0]
                ["entropy_bits"],
                0.0,
            )
            verified = subprocess.run([
                sys.executable, str(script), "--verify", str(output),
            ], capture_output=True, text=True, check=False)
            self.assertEqual(verified.returncode, 0, verified.stderr)
            self.assertEqual(json.loads(verified.stdout), {"verified": True})
            self.assertIn(
                "profile_count 1\n",
                runtime.read_text(encoding="ascii"),
            )

    def test_runtime_materialization_rejects_tampering_and_caps_domains(self):
        artifact = aggregate_value_profiles([{
            "empirical_value_profile_context": self.CONTEXT_A,
            "empirical_value_profiles": [[
                10, 8, 9, 0, [[value, 1] for value in range(9)]],
            ],
        }], min_observations=8, max_distinct_values=9)
        self.assertEqual(artifact["limited_domain_count"], 1)
        runtime = materialize_runtime_profile(artifact).decode("ascii")
        self.assertIn("profile_count 0\n", runtime)

        tampered = copy.deepcopy(artifact)
        tampered["profiles"][0]["site"] = 11
        with self.assertRaises(ValueError):
            materialize_runtime_profile(tampered)

    def test_online_admission_is_exact_domain_scoped_and_verified(self):
        source = {
            "empirical_value_profile_context": self.CONTEXT_A,
            "empirical_value_profiles": [[11, 8, 8, 0, [[1, 8]]]],
            "empirical_domain_feedback": [[
                11, 8, [1], 8, 0, 8, 0, 0, 0, 8, 0, 8_000,
            ]],
        }
        artifact = aggregate_value_profiles(
            [source], min_observations=1)
        online = apply_online_admission_policy(
            artifact, [source], min_solver_queries=8,
            min_validated_ratio_ppm=125_000)
        self.assertEqual(online["schema"], ONLINE_PROFILE_SCHEMA)
        self.assertEqual(online["runtime_domain_count"], 0)
        self.assertEqual(
            online["online_admission"]["suppressed"][0]["domain_values"],
            [1],
        )
        self.assertEqual(
            online["online_admission"]["suppressed"][0]["solver_time_us"],
            8_000,
        )
        self.assertTrue(verify_value_profile(online))
        self.assertIn(
            b"profile_count 0\n", materialize_runtime_profile(online))

        cheap_source = copy.deepcopy(source)
        cheap_source["empirical_domain_feedback"][0][-1] = 999
        cheap = apply_online_admission_policy(
            artifact, [cheap_source], min_solver_queries=8,
            min_validated_ratio_ppm=125_000, min_solver_time_us=1_000)
        self.assertEqual(cheap["runtime_domain_count"], 1)
        self.assertEqual(cheap["online_admission"]["suppressed"], [])
        self.assertTrue(verify_value_profile(cheap))

        legacy = copy.deepcopy(online)
        legacy["schema"] = LEGACY_ONLINE_PROFILE_SCHEMA
        legacy["online_admission"]["schema"] = (
            LEGACY_ONLINE_ADMISSION_SCHEMA)
        legacy["online_admission"].pop("min_solver_time_us")
        legacy_proof = legacy["online_admission"]["suppressed"][0]
        legacy_proof.pop("solver_time_us")
        legacy_proof["reason"] = "low-validated-query-ratio-v1"
        body = dict(legacy)
        body.pop("profile_sha256")
        legacy["profile_sha256"] = hashlib.sha256(json.dumps(
            body, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        ).encode("ascii")).hexdigest()
        self.assertTrue(verify_value_profile(legacy))
        self.assertIn(
            b"profile_count 0\n", materialize_runtime_profile(legacy))

        tampered = copy.deepcopy(online)
        tampered["online_admission"]["suppressed"][0]["reason"] = "forged"
        body = dict(tampered)
        body.pop("profile_sha256")
        tampered["profile_sha256"] = hashlib.sha256(json.dumps(
            body, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        ).encode("ascii")).hexdigest()
        self.assertFalse(verify_value_profile(tampered))

        insufficient_cost = copy.deepcopy(online)
        insufficient_cost["online_admission"]["suppressed"][0][
            "solver_time_us"] = 999
        body = dict(insufficient_cost)
        body.pop("profile_sha256")
        insufficient_cost["profile_sha256"] = hashlib.sha256(json.dumps(
            body, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        ).encode("ascii")).hexdigest()
        self.assertFalse(verify_value_profile(insufficient_cost))

        malformed = copy.deepcopy(online)
        malformed["online_admission"]["suppressed"][0][
            "domain_values"] = [{}]
        body = dict(malformed)
        body.pop("profile_sha256")
        malformed["profile_sha256"] = hashlib.sha256(json.dumps(
            body, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        ).encode("ascii")).hexdigest()
        self.assertFalse(verify_value_profile(malformed))

    def test_cross_document_profile_count_is_globally_bounded(self):
        first = [[site, 8, 1, 0, [[1, 1]]]
                 for site in range(1, MAX_PROFILES + 1)]
        documents = [
            {
                "empirical_value_profile_context": self.CONTEXT_A,
                "empirical_value_profiles": first,
            },
            {
                "empirical_value_profile_context": self.CONTEXT_A,
                "empirical_value_profiles": [
                    [MAX_PROFILES + 1, 8, 1, 0, [[2, 1]]]],
            },
        ]
        artifact = aggregate_value_profiles(
            documents, min_observations=1)
        self.assertEqual(artifact["profile_count"], MAX_PROFILES)
        self.assertTrue(verify_value_profile(artifact))
        reversed_artifact = aggregate_value_profiles(
            reversed(documents), min_observations=1)
        self.assertEqual(
            artifact["profile_sha256"],
            reversed_artifact["profile_sha256"],
        )


if __name__ == "__main__":
    unittest.main()
