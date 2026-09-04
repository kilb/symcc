#!/usr/bin/env python3
"""Run and verify sealed paired holdout campaigns for QF_BV backends."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import resource
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

from qf_bv_backend import (
    SmtLibQfbvSolver,
    lower_qfbv_query,
    normalize_qfbv_capabilities,
)
from qf_bv_conformance import (
    BackendSpec,
    current_backend_identity,
    normalize_backend_spec,
    verify_qfbv_conformance,
)
from query_store import QueryStore


CORPUS_SCHEMA = "symcc-qfbv-corpus-split-v1"
CAMPAIGN_SCHEMA = "symcc-qfbv-holdout-campaign-v1"
REPLAY_SCHEMA = "symcc-qfbv-holdout-campaign-replay-v1"
MIN_CONFIRMATORY_REPETITIONS = 20
MAX_QUERIES = 4096
MAX_CORPUS_BYTES = 256 * 1024 * 1024
_SOLVED = frozenset({"sat", "unsat"})
_STATUSES = frozenset({"sat", "unsat", "unknown", "error"})


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def corpus_digest(corpus: Mapping[str, Any]) -> str:
    return _digest({
        key: value
        for key, value in corpus.items()
        if key != "corpus_sha256"
    })


def campaign_digest(campaign: Mapping[str, Any]) -> str:
    return _digest({
        key: value
        for key, value in campaign.items()
        if key != "campaign_sha256"
    })


def _split_for(
    query_id: str,
    seed: str,
    split_parts: int,
    holdout_parts: int,
) -> str:
    bucket = int(hashlib.sha256(
        f"{seed}:{query_id}".encode("ascii")
    ).hexdigest(), 16) % split_parts
    return "holdout" if bucket < holdout_parts else "train"


def _query_identity(
    store: QueryStore,
    envelope: Mapping[str, Any],
) -> tuple[str, str]:
    query_id, created = store.ingest(envelope)
    if not created:
        raise ValueError(f"duplicate Query IR identity {query_id}")
    loaded = store.load_query_ir(query_id)
    if loaded is None:
        raise ValueError("sealed Query IR cannot be loaded from CAS")
    _, certificate, _ = lower_qfbv_query(
        query_id,
        loaded[0],
        loaded[1],
        normalize_qfbv_capabilities({"accept_unsat": True}),
    )
    return query_id, str(certificate["certificate_sha256"])


def seal_corpus(
    envelopes: Sequence[Mapping[str, Any]],
    *,
    split_seed: str = "symcc-qfbv-holdout-v1",
    split_parts: int = 5,
    holdout_parts: int = 1,
    source_kind: str = "external-query-ir",
) -> dict[str, Any]:
    if (
        not isinstance(envelopes, Sequence)
        or isinstance(envelopes, (str, bytes))
        or not 1 <= len(envelopes) <= MAX_QUERIES
    ):
        raise ValueError(f"corpus must contain 1--{MAX_QUERIES} envelopes")
    split_seed = str(split_seed)
    if not split_seed or len(split_seed.encode("utf-8")) > 256:
        raise ValueError("split seed must be non-empty and bounded")
    split_parts = int(split_parts)
    holdout_parts = int(holdout_parts)
    if not 1 <= split_parts <= 10000:
        raise ValueError("split_parts must be in 1--10000")
    if not 1 <= holdout_parts <= split_parts:
        raise ValueError("holdout_parts must be in 1--split_parts")
    source_kind = str(source_kind)[:64]
    if not source_kind:
        raise ValueError("source_kind must not be empty")
    canonical_size = sum(len(_canonical_json(envelope))
                         for envelope in envelopes)
    if canonical_size > MAX_CORPUS_BYTES:
        raise ValueError("canonical corpus exceeds 256 MiB")

    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="symcc-qfbv-corpus-") as root:
        store = QueryStore(root)
        for raw_envelope in envelopes:
            if not isinstance(raw_envelope, Mapping):
                raise ValueError("corpus envelopes must be objects")
            envelope = copy.deepcopy(dict(raw_envelope))
            query_id, lowering_sha256 = _query_identity(store, envelope)
            rows.append({
                "query_id": query_id,
                "envelope_sha256": _digest(envelope),
                "lowering_certificate_sha256": lowering_sha256,
                "split": _split_for(
                    query_id,
                    split_seed,
                    split_parts,
                    holdout_parts,
                ),
                "envelope": envelope,
            })
    rows.sort(key=lambda row: row["query_id"])
    counts = Counter(str(row["split"]) for row in rows)
    if counts["holdout"] == 0:
        raise ValueError("deterministic split produced no holdout query")
    if split_parts > holdout_parts and len(rows) > 1 and counts["train"] == 0:
        raise ValueError("deterministic split produced no training query")
    corpus: dict[str, Any] = {
        "schema": CORPUS_SCHEMA,
        "source_kind": source_kind,
        "split_seed": split_seed,
        "split_parts": split_parts,
        "holdout_parts": holdout_parts,
        "canonical_bytes": canonical_size,
        "query_count": len(rows),
        "train_count": counts["train"],
        "holdout_count": counts["holdout"],
        "queries": rows,
    }
    corpus["corpus_sha256"] = corpus_digest(corpus)
    return corpus


def verify_corpus(corpus: Mapping[str, Any]) -> bool:
    try:
        if corpus.get("schema") != CORPUS_SCHEMA:
            return False
        if corpus.get("corpus_sha256") != corpus_digest(corpus):
            return False
        split_seed = str(corpus["split_seed"])
        split_parts = int(corpus["split_parts"])
        holdout_parts = int(corpus["holdout_parts"])
        if not 1 <= split_parts <= 10000:
            return False
        if not 1 <= holdout_parts <= split_parts:
            return False
        rows = corpus.get("queries")
        if (
            not isinstance(rows, Sequence)
            or isinstance(rows, (str, bytes))
            or not 1 <= len(rows) <= MAX_QUERIES
        ):
            return False
        query_ids: set[str] = set()
        canonical_size = 0
        counts: Counter[str] = Counter()
        with tempfile.TemporaryDirectory(
                prefix="symcc-qfbv-corpus-verify-") as root:
            store = QueryStore(root)
            for row in rows:
                if not isinstance(row, Mapping):
                    return False
                envelope = row.get("envelope")
                if not isinstance(envelope, Mapping):
                    return False
                canonical_size += len(_canonical_json(envelope))
                if row.get("envelope_sha256") != _digest(envelope):
                    return False
                query_id, lowering_sha256 = _query_identity(store, envelope)
                if query_id != row.get("query_id") or query_id in query_ids:
                    return False
                query_ids.add(query_id)
                if (
                    row.get("lowering_certificate_sha256")
                    != lowering_sha256
                ):
                    return False
                split = _split_for(
                    query_id,
                    split_seed,
                    split_parts,
                    holdout_parts,
                )
                if row.get("split") != split:
                    return False
                counts[split] += 1
        return (
            canonical_size <= MAX_CORPUS_BYTES
            and canonical_size == corpus.get("canonical_bytes")
            and len(rows) == corpus.get("query_count")
            and counts["train"] == corpus.get("train_count")
            and counts["holdout"] == corpus.get("holdout_count")
            and counts["holdout"] > 0
            and (
                split_parts == holdout_parts
                or len(rows) == 1
                or counts["train"] > 0
            )
            and [row["query_id"] for row in rows] == sorted(query_ids)
        )
    except (KeyError, OSError, TypeError, ValueError):
        return False


def load_envelopes(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    source = Path(path)
    if source.is_dir():
        files = sorted(source.rglob("*.query.json"))
        if not files:
            files = sorted({
                *source.rglob("*.json"),
                *source.rglob("*.jsonl"),
            })
        if not files:
            raise ValueError("corpus directory has no JSON files")
    elif source.is_file():
        files = [source]
    else:
        raise FileNotFoundError(f"corpus path does not exist: {source}")
    envelopes: list[dict[str, Any]] = []
    total_bytes = 0
    for file_path in files:
        size = file_path.stat().st_size
        total_bytes += size
        if total_bytes > MAX_CORPUS_BYTES:
            raise ValueError("corpus input exceeds 256 MiB")
        if file_path.suffix == ".jsonl":
            values = [
                json.loads(line)
                for line in file_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        else:
            parsed = json.loads(file_path.read_text(encoding="utf-8"))
            if (
                isinstance(parsed, Mapping)
                and isinstance(parsed.get("queries"), list)
            ):
                values = parsed["queries"]
            elif isinstance(parsed, list):
                values = parsed
            else:
                values = [parsed]
        for value in values:
            if not isinstance(value, Mapping):
                raise ValueError(f"{file_path} contains a non-object query")
            envelopes.append(dict(value))
            if len(envelopes) > MAX_QUERIES:
                raise ValueError("corpus contains more than 4096 queries")
    return envelopes


def _simple_envelope(
    name: str,
    op: str,
    value: int,
    *,
    prefix_value: int | None = None,
) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = [
        {
            "id": 0,
            "op": "read",
            "bits": 8,
            "children": [],
            "attrs": {"index": 0},
        },
        {
            "id": 1,
            "op": "constant",
            "bits": 8,
            "children": [],
            "attrs": {"value_hex": f"{value:02x}"},
        },
        {
            "id": 2,
            "op": op,
            "bits": 1,
            "children": [0, 1],
            "attrs": {},
        },
        {
            "id": 3,
            "op": "bool",
            "bits": 1,
            "children": [],
            "attrs": {"value": True},
        },
    ]
    prefix_roots = [3]
    if prefix_value is not None:
        nodes.extend([
            {
                "id": 4,
                "op": "constant",
                "bits": 8,
                "children": [],
                "attrs": {"value_hex": f"{prefix_value:02x}"},
            },
            {
                "id": 5,
                "op": "equal",
                "bits": 1,
                "children": [0, 4],
                "attrs": {},
            },
        ])
        prefix_roots = [5]
    return {
        "schema": "symcc-query-ir-v1",
        "producer": "qfbv-campaign-smoke",
        "nodes": nodes,
        "prefix_roots": prefix_roots,
        "target_root": 2,
        "input_hex": "00",
        "timeout_ms": 1000,
        "metadata": {"source": f"qfbv-campaign-smoke-{name}"},
        "smt2": "(assert true)\n",
        "prefix_smt2": "(assert true)\n",
        "target_smt2": "(assert true)\n",
    }


def build_smoke_corpus() -> list[dict[str, Any]]:
    """Small functional corpus; never use it for a performance claim."""
    return [
        _simple_envelope("equal", "equal", 0x42),
        _simple_envelope("distinct", "distinct", 0x42),
        _simple_envelope("ult", "ult", 0x10),
        _simple_envelope("ugt", "ugt", 0xF0),
        _simple_envelope("slt", "slt", 0x00),
        _simple_envelope("uge", "uge", 0x80),
        _simple_envelope(
            "contradiction-a",
            "equal",
            0x42,
            prefix_value=0x41,
        ),
        _simple_envelope(
            "contradiction-b",
            "equal",
            0x44,
            prefix_value=0x43,
        ),
    ]


def _selected_specs(
    conformance: Mapping[str, Any],
    backend_names: Sequence[str] | None,
    *,
    check_current: bool = True,
) -> tuple[tuple[BackendSpec, Mapping[str, Any]], ...]:
    if not verify_qfbv_conformance(conformance):
        raise ValueError("QF_BV conformance artifact is invalid")
    entries = conformance.get("backends")
    assert isinstance(entries, Sequence)
    by_name = {
        str(entry["name"]): entry
        for entry in entries
        if isinstance(entry, Mapping)
    }
    selected = (
        tuple(str(name) for name in backend_names)
        if backend_names
        else tuple(sorted(by_name))
    )
    if not selected or len(set(selected)) != len(selected):
        raise ValueError("selected backend names must be non-empty and unique")
    missing = [name for name in selected if name not in by_name]
    if missing:
        raise ValueError(f"backend is absent from conformance: {missing[0]}")
    result = []
    for name in selected:
        entry = by_name[name]
        spec = normalize_backend_spec(entry)
        if check_current:
            identity = current_backend_identity(spec)
            if (
                identity["actual_version"] != entry.get("actual_version")
                or identity["executable_sha256"]
                != entry.get("executable_sha256")
            ):
                raise ValueError(
                    f"current backend identity differs from conformance: {name}")
        result.append((spec, entry))
    return tuple(result)


def _usage_us(usage: resource.struct_rusage) -> int:
    return int(round((usage.ru_utime + usage.ru_stime) * 1_000_000))


def _run_observation(
    row: Mapping[str, Any],
    spec: BackendSpec,
    *,
    timeout_ms: int,
    repetition: int,
) -> dict[str, Any]:
    envelope = copy.deepcopy(dict(row["envelope"]))
    envelope["timeout_ms"] = timeout_ms
    with tempfile.TemporaryDirectory(prefix="symcc-qfbv-campaign-") as root:
        store = QueryStore(root)
        query_id, _ = store.ingest(envelope)
        if query_id != row["query_id"]:
            raise RuntimeError("campaign query identity changed")
        lease = store.claim(f"campaign-{spec.name}-{repetition}")
        if lease is None:
            raise RuntimeError("campaign query is not claimable")
        backend = SmtLibQfbvSolver(
            store,
            spec.command,
            name=spec.name,
            capabilities={"accept_unsat": True},
        )
        usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
        result = dict(backend(lease))
        usage_after = resource.getrusage(resource.RUSAGE_CHILDREN)
        completed = store.complete(
            lease,
            f"campaign-{spec.name}-{repetition}",
            result,
        )
    elapsed_us = max(0, int(result.get("elapsed_us", 0)))
    status = str(result.get("status", "error"))
    par2_us = (
        min(elapsed_us, timeout_ms * 1000)
        if status in _SOLVED
        else 2 * timeout_ms * 1000
    )
    return {
        "query_id": query_id,
        "backend": spec.name,
        "repetition": repetition,
        "status": status,
        "backend_status": str(result.get("backend_status", "")),
        "assignments": result.get("assignments", {}),
        "backend_model_verified": (
            result.get("backend_model_verified") is True
        ),
        "backend_unsat_authorized": (
            result.get("backend_unsat_authorized") is True
        ),
        "backend_unsat_confirmation": str(
            result.get("backend_unsat_confirmation", "")),
        "backend_capabilities": result.get("backend_capabilities", {}),
        "lowering_certificate": result.get("lowering_certificate", {}),
        "elapsed_us": elapsed_us,
        "child_cpu_us": max(
            0,
            _usage_us(usage_after) - _usage_us(usage_before),
        ),
        "par2_us": par2_us,
        "timed_out": "timeout" in str(result.get("reason", "")).lower(),
        "reason": str(result.get("reason", ""))[:512],
        "store_completed": completed,
    }


def _task_order(
    query_ids: Sequence[str],
    backend_names: Sequence[str],
    repetitions: int,
    order_seed: str,
) -> list[tuple[int, str, str]]:
    tasks = [
        (repetition, query_id, backend)
        for repetition in range(repetitions)
        for query_id in query_ids
        for backend in backend_names
    ]
    return sorted(tasks, key=lambda task: hashlib.sha256(
        f"{order_seed}:{task[0]}:{task[1]}:{task[2]}".encode("ascii")
    ).digest())


def aggregate_results(
    results: Sequence[Mapping[str, Any]],
    backend_names: Sequence[str],
) -> dict[str, Any]:
    by_backend: dict[str, dict[str, int]] = {}
    for backend in backend_names:
        selected = [row for row in results if row["backend"] == backend]
        statuses = Counter(str(row["status"]) for row in selected)
        total_par2 = sum(int(row["par2_us"]) for row in selected)
        by_backend[backend] = {
            "runs": len(selected),
            "sat": statuses["sat"],
            "unsat": statuses["unsat"],
            "unknown": statuses["unknown"],
            "error": statuses["error"],
            "solved": statuses["sat"] + statuses["unsat"],
            "model_verified": sum(
                row["backend_model_verified"] is True for row in selected),
            "authorized_unsat": sum(
                row["backend_unsat_authorized"] is True for row in selected),
            "status_only_confirmations": sum(
                bool(row.get("backend_unsat_confirmation"))
                for row in selected
            ),
            "timed_out": sum(row["timed_out"] is True for row in selected),
            "elapsed_us": sum(int(row["elapsed_us"]) for row in selected),
            "child_cpu_us": sum(int(row["child_cpu_us"]) for row in selected),
            "par2_us": total_par2,
            "mean_par2_us": (
                total_par2 // len(selected) if selected else 0
            ),
        }
    indexed = {
        (int(row["repetition"]), str(row["query_id"]), str(row["backend"])): row
        for row in results
    }
    pairs: list[dict[str, Any]] = []
    for left_index, left in enumerate(backend_names):
        for right in backend_names[left_index + 1:]:
            keys = sorted({
                (repetition, query_id)
                for repetition, query_id, backend in indexed
                if backend == left
            })
            both = left_only = right_only = neither = disagreements = 0
            par2_delta = 0
            for repetition, query_id in keys:
                left_row = indexed[(repetition, query_id, left)]
                right_row = indexed[(repetition, query_id, right)]
                left_solved = left_row["status"] in _SOLVED
                right_solved = right_row["status"] in _SOLVED
                if left_solved and right_solved:
                    both += 1
                elif left_solved:
                    left_only += 1
                elif right_solved:
                    right_only += 1
                else:
                    neither += 1
                if {
                    left_row["status"],
                    right_row["status"],
                } == {"sat", "unsat"}:
                    disagreements += 1
                par2_delta += (
                    int(left_row["par2_us"]) - int(right_row["par2_us"]))
            pairs.append({
                "left": left,
                "right": right,
                "paired_runs": len(keys),
                "both_solved": both,
                "left_only_solved": left_only,
                "right_only_solved": right_only,
                "neither_solved": neither,
                "sat_unsat_disagreements": disagreements,
                "left_minus_right_par2_us": par2_delta,
                "mean_left_minus_right_par2_us": (
                    par2_delta // len(keys) if keys else 0
                ),
            })
    logical_keys = {
        (int(row["repetition"]), str(row["query_id"]))
        for row in results
    }
    oracle_solved = sum(
        any(
            indexed[(repetition, query_id, backend)]["status"] in _SOLVED
            for backend in backend_names
        )
        for repetition, query_id in logical_keys
    )
    return {
        "backends": by_backend,
        "pairs": pairs,
        "logical_runs": len(logical_keys),
        "any_backend_solved": oracle_solved,
        "all_backends_unsolved": len(logical_keys) - oracle_solved,
    }


def campaign_semantic_digest(campaign: Mapping[str, Any]) -> str:
    projected_results = []
    results = campaign.get("results", ())
    if isinstance(results, Sequence) and not isinstance(results, (str, bytes)):
        for row in results:
            if not isinstance(row, Mapping):
                continue
            certificate = row.get("lowering_certificate")
            projected_results.append({
                "query_id": row.get("query_id"),
                "backend": row.get("backend"),
                "repetition": row.get("repetition"),
                "status": row.get("status"),
                "backend_status": row.get("backend_status"),
                "backend_model_verified": row.get("backend_model_verified"),
                "backend_unsat_authorized": row.get(
                    "backend_unsat_authorized"),
                "backend_unsat_confirmation": row.get(
                    "backend_unsat_confirmation"),
                "lowering_certificate_sha256": (
                    certificate.get("certificate_sha256")
                    if isinstance(certificate, Mapping)
                    else ""
                ),
            })
    return _digest({
        "corpus_sha256": (
            campaign.get("corpus", {}).get("corpus_sha256")
            if isinstance(campaign.get("corpus"), Mapping)
            else ""
        ),
        "conformance_semantic_sha256": (
            campaign.get("conformance", {}).get("semantic_sha256")
            if isinstance(campaign.get("conformance"), Mapping)
            else ""
        ),
        "protocol": campaign.get("protocol"),
        "backend_names": campaign.get("backend_names"),
        "results": projected_results,
    })


def run_campaign(
    corpus: Mapping[str, Any],
    conformance: Mapping[str, Any],
    *,
    backend_names: Sequence[str] | None = None,
    timeout_ms: int = 5000,
    repetitions: int = 1,
    order_seed: str = "symcc-qfbv-campaign-order-v1",
    confirmatory: bool = False,
) -> dict[str, Any]:
    if not verify_corpus(corpus):
        raise ValueError("QF_BV corpus artifact is invalid")
    timeout_ms = int(timeout_ms)
    repetitions = int(repetitions)
    if not 1 <= timeout_ms <= 3600000:
        raise ValueError("timeout_ms must be in 1--3600000")
    if not 1 <= repetitions <= 100:
        raise ValueError("repetitions must be in 1--100")
    if confirmatory and repetitions < MIN_CONFIRMATORY_REPETITIONS:
        raise ValueError("confirmatory campaigns require at least 20 repetitions")
    if confirmatory and corpus.get("source_kind") == "synthetic-smoke":
        raise ValueError("synthetic smoke corpus cannot be confirmatory")
    if confirmatory and int(corpus.get("train_count", 0)) < 1:
        raise ValueError("confirmatory campaigns require a disjoint train split")
    order_seed = str(order_seed)
    if not order_seed or len(order_seed.encode("utf-8")) > 256:
        raise ValueError("order_seed must be non-empty and bounded")
    selected = _selected_specs(conformance, backend_names)
    specs = {spec.name: spec for spec, _ in selected}
    names = tuple(spec.name for spec, _ in selected)
    rows = {
        str(row["query_id"]): row
        for row in corpus["queries"]
        if row["split"] == "holdout"
    }
    tasks = _task_order(
        sorted(rows),
        names,
        repetitions,
        order_seed,
    )
    results = [
        _run_observation(
            rows[query_id],
            specs[backend],
            timeout_ms=timeout_ms,
            repetition=repetition,
        )
        for repetition, query_id, backend in tasks
    ]
    identities = [{
        "name": spec.name,
        "command": list(spec.command),
        "expected_version": spec.expected_version,
        "actual_version": entry["actual_version"],
        "executable": entry["executable"],
        "executable_sha256": entry["executable_sha256"],
    } for spec, entry in selected]
    campaign: dict[str, Any] = {
        "schema": CAMPAIGN_SCHEMA,
        "generated_unix_ms": int(time.time() * 1000),
        "corpus": corpus,
        "conformance": conformance,
        "backend_names": list(names),
        "backend_identities": identities,
        "protocol": {
            "timeout_ms": timeout_ms,
            "repetitions": repetitions,
            "order_seed": order_seed,
            "confirmatory": bool(confirmatory),
            "minimum_confirmatory_repetitions": (
                MIN_CONFIRMATORY_REPETITIONS),
            "query_split": "holdout-only",
            "execution": "one-shot-sequential-randomized-order",
            "resource_contract": (
                "equal-query-count-equal-wall-timeout-measured-child-cpu"),
            "performance_claims": bool(confirmatory),
        },
        "results": results,
        "aggregate": aggregate_results(results, names),
    }
    campaign["semantic_sha256"] = campaign_semantic_digest(campaign)
    campaign["campaign_sha256"] = campaign_digest(campaign)
    return campaign


def _patched_candidate(
    input_hex: str,
    assignments: Mapping[str, Any],
) -> bytes | None:
    try:
        candidate = bytearray.fromhex(input_hex)
        for raw_offset, raw_value in assignments.items():
            offset = int(raw_offset)
            value = int(raw_value)
            if not 0 <= offset < len(candidate) or not 0 <= value <= 255:
                return None
            candidate[offset] = value
        return bytes(candidate)
    except (TypeError, ValueError):
        return None


def verify_campaign(campaign: Mapping[str, Any]) -> bool:
    try:
        if campaign.get("schema") != CAMPAIGN_SCHEMA:
            return False
        if campaign.get("campaign_sha256") != campaign_digest(campaign):
            return False
        if (
            campaign.get("semantic_sha256")
            != campaign_semantic_digest(campaign)
        ):
            return False
        corpus = campaign.get("corpus")
        conformance = campaign.get("conformance")
        if not isinstance(corpus, Mapping) or not verify_corpus(corpus):
            return False
        if (
            not isinstance(conformance, Mapping)
            or not verify_qfbv_conformance(conformance)
        ):
            return False
        names = campaign.get("backend_names")
        if (
            not isinstance(names, Sequence)
            or isinstance(names, (str, bytes))
            or not names
            or len(set(names)) != len(names)
        ):
            return False
        selected = _selected_specs(
            conformance,
            names,
            check_current=False,
        )
        expected_identities = [{
            "name": spec.name,
            "command": list(spec.command),
            "expected_version": spec.expected_version,
            "actual_version": entry["actual_version"],
            "executable": entry["executable"],
            "executable_sha256": entry["executable_sha256"],
        } for spec, entry in selected]
        if campaign.get("backend_identities") != expected_identities:
            return False
        protocol = campaign.get("protocol")
        if not isinstance(protocol, Mapping):
            return False
        timeout_ms = int(protocol["timeout_ms"])
        repetitions = int(protocol["repetitions"])
        order_seed = str(protocol["order_seed"])
        confirmatory = protocol.get("confirmatory") is True
        if not 1 <= timeout_ms <= 3600000:
            return False
        if not 1 <= repetitions <= 100:
            return False
        if confirmatory and repetitions < MIN_CONFIRMATORY_REPETITIONS:
            return False
        if confirmatory and corpus.get("source_kind") == "synthetic-smoke":
            return False
        if confirmatory and int(corpus.get("train_count", 0)) < 1:
            return False
        if protocol.get("minimum_confirmatory_repetitions") != 20:
            return False
        if protocol.get("query_split") != "holdout-only":
            return False
        if (
            protocol.get("execution")
            != "one-shot-sequential-randomized-order"
        ):
            return False
        if (
            protocol.get("resource_contract")
            != "equal-query-count-equal-wall-timeout-measured-child-cpu"
        ):
            return False
        if protocol.get("performance_claims") is not confirmatory:
            return False
        holdout_rows = {
            str(row["query_id"]): row
            for row in corpus["queries"]
            if row["split"] == "holdout"
        }
        expected_tasks = _task_order(
            sorted(holdout_rows),
            names,
            repetitions,
            order_seed,
        )
        results = campaign.get("results")
        if (
            not isinstance(results, Sequence)
            or isinstance(results, (str, bytes))
            or len(results) != len(expected_tasks)
        ):
            return False
        actual_tasks = [
            (
                int(row["repetition"]),
                str(row["query_id"]),
                str(row["backend"]),
            )
            for row in results
            if isinstance(row, Mapping)
        ]
        if actual_tasks != expected_tasks:
            return False
        capability = normalize_qfbv_capabilities({"accept_unsat": True})
        with tempfile.TemporaryDirectory(
                prefix="symcc-qfbv-campaign-verify-") as root:
            stores: dict[str, QueryStore] = {}
            certificates: dict[str, Mapping[str, Any]] = {}
            for query_id, row in holdout_rows.items():
                store = QueryStore(Path(root) / query_id)
                envelope = copy.deepcopy(dict(row["envelope"]))
                envelope["timeout_ms"] = timeout_ms
                ingested_id, _ = store.ingest(envelope)
                if ingested_id != query_id:
                    return False
                loaded = store.load_query_ir(query_id)
                if loaded is None:
                    return False
                _, certificate, _ = lower_qfbv_query(
                    query_id,
                    loaded[0],
                    loaded[1],
                    capability,
                )
                stores[query_id] = store
                certificates[query_id] = certificate
            for row in results:
                if not isinstance(row, Mapping):
                    return False
                query_id = str(row["query_id"])
                status = str(row["status"])
                if status not in _STATUSES:
                    return False
                if row.get("backend_capabilities") != capability:
                    return False
                if (
                    row.get("lowering_certificate")
                    != certificates[query_id]
                ):
                    return False
                if row.get("store_completed") is not True:
                    return False
                elapsed_us = int(row["elapsed_us"])
                child_cpu_us = int(row["child_cpu_us"])
                par2_us = int(row["par2_us"])
                if elapsed_us < 0 or child_cpu_us < 0:
                    return False
                expected_par2 = (
                    min(elapsed_us, timeout_ms * 1000)
                    if status in _SOLVED
                    else 2 * timeout_ms * 1000
                )
                if par2_us != expected_par2:
                    return False
                if not isinstance(row.get("timed_out"), bool):
                    return False
                assignments = row.get("assignments")
                if not isinstance(assignments, Mapping):
                    return False
                if status == "sat":
                    if row.get("backend_model_verified") is not True:
                        return False
                    envelope = holdout_rows[query_id]["envelope"]
                    candidate = _patched_candidate(
                        str(envelope.get("input_hex", "")),
                        assignments,
                    )
                    if (
                        candidate is None
                        or not stores[query_id].validate_candidate(
                            query_id,
                            candidate,
                        )
                    ):
                        return False
                elif assignments:
                    return False
                if (
                    status == "unsat"
                    and row.get("backend_unsat_authorized") is not True
                ):
                    return False
                confirmation = str(
                    row.get("backend_unsat_confirmation", ""))
                if confirmation not in {"", "status-only-rerun-v1"}:
                    return False
        return (
            campaign.get("aggregate") == aggregate_results(results, names)
            and int(campaign.get("generated_unix_ms", -1)) >= 0
        )
    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
        return False


def replay_campaign(campaign: Mapping[str, Any]) -> dict[str, Any]:
    if not verify_campaign(campaign):
        raise ValueError("source QF_BV campaign artifact is invalid")
    protocol = campaign["protocol"]
    replay = run_campaign(
        campaign["corpus"],
        campaign["conformance"],
        backend_names=campaign["backend_names"],
        timeout_ms=protocol["timeout_ms"],
        repetitions=protocol["repetitions"],
        order_seed=protocol["order_seed"],
        confirmatory=protocol["confirmatory"],
    )
    result = {
        "schema": REPLAY_SCHEMA,
        "source_campaign_sha256": campaign["campaign_sha256"],
        "source_semantic_sha256": campaign["semantic_sha256"],
        "replay_semantic_sha256": replay["semantic_sha256"],
        "semantic_match": (
            replay["semantic_sha256"] == campaign["semantic_sha256"]
        ),
        "replay": replay,
    }
    result["replay_sha256"] = _digest(result)
    return result


def _write_or_print(value: Mapping[str, Any], output: str | None) -> None:
    payload = _canonical_json(value) + b"\n"
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    else:
        print(payload.decode("ascii"), end="")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--verify")
    action.add_argument("--replay")
    corpus = parser.add_mutually_exclusive_group()
    corpus.add_argument("--corpus")
    corpus.add_argument("--smoke-corpus", action="store_true")
    parser.add_argument("--conformance")
    parser.add_argument("--backends", default="")
    parser.add_argument("--timeout-ms", type=int, default=5000)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--split-seed", default="symcc-qfbv-holdout-v1")
    parser.add_argument("--split-parts", type=int, default=5)
    parser.add_argument("--holdout-parts", type=int, default=1)
    parser.add_argument(
        "--order-seed",
        default="symcc-qfbv-campaign-order-v1",
    )
    parser.add_argument("--confirmatory", action="store_true")
    parser.add_argument("--output")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.verify:
        value = json.loads(Path(args.verify).read_text(encoding="ascii"))
        verified = (
            isinstance(value, Mapping)
            and verify_campaign(value)
        )
        print(json.dumps({"verified": verified}, sort_keys=True))
        return 0 if verified else 1
    if args.replay:
        value = json.loads(Path(args.replay).read_text(encoding="ascii"))
        if not isinstance(value, Mapping):
            raise ValueError("QF_BV campaign artifact must be an object")
        replay = replay_campaign(value)
        _write_or_print(replay, args.output)
        return 0 if replay["semantic_match"] else 1
    if not args.conformance:
        raise ValueError("--conformance is required")
    if args.smoke_corpus:
        envelopes = build_smoke_corpus()
        source_kind = "synthetic-smoke"
    elif args.corpus:
        envelopes = load_envelopes(args.corpus)
        source_kind = "external-query-ir"
    else:
        raise ValueError("--corpus or --smoke-corpus is required")
    corpus = seal_corpus(
        envelopes,
        split_seed=args.split_seed,
        split_parts=args.split_parts,
        holdout_parts=args.holdout_parts,
        source_kind=source_kind,
    )
    conformance = json.loads(
        Path(args.conformance).read_text(encoding="ascii"))
    if not isinstance(conformance, Mapping):
        raise ValueError("QF_BV conformance artifact must be an object")
    backend_names = tuple(
        name.strip()
        for name in args.backends.split(",")
        if name.strip()
    )
    campaign = run_campaign(
        corpus,
        conformance,
        backend_names=backend_names or None,
        timeout_ms=args.timeout_ms,
        repetitions=args.repetitions,
        order_seed=args.order_seed,
        confirmatory=args.confirmatory,
    )
    _write_or_print(campaign, args.output)
    return 0 if verify_campaign(campaign) else 1


if __name__ == "__main__":
    raise SystemExit(main())
