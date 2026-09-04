#!/usr/bin/env python3
"""Generate reproducible evidence for bounded SOTA-oriented SymCC features."""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping

from distributed_state import LiveContinuationDescriptor, LiveStateStore
from schedule_exploration import (
    condpor_execution_graph_certificate,
    parse_schedule_trace,
    schedule_smt_artifact,
    solve_joint_path_schedule_query,
    solve_schedule_smt_query,
    source_dpor_certificate,
    verify_condpor_execution_graph_certificate,
    verify_joint_path_schedule_result,
    verify_source_dpor_certificate,
    verify_wakeup_tree_certificate,
    wakeup_tree_certificate,
)


EVIDENCE_SCHEMA_V1 = "symcc-research-evidence-v1"
EVIDENCE_SCHEMA = "symcc-research-evidence-v2"


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def evidence_digest(evidence: Mapping[str, Any]) -> str:
    return _digest(_canonical({
        key: value
        for key, value in evidence.items()
        if key != "evidence_sha256"
    }))


def _status_from_output(output: str) -> str:
    match = re.search(r"(?m)^\s*(sat|unsat|unknown)\b", output)
    return match.group(1) if match else "error"


def _libz3_version(library_path: str) -> str:
    try:
        library = ctypes.CDLL(library_path)
        library.Z3_get_full_version.restype = ctypes.c_char_p
        value = library.Z3_get_full_version()
        return value.decode("utf-8", errors="replace") if value else ""
    except (AttributeError, OSError):
        return ""


def _solve_with_libz3(smt2: str) -> dict[str, Any]:
    library_path = ctypes.util.find_library("z3")
    if not library_path:
        return {
            "backend": "system-libz3-eval",
            "family": "z3",
            "available": False,
            "status": "unavailable",
        }
    started = time.monotonic_ns()
    library = ctypes.CDLL(library_path)
    void = ctypes.c_void_p
    library.Z3_mk_config.restype = void
    library.Z3_mk_context.argtypes = [void]
    library.Z3_mk_context.restype = void
    library.Z3_eval_smtlib2_string.argtypes = [void, ctypes.c_char_p]
    library.Z3_eval_smtlib2_string.restype = ctypes.c_char_p
    library.Z3_del_context.argtypes = [void]
    library.Z3_del_config.argtypes = [void]
    config = library.Z3_mk_config()
    context = library.Z3_mk_context(config)
    try:
        raw = library.Z3_eval_smtlib2_string(
            context, smt2.encode("utf-8")
        )
        output = raw.decode("utf-8", errors="replace") if raw else ""
    finally:
        library.Z3_del_context(context)
        library.Z3_del_config(config)
    return {
        "backend": "system-libz3-eval",
        "family": "z3",
        "available": True,
        "version": _libz3_version(library_path),
        "status": _status_from_output(output),
        "elapsed_us": (time.monotonic_ns() - started) // 1000,
        "output_sha256": _digest(output.encode("utf-8")),
    }


def _solve_with_command(
    executable: str,
    family: str,
    smt2: str,
    *,
    timeout: float,
) -> dict[str, Any]:
    path = shutil.which(executable)
    if not path:
        return {
            "backend": executable + "-cli",
            "family": family,
            "available": False,
            "status": "unavailable",
        }
    command = (
        [path, "-in"]
        if executable == "z3"
        else [path, "--lang", "smt2"]
    )
    started = time.monotonic_ns()
    try:
        completed = subprocess.run(
            command,
            input=smt2,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=max(0.1, float(timeout)),
        )
        output = completed.stdout
        status = _status_from_output(output)
    except subprocess.TimeoutExpired:
        completed = None
        output = ""
        status = "timeout"
    return {
        "backend": executable + "-cli",
        "family": family,
        "available": True,
        "status": status,
        "returncode": (
            completed.returncode if completed is not None else None
        ),
        "elapsed_us": (time.monotonic_ns() - started) // 1000,
        "output_sha256": _digest(output.encode("utf-8")),
    }


def validate_smt_backends(
    smt2: str,
    *,
    expected: str,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Run all available solver families and record agreement honestly."""
    results = [
        _solve_with_libz3(smt2),
        _solve_with_command("z3", "z3", smt2, timeout=timeout),
        _solve_with_command("cvc5", "cvc5", smt2, timeout=timeout),
    ]
    available = [
        result for result in results if result["available"]
    ]
    statuses = {
        str(result["status"])
        for result in available
        if result["status"] in {"sat", "unsat", "unknown"}
    }
    families = {
        str(result["family"])
        for result in available
        if result["status"] in {"sat", "unsat"}
    }
    return {
        "smt2_sha256": _digest(smt2.encode("utf-8")),
        "expected": expected,
        "results": results,
        "available_backend_count": len(available),
        "available_solver_families": sorted(families),
        "backend_disagreement": len(statuses) > 1,
        "independent_family_consensus": (
            len(families) >= 2
            and statuses == {expected}
        ),
        "expected_oracle_passed": bool(available) and all(
            result["status"] == expected for result in available
        ),
    }


def _memory_case(
    name: str,
    events: str,
    memory_model: str,
    outcome_smt2: str,
    expected: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    artifact = schedule_smt_artifact(
        parse_schedule_trace(events),
        memory_model=memory_model,
    )
    observed = solve_schedule_smt_query(
        artifact,
        None,
        extra_smt2=outcome_smt2,
    )
    smt2 = artifact["base_smt2"] + outcome_smt2 + "(check-sat)\n"
    obligation = {
        "id": name,
        "kind": "memory-model-litmus",
        "memory_model": memory_model,
        "trace_sha256": artifact["trace_digest"],
        "artifact_sha256": artifact["artifact_sha256"],
        "bounds": dict(artifact["bounds"]),
        "expected": expected,
        "observed": observed["status"],
        "passed": observed["status"] == expected,
    }
    return obligation, validate_smt_backends(smt2, expected=expected)


def build_research_evidence(
    work_root: str | os.PathLike[str],
) -> dict[str, Any]:
    """Execute bounded proof obligations and return an archival manifest."""
    root = Path(work_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    obligations: list[dict[str, Any]] = []
    backend_cases: list[dict[str, Any]] = []

    independent_a = source_dpor_certificate(parse_schedule_trace(
        "0 1 write x\n1 2 write y\n"
    ))
    independent_b = source_dpor_certificate(parse_schedule_trace(
        "0 2 write y\n1 1 write x\n"
    ))
    dependent = source_dpor_certificate(parse_schedule_trace(
        "0 1 write x\n1 2 read x\n"
    ))
    source_passed = (
        verify_source_dpor_certificate(independent_a)
        and verify_source_dpor_certificate(independent_b)
        and verify_source_dpor_certificate(dependent)
        and independent_a["dependency_graph"]["equivalence_sha256"]
        == independent_b["dependency_graph"]["equivalence_sha256"]
        and independent_a["dependency_graph"]["equivalence_sha256"]
        != dependent["dependency_graph"]["equivalence_sha256"]
    )
    obligations.append({
        "id": "f56-bounded-mazurkiewicz-certificate",
        "kind": "certificate",
        "expected": "equivalent-independent-distinct-dependent",
        "observed": (
            "equivalent-independent-distinct-dependent"
            if source_passed else "mismatch"
        ),
        "certificate_sha256": independent_a["certificate_sha256"],
        "optimality_claimed": independent_a["optimality_claimed"],
        "passed": source_passed,
    })

    path_smt2 = (
        "(set-logic QF_BV)\n"
        "(declare-fun |0| () (_ BitVec 8))\n"
        "(assert (= |0| #x42))\n"
        "(check-sat)\n"
    )
    joint_artifact = schedule_smt_artifact(parse_schedule_trace(
        "0 1 write x value=0x99\n"
        "1 2 read x sym-byte=0 init=0x42\n"
    ))
    joint = solve_joint_path_schedule_query(
        joint_artifact,
        path_smt2,
        query_id="evidence",
    )
    joint_passed = (
        joint["status"] == "sat"
        and joint.get("model", {}).get("input_bytes") == {"0": 66}
        and verify_joint_path_schedule_result(
            joint_artifact, path_smt2, joint
        )
    )
    obligations.append({
        "id": "f57-joint-path-schedule-rf",
        "kind": "joint-solver-certificate",
        "expected": "sat-byte66-rf-init",
        "observed": (
            "sat-byte66-rf-init" if joint_passed else joint["status"]
        ),
        "result_sha256": joint["result_sha256"],
        "value_bridge_count": joint["read_from"]["value_bridge_count"],
        "passed": joint_passed,
    })

    store_buffering = (
        "0 1 write x\n"
        "1 1 read y\n"
        "2 2 write y\n"
        "3 2 read x\n"
    )
    outcome = (
        "(assert (= rf_1 (- 1)))\n"
        "(assert (= rf_3 (- 1)))\n"
    )
    for name, model, expected in (
        ("f58-sb-sc", "SC", "unsat"),
        ("f58-sb-tso", "TSO", "sat"),
        ("f58-sb-ra", "RA", "sat"),
    ):
        obligation, backend = _memory_case(
            name, store_buffering, model, outcome, expected
        )
        obligations.append(obligation)
        backend_cases.append({"id": name, **backend})

    live_root = root / "live-state"
    state_store = LiveStateStore(str(live_root), page_size=64)
    expression = state_store.put_expression({
        "op": "equal", "children": ["input[0]", 66],
    })
    solver_root = state_store.put_solver_frame("", [expression])
    symbolic_store = state_store.put_symbolic_store({"x": expression})
    memory = state_store.create_memory(b"A" * 128, {0: expression})
    forked = state_store.fork_memory(
        memory,
        concrete_writes={70: 66},
    )
    descriptor = LiveContinuationDescriptor.from_mapping({
        "schema": "symcc-live-continuation-v1",
        "engine": "symcc",
        "frames": [{
            "function": "evidence",
            "block": "fork",
            "instruction": 1,
            "call_depth": 0,
        }],
        "path_condition_root": solver_root,
        "symbolic_store_root": symbolic_store,
        "symbolic_memory_root": forked,
    })
    if descriptor is None:
        raise AssertionError("cannot construct evidence continuation")
    checkpoint_id = state_store.put_continuation(descriptor)
    bundle = state_store.restore_continuation(checkpoint_id)
    cow_passed = (
        state_store.memory_diff(memory, forked) == (1,)
        and dict(state_store.memory_pages(memory))[0]
        == dict(state_store.memory_pages(forked))[0]
        and bundle.checkpoint_id == checkpoint_id
        and bundle.solver_frames == ((expression,),)
    )
    obligations.append({
        "id": "f59-live-state-cow-restore",
        "kind": "content-addressed-state",
        "expected": "one-page-diff-and-restorable",
        "observed": (
            "one-page-diff-and-restorable" if cow_passed else "mismatch"
        ),
        "checkpoint_id": checkpoint_id,
        "native_resume_supported": False,
        "passed": cow_passed,
    })

    wakeup = wakeup_tree_certificate(parse_schedule_trace(
        "0 2 ready x decision=0 chosen=2 tids=1,2 "
        "complete=1 prefix=1 fallback=0\n"
        "1 1 read x init=0\n"
        "2 2 write x value=1\n"
    ))
    wakeup_passed = (
        verify_wakeup_tree_certificate(wakeup)
        and wakeup["ready_evidence_complete"]
        and len(wakeup["insert_attempts"]) == 1
        and wakeup["insert_attempts"][0]["inserted"]
        and wakeup["optimality_claimed"] is False
    )
    obligations.append({
        "id": "f61-bounded-wakeup-ready-certificate",
        "kind": "wakeup-tree-certificate",
        "expected": "ready-root-inserted-and-verified",
        "observed": (
            "ready-root-inserted-and-verified"
            if wakeup_passed else "mismatch"
        ),
        "certificate_sha256": wakeup["certificate_sha256"],
        "ready_evidence_complete": wakeup["ready_evidence_complete"],
        "optimality_claimed": wakeup["optimality_claimed"],
        "passed": wakeup_passed,
    })

    execution_graph = condpor_execution_graph_certificate(
        parse_schedule_trace(
            "0 1 read x init=0\n"
            "1 1 constraint branch outcome=0 model-outcome=1\n"
            "2 2 write x value=1\n"
        )
    )
    condpor_passed = (
        verify_condpor_execution_graph_certificate(execution_graph)
        and execution_graph["causal_acyclic"]
        and execution_graph["revisit_count"] == 1
        and execution_graph["revisits"][0]["extension"][
            "deleted_events"
        ] == ["t1:1"]
        and execution_graph["sound_complete_optimal_claimed"] is False
    )
    obligations.append({
        "id": "f62-bounded-condpor-backward-revisit",
        "kind": "execution-graph-certificate",
        "expected": "acyclic-single-revisit-maximal-extension",
        "observed": (
            "acyclic-single-revisit-maximal-extension"
            if condpor_passed else "mismatch"
        ),
        "certificate_sha256": execution_graph["certificate_sha256"],
        "execution_graph_sha256": (
            execution_graph["execution_graph_sha256"]
        ),
        "revisit_sha256": (
            execution_graph["revisits"][0]["revisit_sha256"]
            if execution_graph["revisits"] else ""
        ),
        "sound_complete_optimal_claimed": (
            execution_graph["sound_complete_optimal_claimed"]
        ),
        "passed": condpor_passed,
    })

    claims = [
        {
            "feature": "F56",
            "supported_scope": "bounded observed-trace reduction certificate",
            "excluded_claims": [
                "unbounded completeness", "optimal DPOR",
            ],
        },
        {
            "feature": "F57",
            "supported_scope": (
                "single-context path+schedule+rf satisfiability"
            ),
            "excluded_claims": [
                "runtime reachability without enabledness evidence",
            ],
        },
        {
            "feature": "F58",
            "supported_scope": "bounded SC/TSO/C11-RA consistency encoding",
            "excluded_claims": [
                "full C11", "fences", "undefined non-atomic races",
            ],
        },
        {
            "feature": "F59",
            "supported_scope": "serializable CAS/COW resume state contract",
            "excluded_claims": [
                "native arbitrary-instruction process resume",
            ],
        },
        {
            "feature": "F61",
            "supported_scope": (
                "bounded weak-initial wakeup-tree invariants with "
                "cooperative ready evidence"
            ),
            "excluded_claims": [
                "complete operational enabledness",
                "unbounded Optimal-DPOR",
            ],
        },
        {
            "feature": "F62",
            "supported_scope": (
                "bounded observed-control-flow po/rf/co backward revisit"
            ),
            "excluded_claims": [
                "path-dependent event existence",
                "ConDPOR soundness completeness optimality",
            ],
        },
    ]
    environment = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "libz3": ctypes.util.find_library("z3") or "",
        "available_commands": {
            name: shutil.which(name) or ""
            for name in ("z3", "cvc5")
        },
    }
    passed = (
        all(obligation["passed"] for obligation in obligations)
        and all(
            case["expected_oracle_passed"]
            and not case["backend_disagreement"]
            for case in backend_cases
        )
    )
    evidence: dict[str, Any] = {
        "schema": EVIDENCE_SCHEMA,
        "generated_unix_ns": time.time_ns(),
        "environment": environment,
        "protocol": {
            "runs_per_logical_obligation": 1,
            "solver_timeout_seconds": 10,
            "random_seed": 0,
            "deterministic_inputs": True,
            "performance_claims": False,
            "backend_consensus_required_for_expected_oracle": False,
            "independent_repetition_required_for_paper_results": True,
        },
        "obligations": obligations,
        "backend_cases": backend_cases,
        "claim_scope": claims,
        "passed": passed,
    }
    evidence["evidence_sha256"] = evidence_digest(evidence)
    return evidence


def verify_research_evidence(evidence: Mapping[str, Any]) -> bool:
    try:
        schema = evidence.get("schema")
        if schema not in {EVIDENCE_SCHEMA_V1, EVIDENCE_SCHEMA}:
            return False
        if evidence.get("evidence_sha256") != evidence_digest(evidence):
            return False
        obligations = evidence.get("obligations")
        backend_cases = evidence.get("backend_cases")
        claims = evidence.get("claim_scope")
        if not all(isinstance(value, list) for value in (
            obligations, backend_cases, claims
        )):
            return False
        if not obligations or not all(
            obligation.get("passed") is True
            and obligation.get("expected") == obligation.get("observed")
            for obligation in obligations
        ):
            return False
        if any(case.get("backend_disagreement") for case in backend_cases):
            return False
        if not all(
            case.get("expected_oracle_passed") is True
            for case in backend_cases
        ):
            return False
        by_feature = {
            str(claim["feature"]): claim for claim in claims
        }
        expected_features = {"F56", "F57", "F58", "F59"}
        if schema == EVIDENCE_SCHEMA:
            expected_features.update({"F61", "F62"})
        if set(by_feature) != expected_features:
            return False
        if "optimal DPOR" not in by_feature["F56"]["excluded_claims"]:
            return False
        if "full C11" not in by_feature["F58"]["excluded_claims"]:
            return False
        if (
            "native arbitrary-instruction process resume"
            not in by_feature["F59"]["excluded_claims"]
        ):
            return False
        if schema == EVIDENCE_SCHEMA:
            if (
                "unbounded Optimal-DPOR"
                not in by_feature["F61"]["excluded_claims"]
            ):
                return False
            if (
                "path-dependent event existence"
                not in by_feature["F62"]["excluded_claims"]
            ):
                return False
        return evidence.get("passed") is True
    except (KeyError, TypeError, ValueError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--work-root", default=".symcc-research-evidence",
    )
    parser.add_argument("--output", default="")
    parser.add_argument(
        "--verify", default="",
        help="Verify an existing evidence JSON file instead of running",
    )
    args = parser.parse_args()
    if args.verify:
        evidence = json.loads(Path(args.verify).read_text(encoding="ascii"))
        verified = verify_research_evidence(evidence)
        print(json.dumps({"verified": verified}, sort_keys=True))
        return 0 if verified else 1
    evidence = build_research_evidence(args.work_root)
    payload = _canonical(evidence) + b"\n"
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(
            output.name + f".{os.getpid()}.tmp"
        )
        temporary.write_bytes(payload)
        os.replace(temporary, output)
    sys.stdout.buffer.write(payload)
    return 0 if verify_research_evidence(evidence) else 1


if __name__ == "__main__":
    raise SystemExit(main())
