# RUN: python3 %s

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "util" / "python_test_gate.py"
INVENTORY = ROOT / "util" / "python_test_inventory.py"


class PythonTestGateTests(unittest.TestCase):
    def run_inventory(self, root, source):
        root_path = Path(root)
        test_path = root_path / "test_gate_fixture.py"
        output_path = root_path / "nodeids.json"
        test_path.write_text(source, encoding="ascii")
        completed = subprocess.run(
            [
                sys.executable,
                str(INVENTORY),
                "--output",
                str(output_path),
                "--root",
                root_path.name,
                "--min-collected",
                "1",
                "--",
                "-p",
                "no:terminal",
                "-p",
                "no:cacheprovider",
            ],
            cwd=ROOT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=30,
        )
        payload = None
        if output_path.is_file():
            payload = json.loads(output_path.read_text(encoding="ascii"))
        return completed, output_path, payload

    def run_gate(self, root, source, *options, environment_overrides=None):
        test_path = Path(root) / "test_gate_fixture.py"
        output_path = Path(root) / "gate.json"
        test_path.write_text(source, encoding="ascii")
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        environment.update(environment_overrides or {})
        completed = subprocess.run(
            [
                sys.executable,
                str(GATE),
                "--output",
                str(output_path),
                *options,
                "--",
                "-q",
                "-p",
                "no:cacheprovider",
                str(test_path),
            ],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=30,
        )
        return completed, json.loads(output_path.read_text(encoding="ascii"))

    def test_passes_complete_capability_closed_fixture(self):
        with tempfile.TemporaryDirectory() as root:
            completed, payload = self.run_gate(
                root,
                "def test_ok():\n    assert True\n",
                "--min-collected",
                "1",
                "--require-command",
                Path(sys.executable).name,
                "--require-module",
                "pytest",
                "--require-library",
                "c",
            )
            isolated, isolated_payload = self.run_gate(
                root,
                "def test_ok():\n    assert True\n",
                "--min-collected",
                "1",
                environment_overrides={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": ""},
            )

        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertTrue(payload["gate"]["passed"])
        self.assertEqual(payload["outcomes"]["collected"], 1)
        self.assertEqual(payload["outcomes"]["skipped"], 0)
        self.assertEqual(payload["missing_capabilities"], [])
        self.assertEqual(isolated.returncode, 0, isolated.stdout)
        self.assertTrue(isolated_payload["pytest"]["plugin_autoload_disabled"])

    def test_rejects_skip_and_preserves_reason(self):
        with tempfile.TemporaryDirectory() as root:
            completed, payload = self.run_gate(
                root,
                "import pytest\n\n"
                "@pytest.mark.skip(reason='required backend absent')\n"
                "def test_skipped():\n    pass\n",
                "--min-collected",
                "1",
                "--max-skips",
                "0",
            )

        self.assertEqual(completed.returncode, 1, completed.stdout)
        self.assertFalse(payload["gate"]["passed"])
        self.assertEqual(payload["outcomes"]["skipped"], 1)
        self.assertIn(
            "required backend absent",
            payload["details"]["skipped"][0]["reason"],
        )
        self.assertIn(
            "skipped 1 (1 tests, 0 subtests) exceeds limit 0",
            payload["gate"]["failures"],
        )

    def test_rejects_subtest_skip_and_preserves_reason(self):
        with tempfile.TemporaryDirectory() as root:
            completed, payload = self.run_gate(
                root,
                "import unittest\n\n"
                "class GateFixture(unittest.TestCase):\n"
                "    def test_subtest(self):\n"
                "        with self.subTest(backend='missing'):\n"
                "            self.skipTest('subtest backend absent')\n",
                "--min-collected",
                "1",
                "--max-skips",
                "0",
            )

        self.assertEqual(completed.returncode, 1, completed.stdout)
        self.assertEqual(payload["outcomes"]["skipped"], 0)
        self.assertEqual(payload["outcomes"]["subtests_skipped"], 1)
        self.assertIn(
            "subtest backend absent",
            payload["details"]["subtests_skipped"][0]["reason"],
        )
        self.assertIn(
            "skipped 1 (0 tests, 1 subtests) exceeds limit 0",
            payload["gate"]["failures"],
        )

    def test_counts_duplicate_subtest_contexts_as_distinct_reports(self):
        with tempfile.TemporaryDirectory() as root:
            completed, payload = self.run_gate(
                root,
                "import unittest\n\n"
                "class GateFixture(unittest.TestCase):\n"
                "    def test_duplicate_contexts(self):\n"
                "        for value in ('same', 'same', 'same'):\n"
                "            with self.subTest(value=value):\n"
                "                self.assertEqual(value, 'same')\n",
                "--min-collected",
                "1",
            )

        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertEqual(payload["outcomes"]["passed"], 1)
        self.assertEqual(payload["outcomes"]["subtests_passed"], 3)

    def test_rejects_collection_floor_and_missing_capability(self):
        with tempfile.TemporaryDirectory() as root:
            below_floor, floor_payload = self.run_gate(
                root,
                "def test_one():\n    pass\n",
                "--min-collected",
                "2",
            )
            missing, missing_payload = self.run_gate(
                root,
                "def test_never_runs():\n    pass\n",
                "--require-command",
                "symcc-command-that-does-not-exist",
            )

        self.assertEqual(below_floor.returncode, 1, below_floor.stdout)
        self.assertIn("below floor", floor_payload["gate"]["failures"][0])
        self.assertEqual(missing.returncode, 2, missing.stdout)
        self.assertEqual(missing_payload["outcomes"]["collected"], 0)
        self.assertEqual(
            missing_payload["missing_capabilities"],
            ["command:symcc-command-that-does-not-exist"],
        )

    def test_generated_inventory_matches_exact_collection(self):
        source = "def test_expected():\n    assert True\n"
        with tempfile.TemporaryDirectory(dir=ROOT) as root:
            generated, manifest_path, manifest = self.run_inventory(root, source)
            completed, payload = self.run_gate(
                root,
                source,
                "--min-collected",
                "1",
                "--require-nodeid-manifest",
                str(manifest_path),
            )

        self.assertEqual(generated.returncode, 0, generated.stdout)
        self.assertEqual(manifest["schema"], "symcc-pytest-nodeid-manifest-v1")
        self.assertEqual(manifest["count"], 1)
        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertEqual(payload["schema"], "symcc-python-test-gate-v2")
        self.assertTrue(payload["inventory"]["matched"])
        self.assertEqual(payload["inventory"]["expected"], 1)
        self.assertEqual(payload["inventory"]["observed"], 1)
        self.assertEqual(
            payload["inventory"]["manifest_sha256"],
            payload["inventory"]["observed_sha256"],
        )

    def test_inventory_rejects_equal_count_test_replacement(self):
        before = "def test_expected():\n    assert True\n"
        after = "def test_replacement():\n    assert True\n"
        with tempfile.TemporaryDirectory(dir=ROOT) as root:
            generated, manifest_path, _ = self.run_inventory(root, before)
            completed, payload = self.run_gate(
                root,
                after,
                "--min-collected",
                "1",
                "--require-nodeid-manifest",
                str(manifest_path),
            )

        self.assertEqual(generated.returncode, 0, generated.stdout)
        self.assertEqual(completed.returncode, 1, completed.stdout)
        self.assertEqual(payload["outcomes"]["collected"], 1)
        self.assertEqual(payload["inventory"]["missing_count"], 1)
        self.assertEqual(payload["inventory"]["unexpected_count"], 1)
        self.assertFalse(payload["inventory"]["matched"])
        self.assertIn(
            "missing nodeids 1 exceeds limit 0",
            payload["gate"]["failures"],
        )
        self.assertIn(
            "unexpected nodeids 1 exceeds limit 0",
            payload["gate"]["failures"],
        )

    def test_inventory_rejects_tampered_digest_before_pytest(self):
        source = "def test_never_runs():\n    assert True\n"
        with tempfile.TemporaryDirectory(dir=ROOT) as root:
            generated, manifest_path, manifest = self.run_inventory(root, source)
            manifest["nodeids_sha256"] = "0" * 64
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="ascii",
            )
            completed, payload = self.run_gate(
                root,
                source,
                "--require-nodeid-manifest",
                str(manifest_path),
            )

        with tempfile.TemporaryDirectory(dir=ROOT) as root:
            generated_duplicate, duplicate_path, _ = self.run_inventory(
                root, source
            )
            serialized = duplicate_path.read_text(encoding="ascii")
            duplicate_path.write_text(
                serialized.replace(
                    '  "count": 1,',
                    '  "count": 1,\n  "count": 1,',
                    1,
                ),
                encoding="ascii",
            )
            duplicate, duplicate_payload = self.run_gate(
                root,
                source,
                "--require-nodeid-manifest",
                str(duplicate_path),
            )

        sys.path.insert(0, str(ROOT / "util"))
        try:
            import python_test_inventory as inventory_module
        finally:
            sys.path.pop(0)
        with tempfile.TemporaryDirectory() as root:
            bounded_path = Path(root) / "bounded.json"
            inventory_module.write_nodeid_manifest(
                bounded_path,
                inventory_module.build_nodeid_manifest(
                    "test", ["test/example.py::test_example"]
                ),
            )
            understated = mock.Mock(st_size=1)
            with (
                mock.patch.object(
                    inventory_module,
                    "_MAX_MANIFEST_BYTES",
                    32,
                ),
                mock.patch.object(Path, "stat", return_value=understated),
                self.assertRaisesRegex(
                    inventory_module.ManifestError,
                    "manifest exceeds 32 bytes",
                ),
            ):
                inventory_module.load_nodeid_manifest(bounded_path)

        self.assertEqual(generated.returncode, 0, generated.stdout)
        self.assertEqual(completed.returncode, 2, completed.stdout)
        self.assertEqual(payload["outcomes"]["collected"], 0)
        self.assertIn("does not match", payload["inventory"]["manifest_error"])
        self.assertIn("invalid node-id manifest", payload["gate"]["failures"][0])
        self.assertEqual(generated_duplicate.returncode, 0)
        self.assertEqual(duplicate.returncode, 2, duplicate.stdout)
        self.assertEqual(duplicate_payload["outcomes"]["collected"], 0)
        self.assertIn(
            "duplicate JSON object member 'count'",
            duplicate_payload["inventory"]["manifest_error"],
        )

    def test_ci_uses_strict_gate_and_archives_machine_readable_result(self):
        workflow = (ROOT / ".github/workflows/run_tests.yml").read_text(
            encoding="utf-8"
        )
        requirements = (ROOT / "requirements-test.txt").read_text(
            encoding="utf-8"
        )

        self.assertIn("python_quality:", workflow)
        self.assertIn("util/python_test_gate.py", workflow)
        self.assertIn("--min-collected 1597", workflow)
        self.assertIn("--max-skips 0", workflow)
        self.assertIn("--max-xfails 0", workflow)
        self.assertIn("--max-xpasses 0", workflow)
        self.assertIn("--max-deselected 0", workflow)
        self.assertIn("--max-missing-nodeids 0", workflow)
        self.assertIn("--max-unexpected-nodeids 0", workflow)
        self.assertIn("--require-nodeid-manifest test/pytest-nodeids.json", workflow)
        self.assertIn('PYTEST_DISABLE_PLUGIN_AUTOLOAD: "1"', workflow)
        self.assertIn("--require-command mpiexec", workflow)
        self.assertIn("actions/upload-artifact@v4", workflow)
        self.assertIn("if: always()", workflow)
        self.assertIn("pytest>=9.0,<10", requirements)


if __name__ == "__main__":
    unittest.main()
