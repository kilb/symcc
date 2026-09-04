# RUN: python3 %s

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from executor_portfolio import ExecutorPortfolio  # noqa: E402


class ExecutorPortfolioTests(unittest.TestCase):
    def test_routes_executor_to_independent_engine_and_target(self):
        portfolio = ExecutorPortfolio(
            ["default", "@@"],
            configuration={
                "exact": {
                    "engine": "symsan",
                    "target": ["exact-driver", "@@"],
                    "timeout_scale": 2,
                    "env": {"SYMSAN_SOLVER": "rgd", "PATH": "/bad"},
                },
            },
        )
        route = portfolio.resolve("exact", 30)
        self.assertEqual(route.engine, "symsan")
        self.assertEqual(route.command, ("exact-driver", "@@"))
        self.assertEqual(route.timeout_sec, 60)
        self.assertFalse(route.use_stdin)
        self.assertEqual(route.environment, {"SYMSAN_SOLVER": "rgd"})

    def test_environment_configuration_accepts_json_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "portfolio.json"
            path.write_text(json.dumps({
                "executors": {
                    "sampling": {
                        "engine": "symcc",
                        "target": "sample-runner --input @@",
                        "use_stdin": False,
                    }
                }
            }), encoding="utf-8")
            with mock.patch.dict(
                    os.environ, {"SYMCC_EXECUTOR_PORTFOLIO": str(path)},
                    clear=False):
                portfolio = ExecutorPortfolio.from_environment(["fallback"])
            route = portfolio.resolve("sampling", 10)
            self.assertEqual(
                route.command, ("sample-runner", "--input", "@@"))
            self.assertFalse(route.use_stdin)

    def test_invalid_route_falls_back_to_current_target(self):
        portfolio = ExecutorPortfolio(
            ["target"], configuration={"exact": {"engine": "invalid"}})
        route = portfolio.resolve("unknown", 0)
        self.assertEqual(route.engine, "symcc")
        self.assertEqual(route.command, ("target",))
        self.assertEqual(route.timeout_sec, 1)


if __name__ == "__main__":
    unittest.main()
