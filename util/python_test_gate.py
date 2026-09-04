#!/usr/bin/env python3
"""Run pytest behind an explicit capability and outcome gate."""

from __future__ import annotations

import argparse
from collections import Counter
import ctypes.util
from dataclasses import dataclass, field
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any, Sequence

import pytest

if __package__:
    from .python_test_inventory import (
        ManifestError,
        NodeIdManifest,
        load_nodeid_manifest,
        nodeid_digest,
    )
else:
    from python_test_inventory import (
        ManifestError,
        NodeIdManifest,
        load_nodeid_manifest,
        nodeid_digest,
    )


SCHEMA = "symcc-python-test-gate-v2"
_DETAIL_LIMIT = 512
_INVENTORY_DETAIL_LIMIT = 1024


def _nonnegative(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be nonnegative")
    return parsed


def _bounded_detail(value: object) -> str:
    try:
        rendered = str(value)
    except Exception:
        rendered = type(value).__name__
    return rendered.replace("\x00", "?")[:_DETAIL_LIMIT]


def _report_key(report: object) -> str:
    nodeid = _bounded_detail(getattr(report, "nodeid", "<unknown>"))
    context = getattr(report, "context", None)
    if context is None:
        return nodeid
    return f"{nodeid}::{_bounded_detail(context)}"


def _skip_reason(report: object) -> str:
    longrepr = getattr(report, "longrepr", "")
    if isinstance(longrepr, tuple) and len(longrepr) >= 3:
        return _bounded_detail(longrepr[2])
    return _bounded_detail(longrepr)


@dataclass
class PytestGateRecorder:
    collected: int = 0
    collected_nodeids: list[str] = field(default_factory=list)
    deselected: int = 0
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    xfailed: list[tuple[str, str]] = field(default_factory=list)
    xpassed: list[str] = field(default_factory=list)
    subtests_passed: list[str] = field(default_factory=list)
    subtests_failed: list[str] = field(default_factory=list)
    subtests_skipped: list[tuple[str, str]] = field(default_factory=list)
    collection_errors: list[str] = field(default_factory=list)

    def pytest_collection_finish(self, session: object) -> None:
        items = getattr(session, "items", ())
        self.collected_nodeids = [
            str(getattr(item, "nodeid", "")) for item in items
        ]
        self.collected = len(self.collected_nodeids)

    def pytest_deselected(self, items: Sequence[object]) -> None:
        self.deselected += len(items)

    def pytest_collectreport(self, report: object) -> None:
        if getattr(report, "failed", False):
            self.collection_errors.append(
                _bounded_detail(getattr(report, "longrepr", report))
            )

    def pytest_runtest_logreport(self, report: object) -> None:
        key = _report_key(report)
        is_subtest = type(report).__name__ == "SubtestReport"
        was_xfail = getattr(report, "wasxfail", None)

        if was_xfail is not None:
            if getattr(report, "skipped", False):
                self.xfailed.append((key, _bounded_detail(was_xfail)))
            elif getattr(report, "passed", False):
                self.xpassed.append(key)
            return

        if getattr(report, "skipped", False):
            target = self.subtests_skipped if is_subtest else self.skipped
            target.append((key, _skip_reason(report)))
        elif getattr(report, "failed", False):
            target = self.subtests_failed if is_subtest else self.failed
            target.append(key)
        elif getattr(report, "when", "") == "call" and getattr(
            report, "passed", False
        ):
            target = self.subtests_passed if is_subtest else self.passed
            target.append(key)


def _capabilities(
    commands: Sequence[str],
    modules: Sequence[str],
    libraries: Sequence[str],
) -> tuple[dict[str, dict[str, str]], list[str]]:
    observed: dict[str, dict[str, str]] = {
        "commands": {},
        "python_modules": {},
        "libraries": {},
    }
    missing: list[str] = []

    for command in commands:
        resolved = shutil.which(command) or ""
        observed["commands"][command] = resolved
        if not resolved:
            missing.append(f"command:{command}")

    for module in modules:
        try:
            spec = importlib.util.find_spec(module)
        except (ImportError, ModuleNotFoundError, ValueError):
            spec = None
        resolved = "" if spec is None else str(spec.origin or "namespace")
        observed["python_modules"][module] = resolved
        if spec is None:
            missing.append(f"python-module:{module}")

    for library in libraries:
        try:
            resolved = ctypes.util.find_library(library) or ""
        except (AttributeError, OSError):
            resolved = ""
        observed["libraries"][library] = resolved
        if not resolved:
            missing.append(f"library:{library}")

    return observed, sorted(missing)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="ascii",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--min-collected", type=_nonnegative, default=1)
    parser.add_argument("--max-skips", type=_nonnegative, default=0)
    parser.add_argument("--max-xfails", type=_nonnegative, default=0)
    parser.add_argument("--max-xpasses", type=_nonnegative, default=0)
    parser.add_argument("--max-deselected", type=_nonnegative, default=0)
    parser.add_argument("--max-missing-nodeids", type=_nonnegative, default=0)
    parser.add_argument("--max-unexpected-nodeids", type=_nonnegative, default=0)
    parser.add_argument("--require-nodeid-manifest", type=Path)
    parser.add_argument("--require-command", action="append", default=[])
    parser.add_argument("--require-module", action="append", default=[])
    parser.add_argument("--require-library", action="append", default=[])
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    return parser


def _base_payload(
    arguments: argparse.Namespace,
    capabilities: dict[str, dict[str, str]],
    missing: Sequence[str],
    pytest_args: Sequence[str],
    manifest: NodeIdManifest | None,
    manifest_error: str,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "python": {
            "executable": sys.executable,
            "version": ".".join(str(value) for value in sys.version_info[:3]),
        },
        "pytest": {
            "version": pytest.__version__,
            "arguments": list(pytest_args),
            "exit_code": None,
            "plugin_autoload_disabled": (
                os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") == "1"
            ),
        },
        "capabilities": capabilities,
        "missing_capabilities": list(missing),
        "inventory": {
            "required": arguments.require_nodeid_manifest is not None,
            "manifest_path": (
                None
                if arguments.require_nodeid_manifest is None
                else str(arguments.require_nodeid_manifest)
            ),
            "manifest_schema": (
                None if manifest is None else "symcc-pytest-nodeid-manifest-v1"
            ),
            "manifest_sha256": None if manifest is None else manifest.sha256,
            "manifest_error": manifest_error or None,
            "expected": 0 if manifest is None else len(manifest.nodeids),
            "observed": 0,
            "observed_sha256": None,
            "missing_count": 0,
            "unexpected_count": 0,
            "duplicate_observed_count": 0,
            "missing": [],
            "unexpected": [],
            "duplicate_observed": [],
            "details_truncated": False,
            "matched": False,
        },
        "outcomes": {
            "collected": 0,
            "deselected": 0,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "xfailed": 0,
            "xpassed": 0,
            "subtests_passed": 0,
            "subtests_failed": 0,
            "subtests_skipped": 0,
            "collection_errors": 0,
        },
        "details": {
            "skipped": [],
            "subtests_skipped": [],
            "xfailed": [],
            "collection_errors": [],
        },
        "gate": {
            "limits": {
                "min_collected": arguments.min_collected,
                "max_skips": arguments.max_skips,
                "max_xfails": arguments.max_xfails,
                "max_xpasses": arguments.max_xpasses,
                "max_deselected": arguments.max_deselected,
                "max_missing_nodeids": arguments.max_missing_nodeids,
                "max_unexpected_nodeids": arguments.max_unexpected_nodeids,
            },
            "failures": [],
            "passed": False,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    pytest_args = list(arguments.pytest_args)
    if pytest_args[:1] == ["--"]:
        pytest_args.pop(0)
    if not pytest_args:
        pytest_args = ["-q", "-W", "error"]

    capabilities, missing = _capabilities(
        arguments.require_command,
        arguments.require_module,
        arguments.require_library,
    )
    manifest: NodeIdManifest | None = None
    manifest_error = ""
    if arguments.require_nodeid_manifest is not None:
        try:
            manifest = load_nodeid_manifest(arguments.require_nodeid_manifest)
        except ManifestError as error:
            manifest_error = _bounded_detail(error)
    payload = _base_payload(
        arguments,
        capabilities,
        missing,
        pytest_args,
        manifest,
        manifest_error,
    )
    preflight_failures = [
        f"missing required capability {value}" for value in missing
    ]
    if manifest_error:
        preflight_failures.append(f"invalid node-id manifest: {manifest_error}")
    if preflight_failures:
        payload["gate"]["failures"] = preflight_failures
        _atomic_write_json(arguments.output, payload)
        print("python-test-gate: FAIL (preflight)")
        return 2

    recorder = PytestGateRecorder()
    exit_code = int(pytest.main(pytest_args, plugins=[recorder]))
    outcomes = {
        "collected": recorder.collected,
        "deselected": recorder.deselected,
        "passed": len(recorder.passed),
        "failed": len(recorder.failed),
        "skipped": len(recorder.skipped),
        "xfailed": len(recorder.xfailed),
        "xpassed": len(recorder.xpassed),
        "subtests_passed": len(recorder.subtests_passed),
        "subtests_failed": len(recorder.subtests_failed),
        "subtests_skipped": len(recorder.subtests_skipped),
        "collection_errors": len(recorder.collection_errors),
    }
    payload["pytest"]["exit_code"] = exit_code
    payload["outcomes"] = outcomes
    payload["details"] = {
        "skipped": [
            {"nodeid": nodeid, "reason": reason}
            for nodeid, reason in sorted(recorder.skipped)
        ],
        "subtests_skipped": [
            {"nodeid": nodeid, "reason": reason}
            for nodeid, reason in sorted(recorder.subtests_skipped)
        ],
        "xfailed": [
            {"nodeid": nodeid, "reason": reason}
            for nodeid, reason in sorted(recorder.xfailed)
        ],
        "collection_errors": recorder.collection_errors,
    }

    observed_nodeids = recorder.collected_nodeids
    observed_counts = Counter(observed_nodeids)
    duplicate_observed = sorted(
        nodeid for nodeid, count in observed_counts.items() if count > 1
    )
    observed_unique = set(observed_nodeids)
    expected_nodeids = set() if manifest is None else set(manifest.nodeids)
    missing_nodeids = sorted(expected_nodeids - observed_unique)
    unexpected_nodeids = sorted(observed_unique - expected_nodeids)
    if manifest is None:
        unexpected_nodeids = []
    inventory_groups = (
        missing_nodeids,
        unexpected_nodeids,
        duplicate_observed,
    )
    inventory_details_truncated = any(
        len(group) > _INVENTORY_DETAIL_LIMIT
        or any(len(nodeid) > _DETAIL_LIMIT for nodeid in group)
        for group in inventory_groups
    )
    payload["inventory"].update(
        {
            "observed": len(observed_nodeids),
            "observed_sha256": nodeid_digest(sorted(observed_nodeids)),
            "missing_count": len(missing_nodeids),
            "unexpected_count": len(unexpected_nodeids),
            "duplicate_observed_count": len(duplicate_observed),
            "missing": [
                _bounded_detail(nodeid)
                for nodeid in missing_nodeids[:_INVENTORY_DETAIL_LIMIT]
            ],
            "unexpected": [
                _bounded_detail(nodeid)
                for nodeid in unexpected_nodeids[:_INVENTORY_DETAIL_LIMIT]
            ],
            "duplicate_observed": [
                _bounded_detail(nodeid)
                for nodeid in duplicate_observed[:_INVENTORY_DETAIL_LIMIT]
            ],
            "details_truncated": inventory_details_truncated,
            "matched": (
                manifest is not None
                and not missing_nodeids
                and not unexpected_nodeids
                and not duplicate_observed
            ),
        }
    )

    failures: list[str] = []
    if exit_code != int(pytest.ExitCode.OK):
        failures.append(f"pytest exit code is {exit_code}, expected 0")
    if recorder.collected < arguments.min_collected:
        failures.append(
            f"collected {recorder.collected} is below floor "
            f"{arguments.min_collected}"
        )
    total_skipped = outcomes["skipped"] + outcomes["subtests_skipped"]
    if total_skipped > arguments.max_skips:
        failures.append(
            f"skipped {total_skipped} "
            f"({outcomes['skipped']} tests, "
            f"{outcomes['subtests_skipped']} subtests) exceeds limit "
            f"{arguments.max_skips}"
        )
    if outcomes["xfailed"] > arguments.max_xfails:
        failures.append(
            f"xfailed {outcomes['xfailed']} exceeds limit {arguments.max_xfails}"
        )
    if outcomes["xpassed"] > arguments.max_xpasses:
        failures.append(
            f"xpassed {outcomes['xpassed']} exceeds limit {arguments.max_xpasses}"
        )
    if recorder.deselected > arguments.max_deselected:
        failures.append(
            f"deselected {recorder.deselected} exceeds limit "
            f"{arguments.max_deselected}"
        )
    if recorder.collection_errors:
        failures.append(
            f"collection errors observed: {len(recorder.collection_errors)}"
        )
    if manifest is not None:
        if len(missing_nodeids) > arguments.max_missing_nodeids:
            failures.append(
                f"missing nodeids {len(missing_nodeids)} exceeds limit "
                f"{arguments.max_missing_nodeids}"
            )
        if len(unexpected_nodeids) > arguments.max_unexpected_nodeids:
            failures.append(
                f"unexpected nodeids {len(unexpected_nodeids)} exceeds limit "
                f"{arguments.max_unexpected_nodeids}"
            )
        if duplicate_observed:
            failures.append(
                f"duplicate observed nodeids: {len(duplicate_observed)}"
            )

    payload["gate"]["failures"] = failures
    payload["gate"]["passed"] = not failures
    _atomic_write_json(arguments.output, payload)
    state = "PASS" if not failures else "FAIL"
    print(
        f"python-test-gate: {state} "
        f"(collected={recorder.collected}, skipped={total_skipped}, "
        f"xfailed={outcomes['xfailed']}, deselected={recorder.deselected}, "
        f"missing-nodeids={len(missing_nodeids)}, "
        f"unexpected-nodeids={len(unexpected_nodeids)})"
    )
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
