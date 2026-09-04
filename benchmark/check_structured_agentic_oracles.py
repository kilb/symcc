#!/usr/bin/env python3
"""Deterministic three-arm oracle for the F455 agentic control protocol."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shlex
import sys
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from agentic_concolic_hooks import CommandAgenticBackend  # noqa: E402
from structured_agentic_concolic import (  # noqa: E402
    RESPONSE_SCHEMA,
    StructuredAgenticController,
    StructuredAgenticPolicy,
    compare_ablation_ledgers,
)


PROMPT = "f455 deterministic structured protocol oracle"
PROMPT_SHA256 = hashlib.sha256(PROMPT.encode("ascii")).hexdigest()


def _write_backend(path: Path) -> None:
    path.write_text(
        "import json, sys\n"
        "request = json.load(sys.stdin)\n"
        "print(json.dumps({\n"
        f"  'schema': {RESPONSE_SCHEMA!r},\n"
        "  'request_id': request['request_id'],\n"
        "  'task_sha256': request['task_sha256'],\n"
        "  'actions': [\n"
        "    {'kind': 'schedule', 'strategy': 4, "
        "'target_branch': 77, 'focus_bytes': '0-1', "
        "'s2f_actions': [[77, 'solve']], 'route': 'cottontail'},\n"
        "    {'kind': 'candidate', 'proposal_kind': 'solve_complete', "
        "'data_hex': '7b7d', 'target_branch': 77},\n"
        "  ],\n"
        "  'usage': {'input_tokens': 0, 'output_tokens': 0},\n"
        "}, sort_keys=True))\n",
        encoding="ascii",
    )


def _policy(mode: str) -> StructuredAgenticPolicy:
    return StructuredAgenticPolicy(
        mode=mode,
        strategy_count=7,
        experiment_id="f455-three-arm-oracle",
        program_identity="f455-oracle-target-v1",
        trigger_mode="reactive",
        plateau_threshold=8,
        max_requests=8,
        max_input_tokens=131_072,
        max_output_tokens=32_768,
        max_model_time_us=30_000_000,
        max_input_tokens_per_request=16_384,
        max_output_tokens_per_request=4_096,
        timeout_ms=2_000,
    )


def _drain(controller: StructuredAgenticController) -> list[Any]:
    import time

    deadline = time.monotonic() + 5.0
    decisions = []
    while not decisions and time.monotonic() < deadline:
        decisions = controller.drain()
        if not decisions:
            time.sleep(0.005)
    if not decisions:
        raise RuntimeError("structured controller did not complete")
    return decisions


def run_oracle(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    backend_path = root / "fixed_backend.py"
    _write_backend(backend_path)
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(backend_path))}"
    ledger_paths: dict[str, Path] = {}
    arm_results: dict[str, list[dict[str, Any]]] = {}
    for mode in ("online", "shadow", "fallback"):
        backend = CommandAgenticBackend(
            command,
            name="fixed-command",
            provider="local-oracle",
            model="deterministic-f455-v1",
            prompt_sha256=PROMPT_SHA256,
        )
        ledger_path = root / f"{mode}.jsonl"
        controller = StructuredAgenticController(
            [backend], _policy(mode), ledger_path, workers=1)
        healthy_seed = root / "healthy-seed"
        healthy_seed.write_bytes(b"healthy")
        healthy_task = {
            "schema": 1,
            "input_path": str(healthy_seed),
            "sha256": hashlib.sha256(b"healthy").hexdigest(),
            "strategy": 0,
            "target_branch": 77,
            "open_branches": [77, 88],
            "symbolic_branches": 4,
            "generated": 2,
            "coverage_delta": 1,
        }
        if controller.submit(healthy_task):
            raise RuntimeError("productive task bypassed the reactive gate")
        decisions = []
        for index, content in enumerate((b"{}", b"[]", b"null")):
            seed = root / f"seed-{index}"
            seed.write_bytes(content)
            task = {
                "schema": 1,
                "input_path": str(seed),
                "sha256": hashlib.sha256(content).hexdigest(),
                "strategy": 0,
                "target_branch": 77,
                "open_branches": [77, 88],
                "focus_bytes": "0-1",
                "symbolic_branches": 4,
                "generated": 0,
                "coverage_delta": 0,
                "solver_unknown": 1,
            }
            controller.submit(task, fallback_hint={
                "strategy": 0,
                "target_branch": 77,
            })
            decision = _drain(controller)[0]
            decisions.append({
                "source": decision.source,
                "hint": decision.hint,
                "proposals": len(decision.proposals),
                "model_actions": len(decision.model_actions),
            })
        controller.close()
        ledger_paths[mode] = ledger_path
        arm_results[mode] = decisions
    comparison = compare_ablation_ledgers(ledger_paths)
    online = comparison["arms"]["online"]
    shadow = comparison["arms"]["shadow"]
    fallback = comparison["arms"]["fallback"]
    checks = {
        "task_set_equal": comparison["task_count"] == 3,
        "online_responses": online["requests"] == 3
        and online["valid_responses"] == 3,
        "shadow_responses": shadow["requests"] == 3
        and shadow["valid_responses"] == 3,
        "fallback_no_model_calls": fallback["requests"] == 0,
        "online_candidates_applied": online["candidate_actions"] == 3,
        "shadow_candidates_not_applied": shadow["candidate_actions"] == 0,
        "shadow_candidates_observed": shadow["proposed_candidate_actions"] == 3,
        "token_accounting_nonzero": online["input_tokens"] > 0
        and online["output_tokens"] > 0,
        "reactive_gate": all(
            arm["trigger_evaluations"] == 4
            and arm["triggered_requests"] == 3
            and arm["suppressed_requests"] == 1
            for arm in (online, shadow, fallback)
        ),
    }
    if not all(checks.values()):
        raise RuntimeError(f"F455 oracle failed: {checks}")
    result = {
        "schema": "symcc-f455-structured-agentic-oracle-v1",
        "checks": checks,
        "decisions": arm_results,
        "comparison": comparison,
    }
    result["result_sha256"] = hashlib.sha256(json.dumps(
        result, sort_keys=True, separators=(",", ":"),
    ).encode("ascii")).hexdigest()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir")
    parser.add_argument("--output")
    arguments = parser.parse_args()
    if arguments.workdir:
        result = run_oracle(Path(arguments.workdir))
    else:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_oracle(Path(tmp))
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if arguments.output:
        Path(arguments.output).write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
