#!/usr/bin/env python3
"""Bounded native ConDPOR campaign with model-checked execution graphs."""

from __future__ import annotations

from collections import deque
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import tempfile
import time
from typing import Any, Mapping, Sequence

from schedule_exploration import (
    ScheduleEvent,
    condpor_execution_graph_certificate,
    native_condpor_memory_graph_certificate,
    normalize_schedule_prefix,
    parse_schedule_trace,
    prepend_ld_preload,
    schedule_smt_artifact,
    schedule_trace_digest,
    solve_schedule_smt_query,
    verify_condpor_execution_graph_certificate,
    verify_native_condpor_memory_graph_certificate,
    write_schedule_prefix,
)


NATIVE_CONDPOR_CAMPAIGN_SCHEMA = "symcc-native-condpor-campaign-v1"
_MAX_CAPTURE_PREVIEW = 4096
_MAX_TRACE_BYTES_LIMIT = 256 * 1024 * 1024


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _certificate_digest(certificate: Mapping[str, Any]) -> str:
    return _canonical_digest({
        key: value
        for key, value in certificate.items()
        if key != "certificate_sha256"
    })


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _captured_file(path: Path) -> dict[str, Any]:
    size = path.stat().st_size
    with path.open("rb") as stream:
        preview = stream.read(_MAX_CAPTURE_PREVIEW)
    return {
        "bytes": size,
        "sha256": _file_digest(path),
        "preview": preview.decode("utf-8", errors="replace"),
        "preview_truncated": size > len(preview),
    }


def _trace_event_mapping(event: ScheduleEvent) -> dict[str, Any]:
    row: dict[str, Any] = {
        "seq": event.seq,
        "tid": event.tid,
        "op": event.op,
        "object": event.obj,
    }
    if event.tags:
        row["tags"] = list(event.tags)
    return row


def _events_from_rows(rows: Sequence[Mapping[str, Any]]) -> list[ScheduleEvent]:
    return [
        ScheduleEvent(
            seq=int(row["seq"]),
            tid=int(row["tid"]),
            op=str(row["op"]),
            obj=str(row["object"]),
            tags=tuple(str(tag) for tag in row.get("tags", ())),
        )
        for row in rows
    ]


def _event_tag(event: ScheduleEvent, key: str) -> str:
    prefix = f"{key}="
    return next(
        (tag[len(prefix):] for tag in event.tags if tag.startswith(prefix)),
        "",
    )


def _trace_protocol_status(
    events: Sequence[ScheduleEvent],
    prefix: tuple[int, ...],
    *,
    timed_out: bool,
) -> dict[str, Any]:
    schedulable = [event.tid for event in events if event.schedulable]
    fallback_count = sum(event.op == "fallback" for event in events)
    commit_mismatch_count = sum(
        event.op == "atomic_commit" and _event_tag(event, "mismatch") == "1"
        for event in events
    )
    pending_conflict_count = sum(
        event.op == "atomic_pending_conflict" for event in events
    )
    prefix_matched = tuple(schedulable[:len(prefix)]) == prefix
    valid = not (
        timed_out
        or fallback_count
        or commit_mismatch_count
        or pending_conflict_count
        or not prefix_matched
    )
    error = (
        "timeout" if timed_out
        else "runtime_fallback" if fallback_count
        else "atomic_commit_mismatch" if commit_mismatch_count
        else "atomic_pending_conflict" if pending_conflict_count
        else "prefix_mismatch" if not prefix_matched
        else ""
    )
    return {
        "valid_trace": valid,
        "trace_error": error,
        "fallback_count": fallback_count,
        "atomic_commit_mismatch_count": commit_mismatch_count,
        "atomic_pending_conflict_count": pending_conflict_count,
        "prefix_matched": prefix_matched,
        "schedulable_event_count": len(schedulable),
    }


def _atomic_commit_summary(
    events: Sequence[ScheduleEvent], memory_model: str
) -> dict[str, Any]:
    modeled = [
        event
        for event in events
        if event.memory or event.op in {"fence", "atomic_fence"}
    ]
    atomic = [
        event for event in modeled if _event_tag(event, "atomic") == "1"
    ]
    committed = [
        event
        for event in atomic
        if _event_tag(event, "commit-mode") == "1"
        and _event_tag(event, "commit-mismatch") == "0"
    ]
    all_modeled_atomic = bool(modeled) and len(atomic) == len(modeled)
    complete_commit_evidence = bool(atomic) and len(committed) == len(atomic)
    return {
        "modeled_memory_event_count": len(modeled),
        "atomic_memory_event_count": len(atomic),
        "committed_atomic_event_count": len(committed),
        "all_modeled_memory_events_atomic": all_modeled_atomic,
        "complete_two_phase_commit_evidence": complete_commit_evidence,
        "sc_reads_from_hardware_enforced": (
            memory_model == "SC"
            and all_modeled_atomic
            and complete_commit_evidence
        ),
    }


def _strict_trace(raw: str) -> list[ScheduleEvent]:
    source_rows = [
        line.strip()
        for line in raw.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    for line in source_rows:
        fields = line.split()
        if len(fields) < 4:
            raise ValueError("native trace contains a short row")
        try:
            sequence = int(fields[0], 0)
            tid = int(fields[1], 0)
        except ValueError as exc:
            raise ValueError("native trace contains a non-integer identity") from exc
        if sequence < 0 or tid < 0:
            raise ValueError("native trace contains a negative identity")
    events = parse_schedule_trace(source_rows)
    if len(events) != len(source_rows):
        raise ValueError("native trace parser did not preserve every row")
    sequences = [event.seq for event in events]
    if len(set(sequences)) != len(sequences):
        raise ValueError("native trace contains duplicate sequence ids")
    return events


def _tag_value(tags: Sequence[str], name: str) -> str:
    prefix = name + "="
    for tag in tags:
        if tag.startswith(prefix):
            return tag[len(prefix):]
    return ""


def _trace_equivalence_digest(events: Sequence[ScheduleEvent]) -> str:
    object_ids: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    stable_object_ops = {"action", "constraint", "fence"}
    kept_tags = {
        "atomic",
        "bits",
        "bytes",
        "failure-mo",
        "kind",
        "mo",
        "next",
        "normal",
        "operation",
        "outcome",
        "role",
        "success",
        "value",
    }
    for event in events:
        if event.op in stable_object_ops:
            obj = event.obj
        else:
            obj = object_ids.setdefault(
                event.obj, f"object-{len(object_ids)}"
            )
        tags = []
        for name in sorted(kept_tags):
            value = _tag_value(event.tags, name)
            if value:
                tags.append(f"{name}={value}")
        rows.append({
            "tid": event.tid,
            "op": event.op,
            "object": obj,
            "tags": tags,
        })
    return _canonical_digest(rows)


def _control_flow_digest(events: Sequence[ScheduleEvent]) -> str:
    rows = []
    for event in events:
        if event.op not in {"action", "constraint"}:
            continue
        rows.append({
            "tid": event.tid,
            "op": event.op,
            "site": event.obj,
            "outcome": _tag_value(event.tags, "outcome") or None,
            "next": _tag_value(event.tags, "next") or None,
        })
    return _canonical_digest(rows)


def _graph_summary(certificate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "certificate_sha256": certificate["certificate_sha256"],
        "status": certificate["status"],
        "bounded_exhaustive": certificate["bounded_exhaustive"],
        "candidate_space": certificate["candidate_space"],
        "enumerated_candidate_count": certificate[
            "enumerated_candidate_count"
        ],
        "status_counts": certificate["status_counts"],
        "graph_count": certificate["graph_count"],
        "graph_sha256s": [
            graph["graph_sha256"] for graph in certificate["graphs"]
        ],
        "fully_value_witnessed_graph_count": sum(
            bool(graph["value_evidence"]["fully_value_witnessed"])
            for graph in certificate["graphs"]
        ),
        "contradicted_graph_count": sum(
            int(graph["value_evidence"]["contradicted"]) > 0
            for graph in certificate["graphs"]
        ),
    }


def _observed_graph_summary(certificate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "certificate_sha256": certificate["certificate_sha256"],
        "execution_graph_sha256": certificate["execution_graph_sha256"],
        "revisit_count": certificate["revisit_count"],
        "revisit_sha256s": [
            revisit["revisit_sha256"]
            for revisit in certificate["revisits"]
        ],
        "truncated": certificate["truncated"],
    }


def _analyze_trace(
    events: list[ScheduleEvent],
    prefix: tuple[int, ...],
    *,
    memory_model: str,
    max_depth: int,
    max_window: int,
    max_successors: int,
    max_events: int,
    max_memory_events: int,
    max_graph_candidates: int,
) -> dict[str, Any]:
    commit_summary = _atomic_commit_summary(events, memory_model)
    memory_graph = native_condpor_memory_graph_certificate(
        events,
        memory_model=memory_model,
        max_events=max_events,
        max_memory_events=max_memory_events,
        max_candidates=max_graph_candidates,
    )
    if not verify_native_condpor_memory_graph_certificate(memory_graph):
        raise ValueError("native memory graph failed self-verification")
    observed_graph = condpor_execution_graph_certificate(
        events,
        current_prefix=prefix,
        max_depth=max_depth,
        max_window=max_window,
        max_prefixes=max_successors,
        max_events=max_events,
    )
    if not verify_condpor_execution_graph_certificate(observed_graph):
        raise ValueError("observed ConDPOR graph failed self-verification")
    schedule = schedule_smt_artifact(
        events,
        current_prefix=prefix,
        max_depth=max_depth,
        max_window=max_window,
        max_prefixes=max_successors,
        max_events=max_events,
        max_memory_events=max_memory_events,
        max_queries=max_successors,
        memory_model=memory_model,
    )
    queries: list[dict[str, Any]] = []
    successors: list[dict[str, Any]] = []
    for query_index, query in enumerate(schedule["queries"]):
        result = solve_schedule_smt_query(
            schedule,
            query_index,
            lazy_refinement=False,
        )
        row = {
            "query_index": query_index,
            "status": result["status"],
            "exact_semantics": result["exact_semantics"],
            "prefix": list(query["prefix"]),
            "conflict": query["conflict"],
            "solver_check_count": result["solver_check_count"],
        }
        queries.append(row)
        if result["status"] == "sat" and result["exact_semantics"]:
            successors.append({
                "prefix": list(query["prefix"]),
                "query_index": query_index,
                "conflict": query["conflict"],
                "memory_model": memory_model,
                "hook_order_replayable": True,
                "rf_hardware_enforced": commit_summary[
                    "sc_reads_from_hardware_enforced"
                ],
            })
    return {
        "memory_graph": _graph_summary(memory_graph),
        "observed_graph": _observed_graph_summary(observed_graph),
        "schedule_artifact_sha256": schedule["artifact_sha256"],
        "schedule_truncated": schedule["truncated"],
        "atomic_commit_protocol": commit_summary,
        "queries": queries,
        "successors": successors,
    }


def _execute_run(
    command: Sequence[str],
    *,
    cwd: Path,
    runtime: Path,
    prefix: tuple[int, ...],
    work: Path,
    timeout_seconds: float,
    max_trace_bytes: int,
    environment: Mapping[str, str],
    wait_ms: int,
) -> dict[str, Any]:
    prefix_path = work / "prefix.txt"
    trace_path = work / "trace.log"
    stdout_path = work / "stdout.log"
    stderr_path = work / "stderr.log"
    if not write_schedule_prefix(str(prefix_path), prefix):
        raise OSError("cannot write native replay prefix")
    env = os.environ.copy()
    env.update({str(key): str(value) for key, value in environment.items()})
    env.update({
        "LD_PRELOAD": prepend_ld_preload(
            str(runtime), env.get("LD_PRELOAD")
        ),
        "SYMCC_DPOR": "1",
        "SYMCC_SCHEDULE_MEMORY": "1",
        "SYMCC_SCHEDULE_TRACE": str(trace_path),
        "SYMCC_SCHEDULE_PREFIX": str(prefix_path),
        "SYMCC_SCHEDULE_WAIT_MS": str(wait_ms),
        "SYMCC_SCHEDULE_ATOMIC_COMMIT": "1",
    })
    started = time.monotonic_ns()
    timed_out = False
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        try:
            return_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            return_code = process.wait()
    elapsed_ns = time.monotonic_ns() - started
    result: dict[str, Any] = {
        "return_code": return_code,
        "timed_out": timed_out,
        "elapsed_ns": elapsed_ns,
        "stdout": _captured_file(stdout_path),
        "stderr": _captured_file(stderr_path),
        "trace_present": trace_path.is_file(),
    }
    if not trace_path.is_file():
        result.update({
            "valid_trace": False,
            "trace_error": "missing_trace",
            "trace_events": [],
        })
        return result
    trace_size = trace_path.stat().st_size
    result["trace_bytes"] = trace_size
    result["trace_sha256"] = _file_digest(trace_path)
    if trace_size > max_trace_bytes:
        result.update({
            "valid_trace": False,
            "trace_error": "trace_size_limit",
            "trace_events": [],
        })
        return result
    try:
        raw = trace_path.read_text(encoding="utf-8")
        events = _strict_trace(raw)
    except (OSError, UnicodeError, ValueError) as error:
        result.update({
            "valid_trace": False,
            "trace_error": f"invalid_trace:{error}",
            "trace_events": [],
        })
        return result
    protocol = _trace_protocol_status(events, prefix, timed_out=timed_out)
    result.update({
        **protocol,
        "trace_digest": schedule_trace_digest(events),
        "trace_equivalence_sha256": _trace_equivalence_digest(events),
        "control_flow_sha256": _control_flow_digest(events),
        "trace_events": [_trace_event_mapping(event) for event in events],
        "constraint_event_count": sum(
            event.op == "constraint" for event in events
        ),
        "action_event_count": sum(event.op == "action" for event in events),
        "atomic_value_event_count": sum(
            event.op == "atomic_value" for event in events
        ),
        "atomic_commit_event_count": sum(
            event.op == "atomic_commit" for event in events
        ),
    })
    return result


def run_native_condpor_campaign(
    command: Sequence[str],
    *,
    schedule_runtime: str | os.PathLike[str],
    cwd: str | os.PathLike[str] | None = None,
    memory_model: str = "SC",
    environment: Mapping[str, str] | None = None,
    max_runs: int = 32,
    max_prefixes: int = 256,
    max_successors_per_run: int = 64,
    max_depth: int = 64,
    max_window: int = 32,
    max_events: int = 64,
    max_memory_events: int = 32,
    max_graph_candidates: int = 4096,
    max_trace_bytes: int = 16 * 1024 * 1024,
    timeout_seconds: float = 10.0,
    replay_wait_ms: int = 1000,
) -> dict[str, Any]:
    if not command or any(not isinstance(item, str) or not item for item in command):
        raise ValueError("command must contain non-empty strings")
    runtime = Path(schedule_runtime).resolve(strict=True)
    if not runtime.is_file():
        raise ValueError("schedule runtime must be a regular file")
    working_directory = Path(cwd or os.getcwd()).resolve(strict=True)
    if not working_directory.is_dir():
        raise ValueError("campaign cwd must be a directory")
    run_cap = int(max_runs)
    prefix_cap = int(max_prefixes)
    successor_cap = int(max_successors_per_run)
    depth = int(max_depth)
    window = int(max_window)
    trace_cap = int(max_trace_bytes)
    wait_ms = int(replay_wait_ms)
    timeout = float(timeout_seconds)
    model = str(memory_model).strip().upper()
    event_cap = int(max_events)
    memory_event_cap = int(max_memory_events)
    graph_candidate_cap = int(max_graph_candidates)
    if model not in {"SC", "TSO", "RA"}:
        raise ValueError("memory_model must be one of SC, TSO, or RA")
    if run_cap < 1 or run_cap > 4096:
        raise ValueError("max_runs must be in [1, 4096]")
    if prefix_cap < 1 or prefix_cap > 65536:
        raise ValueError("max_prefixes must be in [1, 65536]")
    if successor_cap < 1 or successor_cap > 512:
        raise ValueError("max_successors_per_run must be in [1, 512]")
    if depth < 1 or depth > 4096 or window < 1 or window > 4096:
        raise ValueError("max_depth and max_window must be in [1, 4096]")
    if trace_cap < 1 or trace_cap > _MAX_TRACE_BYTES_LIMIT:
        raise ValueError("max_trace_bytes is outside the supported range")
    if timeout <= 0 or timeout > 3600:
        raise ValueError("timeout_seconds must be in (0, 3600]")
    if wait_ms < 0 or wait_ms > 60000:
        raise ValueError("replay_wait_ms must be in [0, 60000]")
    if event_cap < 2 or event_cap > 512:
        raise ValueError("max_events must be in [2, 512]")
    if memory_event_cap < 0 or memory_event_cap > 64:
        raise ValueError("max_memory_events must be in [0, 64]")
    if graph_candidate_cap < 1 or graph_candidate_cap > 65536:
        raise ValueError("max_graph_candidates must be in [1, 65536]")
    normalized_environment = {
        str(key): str(value)
        for key, value in (environment or {}).items()
    }
    forbidden = {
        "LD_PRELOAD",
        "SYMCC_SCHEDULE_TRACE",
        "SYMCC_SCHEDULE_PREFIX",
        "SYMCC_SCHEDULE_ATOMIC_COMMIT",
    }
    if forbidden.intersection(normalized_environment):
        raise ValueError("environment overrides a campaign-owned variable")
    search_path = normalized_environment.get("PATH", os.environ.get("PATH"))
    executable_text = command[0]
    if os.sep in executable_text:
        executable = Path(executable_text)
        if not executable.is_absolute():
            executable = working_directory / executable
        executable = executable.resolve(strict=True)
    else:
        resolved = shutil.which(executable_text, path=search_path)
        if resolved is None:
            raise ValueError("command executable is not resolvable")
        executable = Path(resolved).resolve(strict=True)
    if not executable.is_file():
        raise ValueError("command executable must be a regular file")

    pending: deque[tuple[tuple[int, ...], int | None, int | None]] = deque()
    pending.append(((), None, None))
    seen_prefixes = {()}
    runs: list[dict[str, Any]] = []
    prefix_limit_hit = False
    with tempfile.TemporaryDirectory(prefix="symcc-native-condpor-") as temporary:
        root = Path(temporary)
        while pending and len(runs) < run_cap:
            prefix, parent_run, source_query = pending.popleft()
            run_id = len(runs)
            run_work = root / f"run-{run_id:05d}"
            run_work.mkdir()
            execution = _execute_run(
                command,
                cwd=working_directory,
                runtime=runtime,
                prefix=prefix,
                work=run_work,
                timeout_seconds=timeout,
                max_trace_bytes=trace_cap,
                environment=normalized_environment,
                wait_ms=wait_ms,
            )
            row: dict[str, Any] = {
                "run_id": run_id,
                "prefix": list(prefix),
                "parent_run": parent_run,
                "source_query": source_query,
                "execution": execution,
                "analysis": None,
                "path_regenerated": False,
            }
            if execution["valid_trace"]:
                events = _events_from_rows(execution["trace_events"])
                analysis = _analyze_trace(
                    events,
                    prefix,
                    memory_model=model,
                    max_depth=depth,
                    max_window=window,
                    max_successors=successor_cap,
                    max_events=event_cap,
                    max_memory_events=memory_event_cap,
                    max_graph_candidates=graph_candidate_cap,
                )
                row["analysis"] = analysis
                if parent_run is not None:
                    parent_digest = runs[parent_run]["execution"].get(
                        "control_flow_sha256"
                    )
                    row["path_regenerated"] = (
                        parent_digest != execution["control_flow_sha256"]
                    )
                for successor in analysis["successors"]:
                    candidate = normalize_schedule_prefix(
                        successor["prefix"], max_len=depth
                    )
                    if not candidate or candidate in seen_prefixes:
                        continue
                    if len(seen_prefixes) >= prefix_cap:
                        prefix_limit_hit = True
                        continue
                    seen_prefixes.add(candidate)
                    pending.append((
                        candidate,
                        run_id,
                        int(successor["query_index"]),
                    ))
            runs.append(row)

    run_limit_hit = bool(pending)
    pending_prefixes = [
        {
            "prefix": list(prefix),
            "parent_run": parent_run,
            "source_query": source_query,
        }
        for prefix, parent_run, source_query in pending
    ]
    invalid_run_count = sum(
        not run["execution"]["valid_trace"] for run in runs
    )
    analysis_truncated_count = sum(
        bool(run["analysis"])
        and (
            not run["analysis"]["memory_graph"]["bounded_exhaustive"]
            or any(run["analysis"]["schedule_truncated"].values())
            or any(
                query["status"] not in {"sat", "unsat"}
                for query in run["analysis"]["queries"]
            )
        )
        for run in runs
    )
    complete = not (
        run_limit_hit
        or prefix_limit_hit
        or invalid_run_count
        or analysis_truncated_count
    )
    certificate: dict[str, Any] = {
        "schema": NATIVE_CONDPOR_CAMPAIGN_SCHEMA,
        "semantics": "bounded-native-reexecution-condpor-campaign-v1",
        "command": list(command),
        "executable_path": str(executable),
        "executable_sha256": _file_digest(executable),
        "cwd": str(working_directory),
        "schedule_runtime": str(runtime),
        "schedule_runtime_sha256": _file_digest(runtime),
        "memory_model": model,
        "environment": normalized_environment,
        "bounds": {
            "max_runs": run_cap,
            "max_prefixes": prefix_cap,
            "max_successors_per_run": successor_cap,
            "max_depth": depth,
            "max_window": window,
            "max_events": event_cap,
            "max_memory_events": memory_event_cap,
            "max_graph_candidates": graph_candidate_cap,
            "max_trace_bytes": trace_cap,
            "timeout_seconds": timeout,
            "replay_wait_ms": wait_ms,
        },
        "runs": runs,
        "pending_prefixes": pending_prefixes,
        "run_count": len(runs),
        "unique_prefix_count": len(seen_prefixes),
        "pending_prefix_count": len(pending),
        "path_regeneration_count": sum(
            bool(run["path_regenerated"]) for run in runs
        ),
        "invalid_run_count": invalid_run_count,
        "analysis_truncated_count": analysis_truncated_count,
        "status": "complete" if complete else "truncated",
        "bounded_fixed_point": complete,
        "truncated": {
            "run_limit": run_limit_hit,
            "prefix_limit": prefix_limit_hit,
            "invalid_run": invalid_run_count > 0,
            "analysis": analysis_truncated_count > 0,
        },
        "proved_scope": [
            "fresh_process_reexecution_per_prefix",
            "prefix_fidelity_without_runtime_fallback",
            "path_dependent_native_event_regeneration",
            "model_checked_successor_admission",
            "bounded_prefix_fixed_point_when_complete",
        ],
        "not_proved": [
            "unbounded_condpor_soundness_completeness_optimality",
            "hardware_enforcement_of_non_sc_reads_from",
            "external_side_effect_reproducibility",
            "complete_alias_and_iso_c11_undefined_behavior_semantics",
        ],
        "sound_complete_optimal_claimed": False,
    }
    certificate["certificate_sha256"] = _certificate_digest(certificate)
    return certificate


def verify_native_condpor_campaign_certificate(
    certificate: Mapping[str, Any],
) -> bool:
    """Verify campaign topology and recompute every trace analysis."""
    try:
        if certificate.get("schema") != NATIVE_CONDPOR_CAMPAIGN_SCHEMA:
            return False
        if certificate.get("certificate_sha256") != _certificate_digest(
            certificate
        ):
            return False
        bounds = certificate["bounds"]
        runs = certificate["runs"]
        pending_rows = certificate["pending_prefixes"]
        if (
            not isinstance(bounds, Mapping)
            or not isinstance(runs, list)
            or not isinstance(pending_rows, list)
        ):
            return False
        prefixes: set[tuple[int, ...]] = set()
        regeneration_count = 0
        invalid_count = 0
        truncated_count = 0
        for run_id, run in enumerate(runs):
            if int(run["run_id"]) != run_id:
                return False
            prefix = tuple(int(value) for value in run["prefix"])
            if prefix in prefixes or (run_id == 0) != (prefix == ()):
                return False
            prefixes.add(prefix)
            execution = run["execution"]
            if not isinstance(execution, Mapping):
                return False
            valid = bool(execution["valid_trace"])
            has_parsed_trace = "trace_digest" in execution
            if has_parsed_trace:
                events = _events_from_rows(execution["trace_events"])
                protocol = _trace_protocol_status(
                    events,
                    prefix,
                    timed_out=bool(execution["timed_out"]),
                )
                if any(
                    execution.get(key) != value
                    for key, value in protocol.items()
                ):
                    return False
                expected_counts = {
                    "constraint_event_count": sum(
                        event.op == "constraint" for event in events
                    ),
                    "action_event_count": sum(
                        event.op == "action" for event in events
                    ),
                    "atomic_value_event_count": sum(
                        event.op == "atomic_value" for event in events
                    ),
                    "atomic_commit_event_count": sum(
                        event.op == "atomic_commit" for event in events
                    ),
                }
                if any(
                    execution.get(key) != value
                    for key, value in expected_counts.items()
                ):
                    return False
                if execution["trace_digest"] != schedule_trace_digest(events):
                    return False
                if execution["trace_equivalence_sha256"] != (
                    _trace_equivalence_digest(events)
                ):
                    return False
                if execution["control_flow_sha256"] != _control_flow_digest(events):
                    return False
            elif valid or execution.get("trace_events") != []:
                return False
            if not valid:
                invalid_count += 1
                if run["analysis"] is not None:
                    return False
                continue
            expected_analysis = _analyze_trace(
                events,
                prefix,
                memory_model=str(certificate["memory_model"]),
                max_depth=int(bounds["max_depth"]),
                max_window=int(bounds["max_window"]),
                max_successors=int(bounds["max_successors_per_run"]),
                max_events=int(bounds["max_events"]),
                max_memory_events=int(bounds["max_memory_events"]),
                max_graph_candidates=int(bounds["max_graph_candidates"]),
            )
            if run["analysis"] != expected_analysis:
                return False
            parent = run["parent_run"]
            if run_id == 0:
                if parent is not None or run["source_query"] is not None:
                    return False
            else:
                parent_id = int(parent)
                query_id = int(run["source_query"])
                if parent_id < 0 or parent_id >= run_id:
                    return False
                successors = runs[parent_id]["analysis"]["successors"]
                if not any(
                    int(successor["query_index"]) == query_id
                    and tuple(successor["prefix"]) == prefix
                    for successor in successors
                ):
                    return False
                regenerated = (
                    runs[parent_id]["execution"][
                        "control_flow_sha256"
                    ]
                    != execution["control_flow_sha256"]
                )
                if bool(run["path_regenerated"]) != regenerated:
                    return False
                regeneration_count += regenerated
            if (
                not expected_analysis["memory_graph"]["bounded_exhaustive"]
                or any(expected_analysis["schedule_truncated"].values())
                or any(
                    query["status"] not in {"sat", "unsat"}
                    for query in expected_analysis["queries"]
                )
            ):
                truncated_count += 1
        pending_prefixes: set[tuple[int, ...]] = set()
        for pending in pending_rows:
            prefix = tuple(int(value) for value in pending["prefix"])
            if not prefix or prefix in prefixes or prefix in pending_prefixes:
                return False
            parent_id = int(pending["parent_run"])
            query_id = int(pending["source_query"])
            if parent_id < 0 or parent_id >= len(runs):
                return False
            analysis = runs[parent_id]["analysis"]
            if not isinstance(analysis, Mapping) or not any(
                int(successor["query_index"]) == query_id
                and tuple(successor["prefix"]) == prefix
                for successor in analysis["successors"]
            ):
                return False
            pending_prefixes.add(prefix)
        if certificate["run_count"] != len(runs):
            return False
        if certificate["invalid_run_count"] != invalid_count:
            return False
        if certificate["analysis_truncated_count"] != truncated_count:
            return False
        if certificate["path_regeneration_count"] != regeneration_count:
            return False
        if certificate["pending_prefix_count"] != len(pending_prefixes):
            return False
        if certificate["unique_prefix_count"] != (
            len(prefixes) + len(pending_prefixes)
        ):
            return False
        complete = certificate["status"] == "complete"
        if certificate["bounded_fixed_point"] is not complete:
            return False
        if complete and any(certificate["truncated"].values()):
            return False
        return True
    except (
        KeyError,
        TypeError,
        ValueError,
        IndexError,
        OverflowError,
        RuntimeError,
    ):
        return False
