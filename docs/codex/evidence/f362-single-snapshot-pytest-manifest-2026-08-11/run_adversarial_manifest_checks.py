#!/usr/bin/env python3
"""Reproduce F362 manifest-admission counterexamples with production tools."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest import mock


EVIDENCE = Path(__file__).resolve().parent
REPO = EVIDENCE.parents[3]
UTIL = REPO / "util"
sys.path.insert(0, str(UTIL))

import python_test_inventory as inventory  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def _run_gate(
    fixture: Path,
    output: Path,
    *options: str,
    plugin_environment: str = "1",
) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = plugin_environment
    completed = subprocess.run(
        [
            sys.executable,
            str(UTIL / "python_test_gate.py"),
            "--output",
            str(output),
            *options,
            "--",
            "-q",
            "-p",
            "no:cacheprovider",
            str(fixture),
        ],
        cwd=REPO,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=30,
        check=False,
    )
    return completed, json.loads(output.read_text(encoding="ascii"))


def main() -> int:
    arguments = _parser().parse_args()
    with tempfile.TemporaryDirectory(
        prefix=".f362-manifest-", dir=REPO
    ) as temporary_text:
        temporary = Path(temporary_text)
        fixture = temporary / "test_gate_fixture.py"
        fixture.write_text("def test_expected():\n    assert True\n", encoding="ascii")

        bounded_path = temporary / "bounded.json"
        inventory.write_nodeid_manifest(
            bounded_path,
            inventory.build_nodeid_manifest(
                "test", ["test/example.py::test_example"]
            ),
        )
        encoded_bytes = bounded_path.stat().st_size
        fake_stat = mock.Mock(st_size=1)
        bounded_error = ""
        with (
            mock.patch.object(inventory, "_MAX_MANIFEST_BYTES", 32),
            mock.patch.object(Path, "stat", return_value=fake_stat) as stat_probe,
        ):
            try:
                inventory.load_nodeid_manifest(bounded_path)
            except inventory.ManifestError as error:
                bounded_error = str(error)

        root = temporary.relative_to(REPO).as_posix()
        nodeid = f"{root}/{fixture.name}::test_expected"
        duplicate_path = temporary / "duplicate.json"
        inventory.write_nodeid_manifest(
            duplicate_path,
            inventory.build_nodeid_manifest(root, [nodeid]),
        )
        duplicate_path.write_text(
            duplicate_path.read_text(encoding="ascii").replace(
                '  "count": 1,',
                '  "count": 1,\n  "count": 1,',
                1,
            ),
            encoding="ascii",
        )
        duplicate_run, duplicate_payload = _run_gate(
            fixture,
            temporary / "duplicate-gate.json",
            "--require-nodeid-manifest",
            str(duplicate_path),
        )

        empty_run, empty_payload = _run_gate(
            fixture,
            temporary / "empty-env-gate.json",
            "--min-collected",
            "1",
            plugin_environment="",
        )

    payload = {
        "schema": "symcc-f362-adversarial-manifest-check-v1",
        "single_snapshot_size_bound": {
            "configured_limit": 32,
            "encoded_bytes": encoded_bytes,
            "forged_stat_bytes": 1,
            "path_stat_calls_during_load": stat_probe.call_count,
            "rejected": bounded_error == "manifest exceeds 32 bytes",
            "error": bounded_error,
        },
        "duplicate_json_member": {
            "returncode": duplicate_run.returncode,
            "pytest_exit_code": duplicate_payload["pytest"]["exit_code"],
            "collected": duplicate_payload["outcomes"]["collected"],
            "manifest_error": duplicate_payload["inventory"]["manifest_error"],
            "gate_passed": duplicate_payload["gate"]["passed"],
        },
        "empty_plugin_environment": {
            "returncode": empty_run.returncode,
            "collected": empty_payload["outcomes"]["collected"],
            "passed": empty_payload["outcomes"]["passed"],
            "plugin_autoload_disabled": empty_payload["pytest"][
                "plugin_autoload_disabled"
            ],
            "gate_passed": empty_payload["gate"]["passed"],
        },
    }
    arguments.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="ascii",
    )
    ok = (
        payload["single_snapshot_size_bound"]["rejected"] is True
        and payload["single_snapshot_size_bound"]["path_stat_calls_during_load"]
        == 0
        and payload["duplicate_json_member"]["returncode"] == 2
        and payload["duplicate_json_member"]["pytest_exit_code"] is None
        and payload["duplicate_json_member"]["collected"] == 0
        and "duplicate JSON object member 'count'"
        in payload["duplicate_json_member"]["manifest_error"]
        and payload["duplicate_json_member"]["gate_passed"] is False
        and payload["empty_plugin_environment"]
        == {
            "returncode": 0,
            "collected": 1,
            "passed": 1,
            "plugin_autoload_disabled": True,
            "gate_passed": True,
        }
    )
    print(
        "f362-adversarial-manifest-check: "
        f"{'PASS' if ok else 'FAIL'} "
        "(bounded-read, duplicate-key, forced-plugin-isolation)"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
