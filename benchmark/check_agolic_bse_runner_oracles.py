#!/usr/bin/env python3
"""Finite source-level oracle for the executable Agolic BSE adapter."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from agolic_bse_runner import (  # noqa: E402
    AgolicBSERunnerError,
    AgolicContinuationBSERunner,
)
from agolic_planning import AgolicRunLevelPlanner  # noqa: E402


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def program() -> dict[str, Any]:
    return {
        "schema": "symcc-live-program-v1",
        "entry": "main",
        "input_size": 2,
        "functions": {
            "main": {
                "entry": "entry",
                "blocks": {
                    "entry": [
                        {"op": "input", "dst": "route", "offset": 0},
                        {"op": "input", "dst": "payload", "offset": 1},
                        {
                            "op": "binary", "operator": "eq",
                            "dst": "setup_ok", "left": {"var": "route"},
                            "right": {"const": 65, "bits": 8}, "bits": 1,
                        },
                        {
                            "op": "branch", "condition": {"var": "setup_ok"},
                            "true": "ready", "false": "reject", "site": 8001,
                        },
                    ],
                    "ready": [
                        {
                            "op": "call", "function": "release",
                            "args": [{"var": "payload"}], "dst": "result",
                        },
                        {"op": "return", "value": {"var": "result"}},
                    ],
                    "reject": [{"op": "return", "value": 3}],
                },
            },
            "release": {
                "entry": "entry",
                "params": ["payload"],
                "blocks": {
                    "entry": [
                        {
                            "op": "binary", "operator": "eq",
                            "dst": "is_target", "left": {"var": "payload"},
                            "right": {"const": 66, "bits": 8}, "bits": 1,
                        },
                        {
                            "op": "branch", "condition": {"var": "is_target"},
                            "true": "target", "false": "other", "site": 9001,
                        },
                    ],
                    "target": [{"op": "return", "value": 1}],
                    "other": [{"op": "return", "value": 2}],
                },
            },
        },
    }


def source_replay(content: bytes) -> dict[str, Any]:
    route = content[0] if content else 0
    payload = content[1] if len(content) > 1 else 0
    outcomes = {(8001, route == 65)}
    functions = {"main"}
    value = 3
    if route == 65:
        functions.add("release")
        outcomes.add((9001, payload == 66))
        value = 1 if payload == 66 else 2
    return {
        "functions": functions,
        "outcomes": outcomes,
        "value": value,
    }


def make_plan(
    program_sha256: str,
    witness_path: Path,
    witness: bytes,
) -> tuple[AgolicRunLevelPlanner, dict[str, Any]]:
    planner = AgolicRunLevelPlanner(
        None,
        experiment_id="f423-finite-oracle",
        program_id="agolic-release-fixture",
        program_sha256=program_sha256,
        profiles={
            "bounded": {
                "modes": ["witness-guided"],
                "time_limit_seconds": 5.0,
                "memory_limit_mib": 512,
            }
        },
    )
    plans, _diagnostics = planner.plan_round([{
        "target_id": "release-target",
        "target_branch": 9001,
        "source_file": "fixture.c",
        "function": "release",
        "line": 12,
        "distance": 1.0,
        "opportunity": "finite source oracle",
        "modes": ["witness-guided"],
        "witnesses": [{
            "sha256": digest(witness),
            "path": str(witness_path),
            "release_function": "release",
            "release_branch": 0,
            "provenance": "finite-source-oracle",
        }],
    }], max_runs=1)
    if len(plans) != 1:
        raise AssertionError("planner did not emit the finite-oracle plan")
    return planner, plans[0]


def require_rejection(action) -> None:
    try:
        action()
    except (AgolicBSERunnerError, ValueError):
        return
    raise AssertionError("mutation was accepted")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    configurations = 0
    released = 0
    unreleased = 0
    candidates = 0
    source_replay_equivalences = 0
    planner_outcomes = 0
    true_targets = 0
    false_targets = 0
    mutation_rejections = 0
    routes = (65, 90)
    payloads = (0, 65, 66, 255)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        encoded = json.dumps(
            program(), sort_keys=True, separators=(",", ":")
        ).encode("ascii")
        program_path = root / "program.json"
        program_path.write_bytes(encoded)
        program_sha256 = digest(encoded)
        last_runner = None
        last_plan = None
        last_result = None
        for route in routes:
            for payload in payloads:
                configurations += 1
                case = root / f"case-{route}-{payload}"
                case.mkdir()
                witness = bytes((route, payload))
                witness_path = case / "witness"
                witness_path.write_bytes(witness)
                planner, plan = make_plan(
                    program_sha256, witness_path, witness
                )
                runner = AgolicContinuationBSERunner(
                    program_path,
                    case / "campaign",
                    program_sha256=program_sha256,
                    max_steps=128,
                    max_states=16,
                    max_artifacts=8,
                )
                result = runner.execute_plan(plan)
                expected_release = route == 65
                if result["release_reached"] is not expected_release:
                    raise AssertionError("release reach differs from source model")
                if expected_release:
                    released += 1
                    if len(result["candidates"]) != 2:
                        raise AssertionError("released run lacks both target outcomes")
                else:
                    unreleased += 1
                    if result["candidates"]:
                        raise AssertionError("pre-release run emitted a candidate")
                observed_target: set[bool] = set()
                for candidate in result["candidates"]:
                    path = (
                        runner.runs / plan["plan_id"]
                        / candidate["relative_path"]
                    )
                    content = path.read_bytes()
                    source = source_replay(content)
                    if source["functions"] != {"main", "release"}:
                        raise AssertionError("candidate violates witness route")
                    target_taken = (9001, True) in source["outcomes"]
                    observed_target.add(target_taken)
                    source_replay_equivalences += 1
                    candidates += 1
                if expected_release and observed_target != {False, True}:
                    raise AssertionError("candidate set misses a target outcome")
                true_targets += int(True in observed_target)
                false_targets += int(False in observed_target)
                outcome = runner.replay_result(plan, result)
                recorded = planner.record_outcome(plan["plan_id"], outcome)
                expected_class = "new-reach" if expected_release else "not-reached"
                if recorded["target_class"] != expected_class:
                    raise AssertionError("planner classification differs from source")
                planner_outcomes += 1
                last_runner, last_plan, last_result = runner, plan, result

        assert last_runner is not None
        assert last_plan is not None
        assert last_result is not None
        require_rejection(lambda: AgolicContinuationBSERunner(
            program_path,
            root / "wrong-program",
            program_sha256="0" * 64,
        ))
        mutation_rejections += 1

        witness_mutation = copy.deepcopy(last_plan)
        witness_mutation["specification"]["witness"]["sha256"] = "0" * 64
        require_rejection(lambda: last_runner.execute_plan(witness_mutation))
        mutation_rejections += 1

        result_mutation = copy.deepcopy(last_result)
        result_mutation["release_reached"] = not result_mutation["release_reached"]
        require_rejection(lambda: last_runner.replay_result(
            last_plan, result_mutation
        ))
        mutation_rejections += 1

        path_mutation = copy.deepcopy(last_result)
        if path_mutation["candidates"]:
            path_mutation["candidates"][0]["relative_path"] = "../escape"
            require_rejection(lambda: last_runner.replay_result(
                last_plan, path_mutation
            ))
        else:
            forged = {
                "sha256": "0" * 64,
                "size": 1,
                "checkpoint": "0" * 64,
                "relative_path": "../escape",
            }
            path_mutation["candidates"] = [forged]
            path_mutation["release_reached"] = True
            path_mutation["execution"]["witness_guidance"][
                "release_reached"
            ] = True
            require_rejection(lambda: last_runner.replay_result(
                last_plan, path_mutation
            ))
        mutation_rejections += 1

        resource_mutation = copy.deepcopy(last_result)
        resource_mutation["resource_limits"]["max_steps"] += 1
        require_rejection(lambda: last_runner.replay_result(
            last_plan, resource_mutation
        ))
        mutation_rejections += 1

        mode_mutation = copy.deepcopy(last_result)
        mode_mutation["mode"] = "harness-entry"
        require_rejection(lambda: last_runner.replay_result(
            last_plan, mode_mutation
        ))
        mutation_rejections += 1

    summary = {
        "schema": "symcc-agolic-bse-finite-oracle-v1",
        "all_passed": True,
        "configurations": configurations,
        "released_configurations": released,
        "unreleased_configurations": unreleased,
        "candidates": candidates,
        "source_replay_equivalences": source_replay_equivalences,
        "planner_outcomes": planner_outcomes,
        "target_true_sets": true_targets,
        "target_false_sets": false_targets,
        "mutation_rejections": mutation_rejections,
        "claim_boundary": (
            "Finite two-byte continuation fixture with source-level replay; "
            "not native KLEE equivalence, arbitrary external effects, "
            "coverage speedup, or public-campaign evidence"
        ),
    }
    if summary != {
        "schema": "symcc-agolic-bse-finite-oracle-v1",
        "all_passed": True,
        "configurations": 8,
        "released_configurations": 4,
        "unreleased_configurations": 4,
        "candidates": 8,
        "source_replay_equivalences": 8,
        "planner_outcomes": 8,
        "target_true_sets": 4,
        "target_false_sets": 4,
        "mutation_rejections": 6,
        "claim_boundary": (
            "Finite two-byte continuation fixture with source-level replay; "
            "not native KLEE equivalence, arbitrary external effects, "
            "coverage speedup, or public-campaign evidence"
        ),
    }:
        raise AssertionError(f"unexpected finite-oracle summary: {summary}")
    encoded = json.dumps(summary, sort_keys=True, separators=(",", ":"))
    if args.output:
        Path(args.output).write_text(encoded + "\n", encoding="ascii")
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
