#!/usr/bin/env python3
# RUN: python3 %s

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))
ROOT = Path(__file__).resolve().parents[1]

from hydra_transform import (  # noqa: E402
    _artifact_digest,
    build_profile,
    profile_text,
    replay_campaign,
    run_campaign,
    verify_campaign,
    verify_profile,
    write_denylist,
)


class HydraProfileTests(unittest.TestCase):
    def test_profile_attributes_cost_and_is_sealed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            telemetry = root / "telemetry.jsonl"
            telemetry.write_text(
                "\n".join([
                    json.dumps({
                        "solver_time_us": 3000,
                        "branch_trace": [
                            [1, 2, 3, 101, 1, 1],
                            [2, 3, 4, 202, 0, 0],
                        ],
                    }),
                    json.dumps({
                        "solver_time_us": 1000,
                        "branch_trace": [
                            [1, 2, 3, 101, 0, 0],
                        ],
                    }),
                ]),
                encoding="utf-8",
            )
            profile = build_profile([telemetry], denied_sites=[202])
            self.assertTrue(verify_profile(profile))
            self.assertEqual([row["site"] for row in profile["entries"]], [101])
            self.assertEqual(profile["entries"][0]["observations"], 2)
            self.assertEqual(profile["entries"][0]["interesting"], 1)
            self.assertEqual(profile["entries"][0]["solver_time_us"], 2500)
            self.assertIn("101 ", profile_text(profile))

            tampered = copy.deepcopy(profile)
            tampered["entries"][0]["score"] += 1
            self.assertFalse(verify_profile(tampered))

    def test_profile_v2_binds_producing_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            telemetry = root / "telemetry.json"
            telemetry.write_text(json.dumps({
                "solver_time_us": 0,
                "branch_trace": [[1, 2, 3, 404, 0, 0]],
            }), encoding="utf-8")
            command = [
                sys.executable,
                "-c",
                "import sys; sys.stdin.buffer.read()",
            ]
            profile = build_profile(
                [telemetry], profiled_command=command)
            self.assertTrue(verify_profile(profile))
            self.assertEqual(
                profile["schema"], "symcc-hydra-profile-v2")
            self.assertEqual(
                len(profile["profiled_command"]["executable_sha256"]), 64)
            text = profile_text(profile)
            self.assertIn("# symcc-hydra-profile-v2\n", text)
            self.assertIn(
                f"# profile_sha256 {profile['profile_sha256']}\n", text)
            self.assertIn(
                "# profiled_executable_sha256 "
                f"{profile['profiled_command']['executable_sha256']}\n",
                text,
            )

            tampered = copy.deepcopy(profile)
            tampered["profiled_command"]["argv"].append("--changed")
            tampered["profile_sha256"] = _artifact_digest(
                tampered, "profile_sha256")
            self.assertFalse(verify_profile(tampered))

            bool_tampered = copy.deepcopy(profile)
            bool_tampered["entries"][0]["observations"] = True
            bool_tampered["profile_sha256"] = _artifact_digest(
                bool_tampered, "profile_sha256")
            self.assertFalse(verify_profile(bool_tampered))


class HydraReplayTests(unittest.TestCase):
    @staticmethod
    def _manifest(
        path: Path,
        site: int,
        profile: dict | None = None,
    ) -> None:
        record = {
            "schema": "symcc-hydra-transform-v1",
            "site": site,
            "mode": "aggressive-memory",
            "single_site_build": True,
            "requires_original_replay": True,
        }
        if profile is not None:
            entry = next(
                row for row in profile["entries"]
                if row["site"] == site
            )
            record.update({
                "selection_source": "profile-v2",
                "profile_schema": "symcc-hydra-profile-v2",
                "profile_sha256": profile["profile_sha256"],
                "profiled_executable_sha256":
                    profile["profiled_command"]["executable_sha256"],
                "profiled_command_sha256":
                    profile["profiled_command_sha256"],
                "profile_score": entry["score"],
                "profile_observations": entry["observations"],
                "profile_interesting": entry["interesting"],
                "profile_solver_time_us": entry["solver_time_us"],
            })
        path.write_text(
            json.dumps(record) + "\n", encoding="ascii")

    def test_original_replay_filters_spurious_failures(self):
        site = 0xBEEF
        transformed_code = (
            "import sys; d=sys.stdin.buffer.read(); "
            "raise SystemExit(7 if d in (b'S', b'R') else 0)"
        )
        original_code = (
            "import sys; d=sys.stdin.buffer.read(); "
            "raise SystemExit(9 if d == b'R' else 0)"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "manifest.jsonl"
            self._manifest(manifest, site)
            inputs = []
            for name, content in (("spurious", b"S"), ("real", b"R"),
                                  ("ordinary", b"O")):
                path = root / name
                path.write_bytes(content)
                inputs.append(path)
            artifact = run_campaign(
                [sys.executable, "-c", original_code],
                [sys.executable, "-c", transformed_code],
                inputs,
                selected_site=site,
                manifest_path=manifest,
            )
            self.assertTrue(verify_campaign(artifact))
            self.assertTrue(artifact["failure_preservation_holds"])
            self.assertEqual(artifact["spurious_sites"], [site])
            self.assertEqual(len(artifact["accepted_failure_inputs"]), 1)
            classifications = {
                row["classification"] for row in artifact["rows"]
            }
            self.assertEqual(classifications, {
                "original-validated",
                "real-failure",
                "spurious-transformed-failure",
            })

            denylist = root / "denylist.txt"
            write_denylist(artifact["spurious_sites"], denylist)
            self.assertIn(str(site), denylist.read_text(encoding="ascii"))

            replay = replay_campaign(artifact)
            self.assertTrue(replay["semantic_match"])

            tampered = copy.deepcopy(artifact)
            tampered["accepted_failure_inputs"].append("0" * 64)
            self.assertFalse(verify_campaign(tampered))

    def test_failure_preservation_violation_is_explicit(self):
        site = 31337
        original_code = (
            "import sys; raise SystemExit("
            "4 if sys.stdin.buffer.read() == b'V' else 0)"
        )
        transformed_code = "import sys; sys.stdin.buffer.read()"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "manifest.jsonl"
            self._manifest(manifest, site)
            input_path = root / "violation"
            input_path.write_bytes(b"V")
            artifact = run_campaign(
                [sys.executable, "-c", original_code],
                [sys.executable, "-c", transformed_code],
                [input_path],
                selected_site=site,
                manifest_path=manifest,
            )
            self.assertTrue(verify_campaign(artifact))
            self.assertFalse(artifact["failure_preservation_holds"])
            self.assertEqual(
                artifact["rows"][0]["classification"],
                "failure-preservation-violation",
            )
            self.assertEqual(artifact["accepted_failure_inputs"], [])

    def test_profile_v2_is_bound_to_original_campaign_binary(self):
        site = 404
        original_code = "import sys; sys.stdin.buffer.read()"
        transformed_code = "import sys; sys.stdin.buffer.read()"
        original_command = [sys.executable, "-c", original_code]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            telemetry = root / "telemetry.json"
            telemetry.write_text(json.dumps({
                "solver_time_us": 0,
                "branch_trace": [[1, 2, 3, site, 0, 0]],
            }), encoding="utf-8")
            profile = build_profile(
                [telemetry], profiled_command=original_command)
            profile_path = root / "profile.json"
            profile_path.write_text(
                json.dumps(profile), encoding="ascii")
            manifest = root / "manifest.jsonl"
            self._manifest(manifest, site, profile)
            input_path = root / "input"
            input_path.write_bytes(b"P")

            artifact = run_campaign(
                original_command,
                [sys.executable, "-c", transformed_code],
                [input_path],
                selected_site=site,
                manifest_path=manifest,
                profile_artifact_path=profile_path,
            )
            self.assertEqual(
                artifact["schema"], "symcc-hydra-replay-v2")
            self.assertTrue(verify_campaign(artifact))

            tampered = copy.deepcopy(artifact)
            tampered["original"]["executable_sha256"] = "0" * 64
            tampered["campaign_sha256"] = _artifact_digest(
                tampered, "campaign_sha256")
            self.assertFalse(verify_campaign(tampered))

            bool_tampered = copy.deepcopy(artifact)
            bool_tampered["manifest"]["record"]["profile_score"] = True
            bool_tampered["campaign_sha256"] = _artifact_digest(
                bool_tampered, "campaign_sha256")
            self.assertFalse(verify_campaign(bool_tampered))

            with self.assertRaisesRegex(
                    ValueError, "binary identity do not match"):
                run_campaign(
                    [sys.executable, "-c", original_code + "; pass"],
                    [sys.executable, "-c", transformed_code],
                    [input_path],
                    selected_site=site,
                    manifest_path=manifest,
                    profile_artifact_path=profile_path,
                )

    def test_native_smoke_evidence_is_sealed(self):
        artifact = json.loads(
            (ROOT / "benchmark" / "evidence" / "hydra_f248_smoke.json")
            .read_text(encoding="ascii")
        )
        self.assertTrue(verify_campaign(artifact))
        self.assertEqual(artifact["counts"], {
            "real-failure": 1,
            "spurious-transformed-failure": 1,
        })
        self.assertEqual(artifact["spurious_sites"], [424248])
        self.assertTrue(artifact["failure_preservation_holds"])


if __name__ == "__main__":
    unittest.main()
