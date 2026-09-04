# RUN: python3 %s

import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from native_condpor_campaign import (  # noqa: E402
    NATIVE_CONDPOR_CAMPAIGN_SCHEMA,
    _atomic_commit_summary,
    run_native_condpor_campaign,
    verify_native_condpor_campaign_certificate,
)
from schedule_exploration import parse_schedule_trace  # noqa: E402


class NativeCondporCampaignTests(unittest.TestCase):
    def test_sc_hardware_rf_claim_requires_complete_atomic_commit_evidence(self):
        atomic = parse_schedule_trace(
            "0 1 write 0x100 atomic=1 kind=store group=1 bytes=4 mo=seq_cst\n"
            "1 1 atomic_value 0x100 group=1 role=write bits=32 value=0x1\n"
            "2 1 atomic_commit 0x100 group=1 mode=1 advanced=1 "
            "mismatch=0 prefix-index=0\n"
            "3 2 read 0x100 atomic=1 kind=load group=2 bytes=4 mo=seq_cst\n"
            "4 2 atomic_value 0x100 group=2 role=read bits=32 value=0x1\n"
            "5 2 atomic_commit 0x100 group=2 mode=1 advanced=1 "
            "mismatch=0 prefix-index=1\n"
        )
        summary = _atomic_commit_summary(atomic, "SC")
        self.assertTrue(summary["sc_reads_from_hardware_enforced"])
        self.assertEqual(summary["committed_atomic_event_count"], 2)
        self.assertFalse(
            _atomic_commit_summary(atomic, "RA")[
                "sc_reads_from_hardware_enforced"
            ]
        )
        generic = parse_schedule_trace(
            "0 1 write 0x100 atomic=0 bytes=4\n"
            "1 2 read 0x100 atomic=0 bytes=4\n"
        )
        self.assertFalse(
            _atomic_commit_summary(generic, "SC")[
                "sc_reads_from_hardware_enforced"
            ]
        )

    def _build_fixture(self, directory):
        cc = os.environ.get("CC", "cc")
        runtime = directory / "libsymcc_schedule_rt.so"
        target = directory / "native_condpor_target"
        subprocess.run(
            [
                cc,
                "-std=c11",
                "-shared",
                "-fPIC",
                "-pthread",
                str(ROOT / "util" / "symcc_schedule_rt.c"),
                "-ldl",
                "-o",
                str(runtime),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            [
                cc,
                "-std=c11",
                "-O0",
                "-pthread",
                str(ROOT / "test" / "native_condpor_campaign_target.c"),
                "-ldl",
                "-o",
                str(target),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return runtime, target

    def test_native_campaign_reexecutes_and_regenerates_control_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            runtime, target = self._build_fixture(directory)
            certificate = run_native_condpor_campaign(
                [str(target)],
                schedule_runtime=runtime,
                cwd=directory,
                memory_model="SC",
                max_runs=8,
                max_prefixes=16,
                max_successors_per_run=8,
                max_graph_candidates=64,
                timeout_seconds=5,
            )

        self.assertEqual(
            certificate["schema"], NATIVE_CONDPOR_CAMPAIGN_SCHEMA
        )
        self.assertEqual(certificate["status"], "complete")
        self.assertTrue(certificate["bounded_fixed_point"])
        self.assertGreaterEqual(certificate["run_count"], 2)
        self.assertGreaterEqual(certificate["path_regeneration_count"], 1)
        self.assertEqual(certificate["invalid_run_count"], 0)
        actions = {
            event["object"]
            for run in certificate["runs"]
            for event in run["execution"]["trace_events"]
            if event["op"] == "action"
        }
        self.assertEqual(actions, {"0x101", "0x202"})
        self.assertTrue(
            verify_native_condpor_campaign_certificate(certificate)
        )

        tampered = json.loads(json.dumps(certificate))
        tampered["runs"][0]["analysis"]["memory_graph"]["graph_count"] += 1
        self.assertFalse(
            verify_native_condpor_campaign_certificate(tampered)
        )

        protocol_tampered = json.loads(json.dumps(certificate))
        protocol_tampered["runs"][0]["execution"][
            "atomic_commit_mismatch_count"
        ] = 1
        unsigned = {
            key: value
            for key, value in protocol_tampered.items()
            if key != "certificate_sha256"
        }
        protocol_tampered["certificate_sha256"] = hashlib.sha256(
            json.dumps(
                unsigned, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        self.assertFalse(
            verify_native_condpor_campaign_certificate(protocol_tampered)
        )

    def test_native_campaign_reports_run_bound_without_false_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            runtime, target = self._build_fixture(directory)
            certificate = run_native_condpor_campaign(
                [str(target)],
                schedule_runtime=runtime,
                cwd=directory,
                max_runs=1,
                max_prefixes=16,
                max_successors_per_run=8,
                max_graph_candidates=64,
                timeout_seconds=5,
            )

        self.assertEqual(certificate["status"], "truncated")
        self.assertFalse(certificate["bounded_fixed_point"])
        self.assertTrue(certificate["truncated"]["run_limit"])
        self.assertGreater(certificate["pending_prefix_count"], 0)
        self.assertTrue(
            verify_native_condpor_campaign_certificate(certificate)
        )

    def test_native_campaign_rejects_owned_environment_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            runtime, target = self._build_fixture(directory)
            with self.assertRaisesRegex(ValueError, "campaign-owned"):
                run_native_condpor_campaign(
                    [str(target)],
                    schedule_runtime=runtime,
                    cwd=directory,
                    environment={"SYMCC_SCHEDULE_TRACE": "/tmp/other"},
                )

    def test_native_campaign_cli_explore_and_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            runtime, target = self._build_fixture(directory)
            certificate = directory / "campaign.json"
            explored = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "util" / "symcc_native_condpor.py"),
                    "explore",
                    "--runtime",
                    str(runtime),
                    "--output",
                    str(certificate),
                    "--cwd",
                    str(directory),
                    "--max-runs",
                    "8",
                    "--max-prefixes",
                    "16",
                    "--max-successors-per-run",
                    "8",
                    "--max-graph-candidates",
                    "64",
                    "--timeout-seconds",
                    "5",
                    "--",
                    str(target),
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(explored.returncode, 0, explored.stderr)
            self.assertTrue(certificate.is_file())
            verified = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "util" / "symcc_native_condpor.py"),
                    "verify",
                    str(certificate),
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(verified.returncode, 0, verified.stderr)
            self.assertEqual(
                verified.stdout.strip(), explored.stdout.strip()
            )


if __name__ == "__main__":
    unittest.main()
