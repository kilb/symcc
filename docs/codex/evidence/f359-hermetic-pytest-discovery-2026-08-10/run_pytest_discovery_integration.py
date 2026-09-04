#!/usr/bin/env python3
"""Recompute the F359 pytest-discovery boundary from production entry points."""

from __future__ import annotations

import configparser
import json
import os
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[4]
QSYM_TESTS = (
    ROOT / "runtime" / "src" / "backends" / "qsym" / "qsym" / "tests"
)
COLLECTION_RE = re.compile(r"(?P<count>\d+) tests collected(?:, \d+ errors)? in")


def run_pytest(*args: str) -> tuple[int, str]:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", *args],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=30,
        check=False,
    )
    return completed.returncode, completed.stdout


def collected_count(output: str) -> int:
    match = COLLECTION_RE.search(output)
    return int(match.group("count")) if match else -1


def main() -> int:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(ROOT / "pytest.ini", encoding="utf-8")
    pytest_section = parser["pytest"]
    testpaths = tuple(pytest_section.get("testpaths", "").split())
    addopts = tuple(pytest_section.get("addopts", "").split())
    norecursedirs = tuple(pytest_section.get("norecursedirs", "").split())

    project_modules = tuple(sorted(path.name for path in (ROOT / "test").glob("test_*.py")))
    qsym_modules = tuple(sorted(path.name for path in QSYM_TESTS.glob("test_*.py")))

    default_rc, default_output = run_pytest("--collect-only", "-q")
    counterfactual_rc, counterfactual_output = run_pytest(
        "--collect-only", "-q", "-o", "testpaths="
    )
    qsym_rc, qsym_output = run_pytest(
        "--collect-only", "-q", str(QSYM_TESTS / "test_utils.py")
    )
    project_rc, project_output = run_pytest(
        "--collect-only", "-q", "test/test_self_config.py"
    )

    default_count = collected_count(default_output)
    counterfactual_count = collected_count(counterfactual_output)
    qsym_error_modules = tuple(
        name for name in qsym_modules if f"tests/{name}" in counterfactual_output
    )
    qsym_dependency_state = (
        "prerequisite-missing"
        if "No module named 'qsym'" in qsym_output
        else "collectable"
        if qsym_rc == 0
        else "other-error"
    )

    checks = {
        "config_scopes_default_to_project_test_root": testpaths == ("test",),
        "config_does_not_globally_ignore_qsym": not any(
            "qsym" in value.lower() for value in (*addopts, *norecursedirs)
        )
        and not any(value.startswith("--ignore") for value in addopts),
        "source_suites_are_distinct_and_present": bool(project_modules)
        and bool(qsym_modules)
        and (ROOT / "test") not in QSYM_TESTS.parents,
        "default_collection_succeeds": default_rc == 0,
        "default_collection_has_project_tests": default_count > 0,
        "default_collection_excludes_vendored_qsym": (
            "runtime/src/backends/qsym/qsym/tests/" not in default_output
        ),
        "counterfactual_without_testpaths_reproduces_failure": (
            counterfactual_rc == 2 and "6 errors during collection" in counterfactual_output
        ),
        "boundary_changes_scope_not_project_count": (
            default_count > 0 and default_count == counterfactual_count
        ),
        "counterfactual_identifies_all_six_qsym_modules": (
            len(qsym_modules) == 6 and qsym_error_modules == qsym_modules
        ),
        "explicit_qsym_path_is_not_hidden": "tests/test_utils.py" in qsym_output,
        "explicit_qsym_prerequisite_is_transparent": qsym_dependency_state
        in {"prerequisite-missing", "collectable"},
        "explicit_project_path_still_collects": (
            project_rc == 0
            and "test_self_config.py" in project_output
            and collected_count(project_output) > 0
        ),
    }

    payload = {
        "schema": "symcc-f359-hermetic-pytest-discovery-evidence-v1",
        "feature": "F359",
        "configuration": {
            "testpaths": list(testpaths),
            "addopts": list(addopts),
            "norecursedirs": list(norecursedirs),
        },
        "inventory": {
            "project_test_modules": len(project_modules),
            "vendored_qsym_test_modules": len(qsym_modules),
        },
        "observations": {
            "default": {
                "returncode": default_rc,
                "collected": default_count,
                "qsym_paths_observed": False,
            },
            "counterfactual_without_testpaths": {
                "returncode": counterfactual_rc,
                "collected": counterfactual_count,
                "collection_errors": len(qsym_error_modules),
            },
            "explicit_qsym": {
                "returncode": qsym_rc,
                "dependency_state": qsym_dependency_state,
            },
            "explicit_project": {
                "returncode": project_rc,
                "collected": collected_count(project_output),
            },
        },
        "checks": checks,
        "passed": sum(checks.values()),
        "total": len(checks),
    }
    print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
