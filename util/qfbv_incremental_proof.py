#!/usr/bin/env python3
"""Verified incremental-CNF clause exchange and persistent proof fragments."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
import time
import weakref
from collections import OrderedDict
from dataclasses import dataclass, replace
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Mapping, Sequence

from qfbv_incremental_sat import BitBlastPlan, CnfIncrement
from qfbv_artifact_lifecycle import (
    ArtifactJobLease,
    ArtifactLifecycleRegistry,
    ArtifactRef,
)


CLAUSE_PROTOCOL = "symcc-qfbv-incremental-lrup-clause-v1"
CLAUSE_RECORD_SCHEMA = "symcc-qfbv-incremental-clause-record-v1"
# Historical compatibility name.  These records are SymCC's content-addressed
# JSON LRUP DAG, not the external PalRUP binary proof-fragment wire format.
# Keep the serialized value stable because it participates in record hashes.
PROOF_FRAGMENT_SCHEMA = "symcc-qfbv-palrup-proof-fragment-v1"
PROJECT_LRUP_DAG_SCHEMA = PROOF_FRAGMENT_SCHEMA
RESULT_RECEIPT_SCHEMA = "symcc-qfbv-incremental-result-receipt-v1"
STORE_SCHEMA = "symcc-qfbv-incremental-proof-store-v1"
EVENT_STREAM_PROTOCOL = "symcc-qfbv-monotonic-proof-event-stream-v1"
SQLITE_JOURNAL_CONTRACT = "delete-flock-v1"
MAX_RECORD_BYTES = 64 * 1024 * 1024
MAX_CLAUSE_LITERALS = 65_536
MAX_PROOF_STEPS = 1_000_000
MAX_HINTS = 16_000_000
MAX_IMPORTS = 4096
REPLAY_CACHE_PROTOCOL = "symcc-qfbv-proof-replay-cache-v1"
DEFAULT_REPLAY_CACHE_ENTRIES = 4096
DEFAULT_REPLAY_CACHE_BYTES = 16 * 1024 * 1024
MAX_REPLAY_CACHE_ENTRIES = 1_000_000
MAX_REPLAY_CACHE_BYTES = 1 << 34
_HEX64 = re.compile(r"[0-9a-f]{64}")


class IncrementalProofError(ValueError):
    """An incremental proof, scope, receipt, or store failed closed."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hex_digest(value: Any, name: str) -> str:
    if type(value) is not str or _HEX64.fullmatch(value) is None:
        raise IncrementalProofError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _bounded_int(value: Any, name: str, lower: int, upper: int) -> int:
    if type(value) is not int:
        raise IncrementalProofError(f"{name} must be an integer")
    if not lower <= value <= upper:
        raise IncrementalProofError(f"{name} must be in [{lower}, {upper}]")
    return value


def _assumption_literals(
    values: Sequence[Any], *, max_variable: int, name: str
) -> tuple[int, ...]:
    if isinstance(values, (str, bytes)):
        raise IncrementalProofError(f"{name} must be a list")
    normalized: list[int] = []
    variables: set[int] = set()
    for value in values:
        literal = _bounded_int(value, name, -max_variable, max_variable)
        if literal == 0:
            raise IncrementalProofError(f"{name} must be nonzero")
        variable = abs(literal)
        if variable in variables:
            raise IncrementalProofError(f"{name} variables must be unique")
        variables.add(variable)
        normalized.append(literal)
    return tuple(sorted(normalized))


def normalize_clause(
    raw: Sequence[Any],
    *,
    max_variable: int,
    allow_empty: bool = True,
) -> tuple[int, ...]:
    if isinstance(raw, (str, bytes)) or len(raw) > MAX_CLAUSE_LITERALS:
        raise IncrementalProofError("clause is not a bounded literal list")
    seen: set[int] = set()
    literals: list[int] = []
    for value in raw:
        literal = _bounded_int(value, "clause literal", -max_variable, max_variable)
        if literal == 0:
            raise IncrementalProofError("clause literal zero is forbidden")
        if -literal in seen:
            raise IncrementalProofError("tautological clauses are not exchangeable")
        if literal not in seen:
            seen.add(literal)
            literals.append(literal)
    if not literals and not allow_empty:
        raise IncrementalProofError("empty clause is not permitted here")
    return tuple(sorted(literals, key=lambda item: (abs(item), item < 0)))


def _normalize_hints(raw: Any, *, maximum_id: int) -> tuple[int, ...]:
    if (
        not isinstance(raw, Sequence)
        or isinstance(raw, (str, bytes))
        or len(raw) > MAX_HINTS
    ):
        raise IncrementalProofError("LRUP hints must be a bounded list")
    return tuple(_bounded_int(value, "LRUP hint", 1, maximum_id) for value in raw)


def check_lrup(
    clauses: Mapping[int, Sequence[int]],
    candidate: Sequence[int],
    hints: Sequence[int],
    *,
    max_variable: int,
) -> int:
    """Check one ordered LRUP chain and return its propagation count."""
    normalized = normalize_clause(candidate, max_variable=max_variable)
    assignment: dict[int, bool] = {}
    for literal in normalized:
        variable = abs(literal)
        value = literal < 0
        previous = assignment.setdefault(variable, value)
        if previous != value:
            raise IncrementalProofError("candidate negation is inconsistent")
    propagated = 0
    if not hints:
        raise IncrementalProofError("non-tautological LRUP proof has no hints")
    for position, raw_hint in enumerate(hints):
        hint = _bounded_int(raw_hint, "LRUP hint", 1, (1 << 63) - 1)
        raw_clause = clauses.get(hint)
        if raw_clause is None:
            raise IncrementalProofError("LRUP hint references an unavailable clause")
        unassigned: list[int] = []
        satisfied = False
        for raw_literal in raw_clause:
            literal = int(raw_literal)
            current = assignment.get(abs(literal))
            if current is None:
                unassigned.append(literal)
            elif current == (literal > 0):
                satisfied = True
                break
        if satisfied or len(unassigned) > 1:
            raise IncrementalProofError("LRUP hint is neither unit nor conflicting")
        if not unassigned:
            if position != len(hints) - 1:
                raise IncrementalProofError("LRUP chain continues after conflict")
            return propagated
        literal = unassigned[0]
        variable = abs(literal)
        value = literal > 0
        previous = assignment.setdefault(variable, value)
        if previous != value:
            if position != len(hints) - 1:
                raise IncrementalProofError("LRUP chain continues after conflict")
            return propagated
        propagated += 1
    raise IncrementalProofError("LRUP hints do not derive a conflict")


def derive_rup_hints(
    clauses: Sequence[Sequence[int]],
    candidate: Sequence[int],
    *,
    max_variable: int,
    max_scans: int = 16_000_000,
    deadline_ns: int | None = None,
) -> tuple[int, ...]:
    """Construct a deterministic RUP chain for a proposed exchange clause."""
    normalized = normalize_clause(candidate, max_variable=max_variable)
    assignment: dict[int, bool] = {abs(lit): lit < 0 for lit in normalized}
    hints: list[int] = []
    scans = 0
    changed = True
    while changed:
        changed = False
        for clause_id, clause in enumerate(clauses, 1):
            scans += 1
            if scans > max_scans:
                raise IncrementalProofError("RUP hint search exceeded its scan budget")
            if (
                deadline_ns is not None
                and scans & 1023 == 0
                and time.monotonic_ns() >= deadline_ns
            ):
                raise IncrementalProofError("RUP hint search exceeded its deadline")
            unassigned: list[int] = []
            satisfied = False
            for literal in clause:
                current = assignment.get(abs(literal))
                if current is None:
                    unassigned.append(int(literal))
                elif current == (literal > 0):
                    satisfied = True
                    break
            if satisfied or len(unassigned) > 1:
                continue
            hints.append(clause_id)
            if not unassigned:
                return tuple(hints)
            literal = unassigned[0]
            previous = assignment.setdefault(abs(literal), literal > 0)
            if previous != (literal > 0):
                return tuple(hints)
            changed = True
    raise IncrementalProofError("proposed clause is not RUP under the formula")


def _formula_position(plan: BitBlastPlan, digest: str) -> tuple[int, int, int]:
    for increment in plan.increments:
        if increment.formula_sha256 == digest:
            return (
                increment.ordinal,
                increment.last_clause_id,
                increment.max_variable,
            )
    raise IncrementalProofError("proof formula is not an increment of the target")


def _normalize_import(raw: Mapping[str, Any], *, max_variable: int) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise IncrementalProofError("proof import must be an object")
    return {
        "receipt_sha256": _hex_digest(raw.get("receipt_sha256"), "import receipt"),
        "local_clause_id": _bounded_int(
            raw.get("local_clause_id"), "import local clause ID", 1, (1 << 63) - 1
        ),
        "clause": list(
            normalize_clause(raw.get("clause", ()), max_variable=max_variable)
        ),
    }


def normalize_clause_record(
    raw: Mapping[str, Any],
    *,
    max_variable: int,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise IncrementalProofError("clause record must be an object")
    if raw.get("schema") != CLAUSE_RECORD_SCHEMA:
        raise IncrementalProofError("unsupported clause record schema")
    if raw.get("fragment_schema") != PROOF_FRAGMENT_SCHEMA:
        raise IncrementalProofError("unsupported persistent proof fragment schema")
    imports_raw = raw.get("imports", ())
    steps_raw = raw.get("proof_steps", ())
    if (
        not isinstance(imports_raw, Sequence)
        or isinstance(imports_raw, (str, bytes))
        or len(imports_raw) > MAX_IMPORTS
        or not isinstance(steps_raw, Sequence)
        or isinstance(steps_raw, (str, bytes))
        or not 1 <= len(steps_raw) <= MAX_PROOF_STEPS
    ):
        raise IncrementalProofError("proof imports or steps are not bounded lists")
    imports = tuple(
        _normalize_import(item, max_variable=max_variable) for item in imports_raw
    )
    imported_ids = {item["local_clause_id"] for item in imports}
    if len(imported_ids) != len(imports):
        raise IncrementalProofError("proof imports reuse a local clause ID")
    steps: list[dict[str, Any]] = []
    maximum_id = max(imported_ids, default=0)
    total_hints = 0
    for raw_step in steps_raw:
        if not isinstance(raw_step, Mapping):
            raise IncrementalProofError("proof step must be an object")
        clause_id = _bounded_int(
            raw_step.get("clause_id"), "proof clause ID", 1, (1 << 63) - 1
        )
        if clause_id in imported_ids or clause_id <= maximum_id:
            raise IncrementalProofError("proof step IDs must be strictly increasing")
        hints = _normalize_hints(raw_step.get("hints", ()), maximum_id=clause_id - 1)
        total_hints += len(hints)
        if total_hints > MAX_HINTS:
            raise IncrementalProofError("proof fragment has too many hints")
        steps.append(
            {
                "clause_id": clause_id,
                "clause": list(
                    normalize_clause(
                        raw_step.get("clause", ()), max_variable=max_variable
                    )
                ),
                "hints": list(hints),
            }
        )
        maximum_id = clause_id
    dependency_raw = raw.get("dependency_assumptions", ())
    if not isinstance(dependency_raw, Sequence) or isinstance(
        dependency_raw, (str, bytes)
    ):
        raise IncrementalProofError("dependency assumptions must be a list")
    dependencies = _assumption_literals(
        dependency_raw,
        max_variable=max_variable,
        name="dependency assumption",
    )
    final_clause = tuple(steps[-1]["clause"])
    if any(-assumption not in final_clause for assumption in dependencies):
        raise IncrementalProofError(
            "dependency assumptions are not lifted into the shared clause"
        )
    normalized: dict[str, Any] = {
        "schema": CLAUSE_RECORD_SCHEMA,
        "fragment_schema": PROOF_FRAGMENT_SCHEMA,
        "protocol": CLAUSE_PROTOCOL,
        "formula_sha256": _hex_digest(raw.get("formula_sha256"), "proof formula"),
        "cnf_sha256": _hex_digest(raw.get("cnf_sha256"), "proof CNF"),
        "base_clause_count": _bounded_int(
            raw.get("base_clause_count"), "base clause count", 1, 100_000_000
        ),
        "max_variable": _bounded_int(
            raw.get("max_variable"), "proof max variable", 1, max_variable
        ),
        "source_worker": str(raw.get("source_worker", ""))[:256],
        "worker_epoch": _bounded_int(
            raw.get("worker_epoch", 0), "worker epoch", 0, (1 << 63) - 1
        ),
        "sequence": _bounded_int(
            raw.get("sequence", 0), "fragment sequence", 0, (1 << 63) - 1
        ),
        "dependency_assumptions": list(dependencies),
        "imports": list(imports),
        "proof_steps": steps,
        "shared_clause": list(final_clause),
    }
    if not normalized["source_worker"]:
        raise IncrementalProofError("source worker must not be empty")
    expected = _digest(_canonical_json(normalized))
    supplied = raw.get("record_sha256", expected)
    if type(supplied) is not str:
        raise IncrementalProofError("clause record identity must be a string")
    if supplied != expected:
        raise IncrementalProofError("clause record content identity changed")
    normalized["record_sha256"] = expected
    return normalized


@dataclass(frozen=True)
class ClauseAuthorization:
    record_sha256: str
    formula_sha256: str
    clause: tuple[int, ...]
    source_worker: str
    worker_epoch: int
    sequence: int
    dependency_assumptions: tuple[int, ...]
    checker_elapsed_us: int
    proof_steps: int
    propagation_count: int
    import_count: int
    replay_cache_hit: bool = False


@dataclass(frozen=True)
class ResultAuthorization:
    receipt_sha256: str
    status: str
    formula_sha256: str
    assumption_sha256: str
    failed_assumptions: tuple[int, ...]
    clause_receipt_sha256: str
    checker_elapsed_us: int


@dataclass(frozen=True)
class _ReplayCacheEntry:
    plan_reference: weakref.ReferenceType[BitBlastPlan]
    plan_scope: tuple[object, ...]
    authorization: ClauseAuthorization
    import_closure: tuple[tuple[str, str], ...]
    accounted_bytes: int


class IncrementalProofChecker:
    """Small trusted LRUP replay core over a recomputed bit-blast plan."""

    def __init__(
        self,
        store: "IncrementalProofStore | None" = None,
        *,
        replay_cache_entries: int = DEFAULT_REPLAY_CACHE_ENTRIES,
        replay_cache_bytes: int = DEFAULT_REPLAY_CACHE_BYTES,
    ) -> None:
        entries = _bounded_int(
            replay_cache_entries,
            "proof replay cache entries",
            0,
            MAX_REPLAY_CACHE_ENTRIES,
        )
        byte_budget = _bounded_int(
            replay_cache_bytes,
            "proof replay cache bytes",
            0,
            MAX_REPLAY_CACHE_BYTES,
        )
        if (entries == 0) != (byte_budget == 0):
            raise IncrementalProofError(
                "proof replay cache entry and byte budgets must be disabled together"
            )
        self.store = store
        self.replay_cache_entries = entries
        self.replay_cache_bytes = byte_budget
        self._replay_cache: OrderedDict[tuple[int, str], _ReplayCacheEntry] = (
            OrderedDict()
        )
        self._replay_cache_accounted_bytes = 0
        self._replay_cache_hits = 0
        self._replay_cache_misses = 0
        self._replay_cache_inserts = 0
        self._replay_cache_evictions = 0
        self._replay_cache_oversized = 0
        self._replay_cache_bypassed = 0
        self._replay_cache_attestation_failures = 0
        self._replay_cache_attested_objects = 0
        self._replay_cache_attested_bytes = 0
        self._replay_cache_lock = threading.RLock()
        self.policy_sha256 = _digest(
            _canonical_json(
                {
                    "schema": "symcc-qfbv-incremental-proof-policy-v1",
                    "protocol": CLAUSE_PROTOCOL,
                    "checker": "ordered-lrup-replay-v1",
                    "store_identity_sha256": (
                        store.identity_sha256 if store is not None else ""
                    ),
                }
            )
        )
        self.replay_cache_policy_sha256 = _digest(
            _canonical_json(
                {
                    "protocol": REPLAY_CACHE_PROTOCOL,
                    "checker_policy_sha256": self.policy_sha256,
                    "max_entries": entries,
                    "max_bytes": byte_budget,
                }
            )
        )

    @staticmethod
    def _plan_replay_scope(plan: BitBlastPlan) -> tuple[object, ...]:
        return (
            str(plan.query_id),
            _hex_digest(plan.formula_sha256, "cache plan formula"),
            _hex_digest(plan.assumption_sha256, "cache plan assumptions"),
            id(plan.clauses),
            len(plan.clauses),
            int(plan.max_variable),
            id(plan.increments),
            len(plan.increments),
        )

    @staticmethod
    def _authorization_size(authorization: ClauseAuthorization) -> int:
        return (
            256
            + len(authorization.record_sha256)
            + len(authorization.formula_sha256)
            + len(authorization.source_worker.encode("utf-8"))
            + 8 * len(authorization.clause)
            + 8 * len(authorization.dependency_assumptions)
        )

    @staticmethod
    def _cache_safe_plan(plan: BitBlastPlan) -> bool:
        return (
            type(plan) is BitBlastPlan
            and type(plan.clauses) is tuple
            and all(
                type(clause) is tuple and all(type(literal) is int for literal in clause)
                for clause in plan.clauses
            )
            and type(plan.increments) is tuple
            and all(type(increment) is CnfIncrement for increment in plan.increments)
        )

    def _cached_authorization(
        self,
        plan: BitBlastPlan,
        digest: str,
    ) -> _ReplayCacheEntry | None:
        if self.replay_cache_entries == 0:
            with self._replay_cache_lock:
                self._replay_cache_bypassed += 1
            return None
        key = (id(plan), digest)
        scope = self._plan_replay_scope(plan)
        with self._replay_cache_lock:
            entry = self._replay_cache.get(key)
            if (
                entry is None
                or entry.plan_reference() is not plan
                or entry.plan_scope != scope
            ):
                if entry is not None:
                    self._replay_cache_accounted_bytes -= entry.accounted_bytes
                    del self._replay_cache[key]
                    self._replay_cache_evictions += 1
                self._replay_cache_misses += 1
                return None
            self._replay_cache.move_to_end(key)
            return entry

    def _reject_cached_authorization(
        self,
        plan: BitBlastPlan,
        digest: str,
        entry: _ReplayCacheEntry,
    ) -> None:
        """Discard exactly the cache entry whose dependency attestation failed."""
        key = (id(plan), digest)
        with self._replay_cache_lock:
            self._replay_cache_attestation_failures += 1
            current = self._replay_cache.get(key)
            if current is entry:
                self._replay_cache_accounted_bytes -= current.accounted_bytes
                del self._replay_cache[key]
                self._replay_cache_evictions += 1

    def _cache_authorization(
        self,
        plan: BitBlastPlan,
        authorization: ClauseAuthorization,
        import_closure: Mapping[str, str],
    ) -> None:
        if self.replay_cache_entries == 0:
            return
        if not self._cache_safe_plan(plan):
            with self._replay_cache_lock:
                self._replay_cache_bypassed += 1
            return
        cached = replace(
            authorization,
            checker_elapsed_us=0,
            replay_cache_hit=False,
        )
        normalized_closure = tuple(sorted(import_closure.items()))
        accounted_bytes = self._authorization_size(cached) + 128 * len(
            normalized_closure
        )
        if accounted_bytes > self.replay_cache_bytes:
            with self._replay_cache_lock:
                self._replay_cache_oversized += 1
            return
        key = (id(plan), authorization.record_sha256)
        entry = _ReplayCacheEntry(
            plan_reference=weakref.ref(plan),
            plan_scope=self._plan_replay_scope(plan),
            authorization=cached,
            import_closure=normalized_closure,
            accounted_bytes=accounted_bytes,
        )
        with self._replay_cache_lock:
            previous = self._replay_cache.pop(key, None)
            if previous is not None:
                self._replay_cache_accounted_bytes -= previous.accounted_bytes
            self._replay_cache[key] = entry
            self._replay_cache_accounted_bytes += accounted_bytes
            self._replay_cache_inserts += 1
            while (
                len(self._replay_cache) > self.replay_cache_entries
                or self._replay_cache_accounted_bytes > self.replay_cache_bytes
            ):
                _old_key, old_entry = self._replay_cache.popitem(last=False)
                self._replay_cache_accounted_bytes -= old_entry.accounted_bytes
                self._replay_cache_evictions += 1

    def clear_replay_cache(self) -> None:
        with self._replay_cache_lock:
            self._replay_cache.clear()
            self._replay_cache_accounted_bytes = 0

    def replay_cache_stats(self) -> dict[str, Any]:
        with self._replay_cache_lock:
            return {
                "schema": REPLAY_CACHE_PROTOCOL,
                "policy_sha256": self.replay_cache_policy_sha256,
                "max_entries": self.replay_cache_entries,
                "max_bytes": self.replay_cache_bytes,
                "entries": len(self._replay_cache),
                "accounted_bytes": self._replay_cache_accounted_bytes,
                "hits": self._replay_cache_hits,
                "misses": self._replay_cache_misses,
                "inserts": self._replay_cache_inserts,
                "evictions": self._replay_cache_evictions,
                "oversized": self._replay_cache_oversized,
                "bypassed": self._replay_cache_bypassed,
                "attestation_failures": self._replay_cache_attestation_failures,
                "attested_objects": self._replay_cache_attested_objects,
                "attested_bytes": self._replay_cache_attested_bytes,
            }

    def verify_clause_record(
        self,
        plan: BitBlastPlan,
        raw: Mapping[str, Any],
        *,
        _stack: frozenset[str] = frozenset(),
        _closure_out: dict[str, str] | None = None,
    ) -> ClauseAuthorization:
        started = time.monotonic_ns()
        record = normalize_clause_record(raw, max_variable=plan.max_variable)
        digest = str(record["record_sha256"])
        if digest in _stack:
            raise IncrementalProofError("proof import graph contains a cycle")
        cache_eligible = self._cache_safe_plan(plan)
        if self.replay_cache_entries > 0 and not cache_eligible:
            with self._replay_cache_lock:
                self._replay_cache_bypassed += 1
            cached_entry = None
        else:
            cached_entry = self._cached_authorization(plan, digest)
        if cached_entry is not None:
            if cached_entry.import_closure and self.store is None:
                raise IncrementalProofError("cached imports require a configured store")
            try:
                attested_bytes = 0
                if cached_entry.import_closure:
                    assert self.store is not None
                    attested_bytes = self.store.attest_encoded_objects(
                        dict(cached_entry.import_closure)
                    )
            except (IncrementalProofError, OSError, sqlite3.Error):
                self._reject_cached_authorization(plan, digest, cached_entry)
                raise
            with self._replay_cache_lock:
                self._replay_cache_hits += 1
                self._replay_cache_attested_objects += len(
                    cached_entry.import_closure
                )
                self._replay_cache_attested_bytes += attested_bytes
            if _closure_out is not None:
                _closure_out.update(cached_entry.import_closure)
            return replace(
                cached_entry.authorization,
                checker_elapsed_us=(time.monotonic_ns() - started) // 1000,
                replay_cache_hit=True,
            )
        ordinal, base_count, formula_max_variable = _formula_position(
            plan, str(record["formula_sha256"])
        )
        if int(record["base_clause_count"]) != base_count:
            raise IncrementalProofError("proof base clause boundary is inconsistent")
        prefix = plan.clauses[:base_count]
        if str(record["cnf_sha256"]) != _digest(_canonical_json(prefix)):
            raise IncrementalProofError("proof CNF prefix identity changed")
        if int(record["max_variable"]) != formula_max_variable:
            raise IncrementalProofError(
                "proof variable domain differs from its formula increment"
            )
        clause_table: dict[int, Sequence[int]] = {
            index: clause for index, clause in enumerate(prefix, 1)
        }
        import_closure: dict[str, str] = {}
        for imported in record["imports"]:
            if self.store is None:
                raise IncrementalProofError("proof imports require a configured store")
            imported_digest = str(imported["receipt_sha256"])
            imported_raw = self.store.load(imported_digest)
            child_closure: dict[str, str] = {}
            authorization = self.verify_clause_record(
                plan,
                imported_raw,
                _stack=_stack | {digest},
                _closure_out=child_closure,
            )
            import_closure[imported_digest] = _digest(_canonical_json(imported_raw))
            import_closure.update(child_closure)
            imported_ordinal, _imported_count, _imported_max_variable = (
                _formula_position(plan, authorization.formula_sha256)
            )
            if imported_ordinal > ordinal:
                raise IncrementalProofError(
                    "proof fragment imports from a descendant increment"
                )
            if tuple(imported["clause"]) != authorization.clause:
                raise IncrementalProofError("imported clause content does not match")
            local_id = int(imported["local_clause_id"])
            if local_id in clause_table:
                raise IncrementalProofError("import shadows an existing clause ID")
            clause_table[local_id] = authorization.clause
        propagated = 0
        for step in record["proof_steps"]:
            clause_id = int(step["clause_id"])
            if clause_id in clause_table:
                raise IncrementalProofError("proof step shadows an existing clause ID")
            propagated += check_lrup(
                clause_table,
                step["clause"],
                step["hints"],
                max_variable=int(record["max_variable"]),
            )
            clause_table[clause_id] = tuple(step["clause"])
        authorization = ClauseAuthorization(
            record_sha256=digest,
            formula_sha256=str(record["formula_sha256"]),
            clause=tuple(record["shared_clause"]),
            source_worker=str(record["source_worker"]),
            worker_epoch=int(record["worker_epoch"]),
            sequence=int(record["sequence"]),
            dependency_assumptions=tuple(record["dependency_assumptions"]),
            checker_elapsed_us=(time.monotonic_ns() - started) // 1000,
            proof_steps=len(record["proof_steps"]),
            propagation_count=propagated,
            import_count=len(record["imports"]),
        )
        if cache_eligible:
            self._cache_authorization(plan, authorization, import_closure)
        if _closure_out is not None:
            _closure_out.update(import_closure)
        return authorization

    def verify_result_receipt(
        self,
        plan: BitBlastPlan,
        raw: Mapping[str, Any],
    ) -> ResultAuthorization:
        started = time.monotonic_ns()
        if not isinstance(raw, Mapping) or raw.get("schema") != RESULT_RECEIPT_SCHEMA:
            raise IncrementalProofError("unsupported result receipt schema")
        if raw.get("protocol") != CLAUSE_PROTOCOL:
            raise IncrementalProofError("unsupported result receipt protocol")
        status = str(raw.get("status", ""))
        if status != "unsat":
            raise IncrementalProofError(
                "incremental proof receipts currently authorize UNSAT"
            )
        formula = _hex_digest(raw.get("formula_sha256"), "result formula")
        if formula != plan.formula_sha256:
            raise IncrementalProofError("result formula does not match the target")
        assumption = _hex_digest(raw.get("assumption_sha256"), "result assumptions")
        if assumption != plan.assumption_sha256:
            raise IncrementalProofError("result assumptions do not match the target")
        failed_raw = raw.get("failed_assumptions", ())
        if not isinstance(failed_raw, Sequence) or isinstance(failed_raw, (str, bytes)):
            raise IncrementalProofError("failed assumptions must be a list")
        failed = _assumption_literals(
            failed_raw,
            max_variable=plan.max_variable,
            name="failed assumption",
        )
        if not failed or not set(failed) <= set(plan.assumptions):
            raise IncrementalProofError("failed assumptions are not active assumptions")
        clause_digest = _hex_digest(
            raw.get("clause_receipt_sha256"), "result clause receipt"
        )
        if self.store is None:
            raise IncrementalProofError("result verification requires a proof store")
        clause = self.verify_clause_record(plan, self.store.load(clause_digest))
        expected = normalize_clause(
            tuple(-literal for literal in failed), max_variable=plan.max_variable
        )
        if clause.clause != expected:
            raise IncrementalProofError(
                "result proof does not derive the failed-assumption clause"
            )
        normalized = {
            "schema": RESULT_RECEIPT_SCHEMA,
            "protocol": CLAUSE_PROTOCOL,
            "status": "unsat",
            "formula_sha256": formula,
            "assumption_sha256": assumption,
            "failed_assumptions": list(failed),
            "clause_receipt_sha256": clause_digest,
        }
        expected_digest = _digest(_canonical_json(normalized))
        supplied_receipt = raw.get("receipt_sha256", expected_digest)
        if type(supplied_receipt) is not str or supplied_receipt != expected_digest:
            raise IncrementalProofError("result receipt content identity changed")
        return ResultAuthorization(
            receipt_sha256=expected_digest,
            status="unsat",
            formula_sha256=formula,
            assumption_sha256=assumption,
            failed_assumptions=failed,
            clause_receipt_sha256=clause_digest,
            checker_elapsed_us=(time.monotonic_ns() - started) // 1000,
        )


def make_rup_clause_record(
    plan: BitBlastPlan,
    clause: Sequence[int],
    *,
    formula_sha256: str | None = None,
    dependency_assumptions: Sequence[int] = (),
    source_worker: str,
    worker_epoch: int,
    sequence: int,
    deadline_ns: int | None = None,
) -> dict[str, Any]:
    """Create a self-contained RUP fragment; verification remains mandatory."""
    formula = formula_sha256 or plan.formula_sha256
    _ordinal, base_count, formula_max_variable = _formula_position(plan, formula)
    prefix = plan.clauses[:base_count]
    normalized_clause = normalize_clause(clause, max_variable=formula_max_variable)
    hints = derive_rup_hints(
        prefix,
        normalized_clause,
        max_variable=formula_max_variable,
        deadline_ns=deadline_ns,
    )
    body: dict[str, Any] = {
        "schema": CLAUSE_RECORD_SCHEMA,
        "fragment_schema": PROOF_FRAGMENT_SCHEMA,
        "protocol": CLAUSE_PROTOCOL,
        "formula_sha256": formula,
        "cnf_sha256": _digest(_canonical_json(prefix)),
        "base_clause_count": base_count,
        "max_variable": formula_max_variable,
        "source_worker": str(source_worker)[:256],
        "worker_epoch": _bounded_int(worker_epoch, "worker epoch", 0, (1 << 63) - 1),
        "sequence": _bounded_int(sequence, "proof sequence", 0, (1 << 63) - 1),
        "dependency_assumptions": list(
            _assumption_literals(
                dependency_assumptions,
                max_variable=formula_max_variable,
                name="dependency assumption",
            )
        ),
        "imports": [],
        "proof_steps": [
            {
                "clause_id": base_count + 1,
                "clause": list(normalized_clause),
                "hints": list(hints),
            }
        ],
        "shared_clause": list(normalized_clause),
    }
    body["record_sha256"] = _digest(_canonical_json(body))
    return normalize_clause_record(body, max_variable=formula_max_variable)


def make_imported_lrup_clause_record(
    plan: BitBlastPlan,
    imported_clauses: Sequence[ClauseAuthorization],
    derived_steps: Sequence[tuple[Sequence[int], Sequence[int]]],
    *,
    dependency_assumptions: Sequence[int] = (),
    source_worker: str,
    worker_epoch: int,
    sequence: int,
) -> dict[str, Any]:
    """Build a bounded LRUP fragment over checked imported clauses.

    ``derived_steps`` contains ``(clause, hints)`` pairs.  Hint IDs use the
    local numbering emitted by this function: permanent clauses first,
    imported clauses second, followed by derived clauses.  The returned record
    is normalized but remains untrusted until ``IncrementalProofChecker``
    replays it and recursively verifies every import.
    """
    if not imported_clauses:
        raise IncrementalProofError("imported LRUP fragment requires imports")
    if len(imported_clauses) > MAX_IMPORTS:
        raise IncrementalProofError("imported LRUP fragment has too many imports")
    if not derived_steps or len(derived_steps) > MAX_PROOF_STEPS:
        raise IncrementalProofError("imported LRUP fragment has invalid steps")
    _ordinal, base_count, formula_max_variable = _formula_position(
        plan, plan.formula_sha256
    )
    imports: list[dict[str, Any]] = []
    seen_receipts: set[str] = set()
    for index, authorization in enumerate(imported_clauses, 1):
        if authorization.formula_sha256 != plan.formula_sha256:
            raise IncrementalProofError(
                "imported LRUP clause belongs to another formula increment"
            )
        if authorization.record_sha256 in seen_receipts:
            raise IncrementalProofError("imported LRUP receipts must be unique")
        seen_receipts.add(authorization.record_sha256)
        imports.append(
            {
                "receipt_sha256": authorization.record_sha256,
                "local_clause_id": base_count + index,
                "clause": list(authorization.clause),
            }
        )
    first_step_id = base_count + len(imports) + 1
    steps = [
        {
            "clause_id": first_step_id + index,
            "clause": list(normalize_clause(clause, max_variable=formula_max_variable)),
            "hints": list(hints),
        }
        for index, (clause, hints) in enumerate(derived_steps)
    ]
    normalized_dependencies = list(
        _assumption_literals(
            dependency_assumptions,
            max_variable=formula_max_variable,
            name="dependency assumption",
        )
    )
    body: dict[str, Any] = {
        "schema": CLAUSE_RECORD_SCHEMA,
        "fragment_schema": PROOF_FRAGMENT_SCHEMA,
        "protocol": CLAUSE_PROTOCOL,
        "formula_sha256": plan.formula_sha256,
        "cnf_sha256": _digest(_canonical_json(plan.clauses[:base_count])),
        "base_clause_count": base_count,
        "max_variable": formula_max_variable,
        "source_worker": str(source_worker)[:256],
        "worker_epoch": _bounded_int(worker_epoch, "worker epoch", 0, (1 << 63) - 1),
        "sequence": _bounded_int(sequence, "proof sequence", 0, (1 << 63) - 1),
        "dependency_assumptions": normalized_dependencies,
        "imports": imports,
        "proof_steps": steps,
        "shared_clause": list(steps[-1]["clause"]),
    }
    body["record_sha256"] = _digest(_canonical_json(body))
    return normalize_clause_record(body, max_variable=formula_max_variable)


def lift_ascii_lrat_proof(
    plan: BitBlastPlan,
    proof: str,
    *,
    failed_assumptions: Sequence[int] | None = None,
    imported_clauses: Sequence[ClauseAuthorization] = (),
    source_worker: str,
    worker_epoch: int,
    sequence: int,
) -> dict[str, Any]:
    """Lift an LRAT refutation of formula+assumption-units into one clause.

    The DIMACS producer appends assumption unit clauses after the permanent
    activation-guarded formula.  Negated failed assumptions are added to every
    LRAT clause.  While checking the negation of each lifted clause, those
    assumptions are therefore already assigned, so the remaining LRUP chain is
    independently replayable over the permanent formula.
    """
    failed = _assumption_literals(
        plan.assumptions if failed_assumptions is None else failed_assumptions,
        max_variable=plan.max_variable,
        name="LRAT failed assumption",
    )
    if not failed or not set(failed) <= set(plan.assumptions):
        raise IncrementalProofError("LRAT failed assumptions are not active")
    base_count = len(plan.clauses)
    if len(imported_clauses) > MAX_IMPORTS:
        raise IncrementalProofError("LRAT lift has too many imported clauses")
    imports: list[dict[str, Any]] = []
    for index, authorization in enumerate(imported_clauses, 1):
        imports.append(
            {
                "receipt_sha256": authorization.record_sha256,
                "local_clause_id": base_count + index,
                "clause": list(authorization.clause),
            }
        )
    assumption_start = base_count + len(imports)
    assumption_clause_ids = {
        assumption_start + index + 1: literal
        for index, literal in enumerate(plan.assumptions)
    }
    ignored_assumption_ids = set(assumption_clause_ids)
    guards = tuple(-literal for literal in failed)
    steps: list[dict[str, Any]] = []
    imported_ids = {int(item["local_clause_id"]) for item in imports}
    seen_ids = set(range(1, base_count + 1)) | imported_ids | ignored_assumption_ids
    deleted: set[int] = set()
    final_clause: tuple[int, ...] | None = None
    encoded_size = 0
    for line_number, raw_line in enumerate(proof.splitlines(), 1):
        encoded_size += len(raw_line) + 1
        if encoded_size > MAX_RECORD_BYTES:
            raise IncrementalProofError("ASCII LRAT proof exceeds its byte limit")
        line = raw_line.strip()
        if not line or line.startswith("c"):
            continue
        tokens = line.split()
        try:
            clause_id = int(tokens[0])
        except (IndexError, ValueError) as error:
            raise IncrementalProofError(
                f"invalid LRAT clause ID on line {line_number}"
            ) from error
        if len(tokens) >= 2 and tokens[1] == "d":
            if clause_id <= 0 or clause_id not in seen_ids:
                raise IncrementalProofError("LRAT deletion anchor is unavailable")
            try:
                deletions = tuple(int(value) for value in tokens[2:])
            except ValueError as error:
                raise IncrementalProofError("invalid LRAT deletion") from error
            if not deletions or deletions[-1] != 0:
                raise IncrementalProofError("unterminated LRAT deletion")
            for target in deletions[:-1]:
                if target <= 0 or target not in seen_ids:
                    raise IncrementalProofError("LRAT deletes an unavailable clause")
                deleted.add(target)
            continue
        if clause_id <= 0 or clause_id in seen_ids:
            raise IncrementalProofError("LRAT clause IDs must be fresh and positive")
        try:
            numbers = tuple(int(value) for value in tokens[1:])
        except ValueError as error:
            raise IncrementalProofError("invalid LRAT integer") from error
        try:
            clause_end = numbers.index(0)
            hint_end = numbers.index(0, clause_end + 1)
        except ValueError as error:
            raise IncrementalProofError("unterminated LRAT addition") from error
        if hint_end != len(numbers) - 1:
            raise IncrementalProofError("LRAT addition has trailing fields")
        original_clause = numbers[:clause_end]
        raw_hints = numbers[clause_end + 1 : hint_end]
        if any(hint <= 0 for hint in raw_hints):
            raise IncrementalProofError(
                "RAT hint blocks are outside the LRUP exchange contract"
            )
        hints: list[int] = []
        for hint in raw_hints:
            if hint in ignored_assumption_ids:
                continue
            if hint not in seen_ids or hint in deleted:
                raise IncrementalProofError(
                    "LRAT hint references an unavailable clause"
                )
            hints.append(hint)
        lifted = normalize_clause(
            tuple(original_clause) + guards,
            max_variable=plan.max_variable,
        )
        steps.append(
            {
                "clause_id": clause_id,
                "clause": list(lifted),
                "hints": hints,
            }
        )
        if len(steps) > MAX_PROOF_STEPS:
            raise IncrementalProofError("LRAT proof has too many additions")
        seen_ids.add(clause_id)
        final_clause = lifted
    expected_final = normalize_clause(guards, max_variable=plan.max_variable)
    if final_clause != expected_final:
        raise IncrementalProofError("LRAT proof does not finish with a refutation")
    body: dict[str, Any] = {
        "schema": CLAUSE_RECORD_SCHEMA,
        "fragment_schema": PROOF_FRAGMENT_SCHEMA,
        "protocol": CLAUSE_PROTOCOL,
        "formula_sha256": plan.formula_sha256,
        "cnf_sha256": _digest(_canonical_json(plan.clauses)),
        "base_clause_count": base_count,
        "max_variable": plan.max_variable,
        "source_worker": str(source_worker)[:256],
        "worker_epoch": _bounded_int(worker_epoch, "worker epoch", 0, (1 << 63) - 1),
        "sequence": _bounded_int(sequence, "proof sequence", 0, (1 << 63) - 1),
        "dependency_assumptions": list(failed),
        "imports": imports,
        "proof_steps": steps,
        "shared_clause": list(expected_final),
    }
    body["record_sha256"] = _digest(_canonical_json(body))
    return normalize_clause_record(body, max_variable=plan.max_variable)


def make_unsat_result_receipt(
    plan: BitBlastPlan,
    clause_receipt_sha256: str,
    failed_assumptions: Sequence[int],
) -> dict[str, Any]:
    normalized_failed = list(
        _assumption_literals(
            failed_assumptions,
            max_variable=plan.max_variable,
            name="result failed assumption",
        )
    )
    body: dict[str, Any] = {
        "schema": RESULT_RECEIPT_SCHEMA,
        "protocol": CLAUSE_PROTOCOL,
        "status": "unsat",
        "formula_sha256": plan.formula_sha256,
        "assumption_sha256": plan.assumption_sha256,
        "failed_assumptions": normalized_failed,
        "clause_receipt_sha256": _hex_digest(
            clause_receipt_sha256, "result clause receipt"
        ),
    }
    body["receipt_sha256"] = _digest(_canonical_json(body))
    return body


class IncrementalProofStore:
    """Immutable JSON CAS for recursively verifiable clause proof fragments."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        max_records: int = 1_000_000,
        max_bytes: int = 64 * 1024 * 1024 * 1024,
        lifecycle: ArtifactLifecycleRegistry | None = None,
        lifecycle_lease: ArtifactJobLease | None = None,
    ) -> None:
        self.max_records = _bounded_int(max_records, "proof max records", 1, 10_000_000)
        self.max_bytes = _bounded_int(max_bytes, "proof max bytes", 1, 1 << 44)
        self.lifecycle = lifecycle
        self.lifecycle_lease = lifecycle_lease
        if lifecycle_lease is not None and lifecycle is None:
            raise IncrementalProofError(
                "incremental proof lifecycle lease requires a registry"
            )
        root_path = Path(root)
        if root_path.is_symlink():
            raise IncrementalProofError("proof store root must not be a symlink")
        self.root = root_path.resolve()
        self.identity_sha256 = _digest(
            _canonical_json(
                {
                    "schema": STORE_SCHEMA,
                    "protocol": CLAUSE_PROTOCOL,
                    "root": str(self.root),
                    "sqlite_journal_contract": SQLITE_JOURNAL_CONTRACT,
                }
            )
        )
        self.objects = self.root / "objects"
        self.db_path = self.root / "index.sqlite3"
        self.lock_path = self.root / ".store.lock"
        self.objects.mkdir(parents=True, exist_ok=True)
        with self._lock(exclusive=True):
            self._initialize()
        if self.lifecycle is not None:
            inventory = self.synchronize_lifecycle(max_entries=self.max_records)
            if not bool(inventory["complete"]):
                raise IncrementalProofError(
                    "incremental proof lifecycle synchronization is incomplete"
                )

    def _connect(self) -> sqlite3.Connection:
        database = sqlite3.connect(self.db_path, timeout=30.0)
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA busy_timeout=30000")
        # Every database transaction is already serialized by the stable
        # cross-process flock above.  A rollback journal keeps that contract
        # usable on qualified shared filesystems, where SQLite WAL's shared
        # memory sidecar is not supported.
        database.execute("PRAGMA journal_mode=DELETE")
        database.execute("PRAGMA synchronous=FULL")
        return database

    def _initialize(self) -> None:
        with self._connect() as database:
            database.executescript(
                """
                CREATE TABLE IF NOT EXISTS records(
                    digest TEXT PRIMARY KEY,
                    formula_sha256 TEXT NOT NULL,
                    clause_json TEXT NOT NULL,
                    encoded_bytes INTEGER NOT NULL,
                    object_path TEXT NOT NULL UNIQUE,
                    created REAL NOT NULL,
                    last_seen REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS records_by_formula
                    ON records(formula_sha256, last_seen DESC, digest);
                CREATE TABLE IF NOT EXISTS proof_events(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    digest TEXT NOT NULL UNIQUE,
                    formula_sha256 TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS proof_events_by_formula
                    ON proof_events(formula_sha256, sequence);
                CREATE TABLE IF NOT EXISTS metadata(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            expected = {
                "schema": STORE_SCHEMA,
                "protocol": CLAUSE_PROTOCOL,
                "event_stream_protocol": EVENT_STREAM_PROTOCOL,
                "sqlite_journal_contract": SQLITE_JOURNAL_CONTRACT,
                "max_records": str(self.max_records),
                "max_bytes": str(self.max_bytes),
                "lifecycle_identity_sha256": (
                    self.lifecycle.identity_sha256 if self.lifecycle is not None else ""
                ),
            }
            for key, value in expected.items():
                row = database.execute(
                    "SELECT value FROM metadata WHERE key=?", (key,)
                ).fetchone()
                if row is None:
                    database.execute(
                        "INSERT INTO metadata(key,value) VALUES(?,?)", (key, value)
                    )
                elif (
                    key == "lifecycle_identity_sha256"
                    and str(row["value"]) == ""
                    and value != ""
                ):
                    database.execute(
                        "UPDATE metadata SET value=? WHERE key=?", (value, key)
                    )
                elif str(row["value"]) != value:
                    raise IncrementalProofError(
                        f"incremental proof store metadata mismatch for {key}"
                    )
            database.execute(
                "INSERT OR IGNORE INTO proof_events(digest,formula_sha256) "
                "SELECT digest,formula_sha256 FROM records "
                "ORDER BY created,digest"
            )

    def _lock(self, *, exclusive: bool):
        class Lock:
            def __init__(inner, outer: "IncrementalProofStore") -> None:
                inner.outer = outer
                inner.descriptor = -1

            def __enter__(inner) -> None:
                no_follow = getattr(os, "O_NOFOLLOW", None)
                if no_follow is None:
                    raise OSError("O_NOFOLLOW is required for proof store locks")
                inner.descriptor = os.open(
                    inner.outer.lock_path,
                    os.O_RDWR | os.O_CREAT | no_follow | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                )
                metadata = os.fstat(inner.descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    os.close(inner.descriptor)
                    inner.descriptor = -1
                    raise IncrementalProofError("proof store lock identity is invalid")
                fcntl.flock(
                    inner.descriptor,
                    fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
                )

            def __exit__(inner, *_exc: object) -> None:
                if inner.descriptor >= 0:
                    fcntl.flock(inner.descriptor, fcntl.LOCK_UN)
                    os.close(inner.descriptor)

        return Lock(self)

    def _path(self, digest: str) -> Path:
        return self.objects / digest[:2] / f"{digest}.json"

    @contextmanager
    def _object_parent(self, digest: str, *, create: bool):
        """Open the CAS shard without following replaceable directory links."""
        no_follow = getattr(os, "O_NOFOLLOW", None)
        directory = getattr(os, "O_DIRECTORY", None)
        if no_follow is None or directory is None:
            raise OSError("O_NOFOLLOW and O_DIRECTORY are required for proof CAS")
        flags = os.O_RDONLY | no_follow | directory | getattr(os, "O_CLOEXEC", 0)
        root_descriptor = os.open(self.root, flags)
        objects_descriptor = -1
        shard_descriptor = -1
        try:
            objects_descriptor = os.open("objects", flags, dir_fd=root_descriptor)
            if create:
                try:
                    os.mkdir(digest[:2], mode=0o700, dir_fd=objects_descriptor)
                except FileExistsError:
                    pass
            shard_descriptor = os.open(digest[:2], flags, dir_fd=objects_descriptor)
            yield shard_descriptor, f"{digest}.json"
        finally:
            if shard_descriptor >= 0:
                os.close(shard_descriptor)
            if objects_descriptor >= 0:
                os.close(objects_descriptor)
            os.close(root_descriptor)

    @staticmethod
    def _read_object_at(directory_descriptor: int, name: str, max_bytes: int) -> bytes:
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise OSError("O_NOFOLLOW is required for proof CAS reads")
        descriptor = os.open(
            name,
            os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0),
            dir_fd=directory_descriptor,
        )
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
                raise IncrementalProofError(
                    "proof object is not a bounded regular file"
                )
            content = bytearray()
            while len(content) <= max_bytes:
                chunk = os.read(
                    descriptor,
                    min(1024 * 1024, max_bytes + 1 - len(content)),
                )
                if not chunk:
                    break
                content.extend(chunk)
            after = os.fstat(descriptor)
            entry = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
            identity = (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            )
            if (
                len(content) > max_bytes
                or identity
                != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                )
                or (before.st_dev, before.st_ino) != (entry.st_dev, entry.st_ino)
                or not stat.S_ISREG(entry.st_mode)
            ):
                raise IncrementalProofError("proof object changed during stable read")
            return bytes(content)
        finally:
            os.close(descriptor)

    def _read_object(self, digest: str, max_bytes: int) -> bytes:
        with self._object_parent(digest, create=False) as (directory, name):
            return self._read_object_at(directory, name, max_bytes)

    def _record_lifecycle(self, record: Mapping[str, Any], size: int) -> None:
        if self.lifecycle is None:
            return
        self.lifecycle.record_artifact(
            ArtifactRef("sat-proof", str(record["record_sha256"])),
            encoded_bytes=size,
            edges=tuple(
                ArtifactRef("sat-proof", str(item["receipt_sha256"]))
                for item in record["imports"]
            ),
            lease=self.lifecycle_lease,
        )

    def _publish_file(self, digest: str, encoded: bytes) -> None:
        with self._object_parent(digest, create=True) as (directory, name):
            try:
                existing = self._read_object_at(directory, name, MAX_RECORD_BYTES)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                if existing != encoded:
                    raise IncrementalProofError("proof CAS object content changed")
                return
            descriptor = -1
            temporary = ""
            for nonce in range(128):
                temporary = f".proof-{os.getpid()}-{time.time_ns()}-{nonce}"
                try:
                    descriptor = os.open(
                        temporary,
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(os, "O_CLOEXEC", 0),
                        0o600,
                        dir_fd=directory,
                    )
                    break
                except FileExistsError:
                    continue
            if descriptor < 0:
                raise IncrementalProofError(
                    "cannot allocate a proof CAS temporary object"
                )
            try:
                view = memoryview(encoded)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("short proof CAS write")
                    view = view[written:]
                os.fsync(descriptor)
                os.close(descriptor)
                descriptor = -1
                try:
                    os.link(
                        temporary,
                        name,
                        src_dir_fd=directory,
                        dst_dir_fd=directory,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    pass
                if self._read_object_at(directory, name, MAX_RECORD_BYTES) != encoded:
                    raise IncrementalProofError("proof CAS publication lost identity")
                os.fsync(directory)
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass

    def publish(self, record: Mapping[str, Any]) -> tuple[str, bool]:
        operation = (
            self.lifecycle.operation() if self.lifecycle is not None else nullcontext()
        )
        with operation:
            return self._publish(record)

    def _publish(self, record: Mapping[str, Any]) -> tuple[str, bool]:
        max_variable = _bounded_int(
            record.get("max_variable"), "proof max variable", 1, 20_000_000
        )
        normalized = normalize_clause_record(record, max_variable=max_variable)
        digest = str(normalized["record_sha256"])
        encoded = _canonical_json(normalized)
        if len(encoded) > MAX_RECORD_BYTES:
            raise IncrementalProofError("proof record exceeds its byte limit")
        path = self._path(digest)
        relative = str(path.relative_to(self.root))
        now = time.time()
        with self._lock(exclusive=True):
            with self._connect() as database:
                row = database.execute(
                    "SELECT encoded_bytes, object_path FROM records WHERE digest=?",
                    (digest,),
                ).fetchone()
                if row is not None:
                    if (
                        int(row["encoded_bytes"]) != len(encoded)
                        or str(row["object_path"]) != relative
                    ):
                        raise IncrementalProofError("proof index identity changed")
                    if self._read_object(digest, MAX_RECORD_BYTES) != encoded:
                        raise IncrementalProofError("indexed proof object changed")
                    database.execute(
                        "UPDATE records SET last_seen=MAX(last_seen,?) WHERE digest=?",
                        (now, digest),
                    )
                    self._record_lifecycle(normalized, len(encoded))
                    return digest, False
                counts = database.execute(
                    "SELECT COUNT(*),COALESCE(SUM(encoded_bytes),0) FROM records"
                ).fetchone()
                if (
                    int(counts[0]) >= self.max_records
                    or int(counts[1]) + len(encoded) > self.max_bytes
                ):
                    raise IncrementalProofError(
                        "incremental proof store quota exceeded"
                    )
                self._publish_file(digest, encoded)
                database.execute(
                    "INSERT INTO records VALUES(?,?,?,?,?,?,?)",
                    (
                        digest,
                        normalized["formula_sha256"],
                        _canonical_json(normalized["shared_clause"]).decode("ascii"),
                        len(encoded),
                        relative,
                        now,
                        now,
                    ),
                )
                database.execute(
                    "INSERT INTO proof_events(digest,formula_sha256) VALUES(?,?)",
                    (digest, normalized["formula_sha256"]),
                )
                self._record_lifecycle(normalized, len(encoded))
        return digest, True

    def attest_encoded_object(
        self,
        digest: str,
        expected_encoded_sha256: str,
    ) -> int:
        """Stable-read one CAS object and match its previously checked bytes."""
        return self.attest_encoded_objects({digest: expected_encoded_sha256})

    def attest_encoded_objects(
        self,
        expected_objects: Mapping[str, str],
    ) -> int:
        """Batch-attest a bounded proof import closure under one store lock."""
        if not isinstance(expected_objects, Mapping) or len(expected_objects) > (
            MAX_REPLAY_CACHE_ENTRIES
        ):
            raise IncrementalProofError("proof attestation set is not bounded")
        normalized = tuple(
            sorted(
                (
                    _hex_digest(digest, "proof record"),
                    _hex_digest(encoded_sha256, "encoded proof object"),
                )
                for digest, encoded_sha256 in expected_objects.items()
            )
        )
        total_bytes = 0
        with self._lock(exclusive=False):
            with self._connect() as database:
                for normalized_digest, expected_sha256 in normalized:
                    row = database.execute(
                        "SELECT encoded_bytes,object_path FROM records WHERE digest=?",
                        (normalized_digest,),
                    ).fetchone()
                    if row is None:
                        raise IncrementalProofError("proof record is unavailable")
                    expected_path = self._path(normalized_digest)
                    if str(row["object_path"]) != str(
                        expected_path.relative_to(self.root)
                    ):
                        raise IncrementalProofError(
                            "proof object path is not canonical"
                        )
                    encoded = self._read_object(normalized_digest, MAX_RECORD_BYTES)
                    if len(encoded) != int(row["encoded_bytes"]):
                        raise IncrementalProofError("proof object size changed")
                    if _digest(encoded) != expected_sha256:
                        raise IncrementalProofError(
                            "proof object changed after replay authorization"
                        )
                    total_bytes += len(encoded)
                now = time.time()
                database.executemany(
                    "UPDATE records SET last_seen=MAX(last_seen,?) WHERE digest=?",
                    ((now, digest) for digest, _sha256 in normalized),
                )
        return total_bytes

    def load(self, digest: str) -> dict[str, Any]:
        normalized_digest = _hex_digest(digest, "proof record")
        with self._lock(exclusive=False):
            with self._connect() as database:
                row = database.execute(
                    "SELECT encoded_bytes,object_path FROM records WHERE digest=?",
                    (normalized_digest,),
                ).fetchone()
                if row is None:
                    raise IncrementalProofError("proof record is unavailable")
                expected = self._path(normalized_digest)
                if str(row["object_path"]) != str(expected.relative_to(self.root)):
                    raise IncrementalProofError("proof object path is not canonical")
                encoded = self._read_object(normalized_digest, MAX_RECORD_BYTES)
                if len(encoded) != int(row["encoded_bytes"]):
                    raise IncrementalProofError("proof object size changed")
                try:
                    value = json.loads(encoded, object_pairs_hook=_reject_duplicates)
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise IncrementalProofError(
                        "proof object is not canonical JSON"
                    ) from error
                normalized = normalize_clause_record(
                    value,
                    max_variable=_bounded_int(
                        value.get("max_variable"), "proof max variable", 1, 20_000_000
                    ),
                )
                if (
                    _canonical_json(normalized) != encoded
                    or normalized["record_sha256"] != normalized_digest
                ):
                    raise IncrementalProofError(
                        "proof object canonical identity changed"
                    )
                database.execute(
                    "UPDATE records SET last_seen=MAX(last_seen,?) WHERE digest=?",
                    (time.time(), normalized_digest),
                )
                return normalized

    def records_for_formula(
        self, formula_sha256: str, *, limit: int = 64
    ) -> tuple[str, ...]:
        formula = _hex_digest(formula_sha256, "proof formula")
        bounded = _bounded_int(limit, "proof lookup limit", 1, 4096)
        with self._lock(exclusive=False):
            with self._connect() as database:
                return tuple(
                    str(row[0])
                    for row in database.execute(
                        "SELECT digest FROM records WHERE formula_sha256=? "
                        "ORDER BY last_seen DESC,digest LIMIT ?",
                        (formula, bounded),
                    )
                )

    def events_after(
        self,
        formula_sha256: str,
        *,
        after_sequence: int = 0,
        limit: int = 64,
    ) -> tuple[tuple[int, str], ...]:
        """Read an immutable formula-scoped suffix of the proof event stream."""
        formula = _hex_digest(formula_sha256, "proof event formula")
        cursor = _bounded_int(after_sequence, "proof event cursor", 0, (1 << 63) - 1)
        bounded = _bounded_int(limit, "proof event limit", 1, 4096)
        with self._lock(exclusive=False):
            with self._connect() as database:
                rows = database.execute(
                    "SELECT sequence,digest FROM proof_events "
                    "WHERE formula_sha256=? AND sequence>? "
                    "ORDER BY sequence LIMIT ?",
                    (formula, cursor, bounded),
                ).fetchall()
        return tuple((int(row[0]), str(row[1])) for row in rows)

    def event_at(self, sequence: int) -> tuple[str, str] | None:
        cursor = _bounded_int(sequence, "proof event sequence", 1, (1 << 63) - 1)
        with self._lock(exclusive=False):
            with self._connect() as database:
                row = database.execute(
                    "SELECT formula_sha256,digest FROM proof_events WHERE sequence=?",
                    (cursor,),
                ).fetchone()
        if row is None:
            return None
        return str(row[0]), str(row[1])

    def event_for_record(self, digest: str) -> tuple[int, str] | None:
        """Return the immutable event sequence and formula for one record."""
        normalized_digest = _hex_digest(digest, "proof record")
        with self._lock(exclusive=False):
            with self._connect() as database:
                row = database.execute(
                    "SELECT sequence,formula_sha256 FROM proof_events WHERE digest=?",
                    (normalized_digest,),
                ).fetchone()
        if row is None:
            return None
        return int(row[0]), str(row[1])

    def latest_event_sequence(self, formula_sha256: str) -> int:
        formula = _hex_digest(formula_sha256, "proof event formula")
        with self._lock(exclusive=False):
            with self._connect() as database:
                row = database.execute(
                    "SELECT COALESCE(MAX(sequence),0) FROM proof_events "
                    "WHERE formula_sha256=?",
                    (formula,),
                ).fetchone()
        return int(row[0])

    def stats(self) -> dict[str, int]:
        with self._lock(exclusive=False):
            with self._connect() as database:
                row = database.execute(
                    "SELECT COUNT(*),COALESCE(SUM(encoded_bytes),0) FROM records"
                ).fetchone()
                events = int(
                    database.execute("SELECT COUNT(*) FROM proof_events").fetchone()[0]
                )
                return {
                    "records": int(row[0]),
                    "bytes": int(row[1]),
                    "events": events,
                }

    def delete_lifecycle_artifact(
        self, kind: str, digest: str, expected_size: int
    ) -> int:
        if kind != "sat-proof":
            raise IncrementalProofError("invalid incremental proof artifact kind")
        normalized_digest = _hex_digest(digest, "proof record")
        size = _bounded_int(expected_size, "proof artifact size", 0, MAX_RECORD_BYTES)
        with self._lock(exclusive=True):
            with self._connect() as database:
                row = database.execute(
                    "SELECT encoded_bytes,object_path FROM records WHERE digest=?",
                    (normalized_digest,),
                ).fetchone()
                if row is None:
                    return 0
                if int(row["encoded_bytes"]) != size:
                    raise IncrementalProofError("proof lifecycle size changed")
                path = self._path(normalized_digest)
                if str(row["object_path"]) != str(path.relative_to(self.root)):
                    raise IncrementalProofError("proof lifecycle path changed")
                encoded = self._read_object(normalized_digest, MAX_RECORD_BYTES)
                if len(encoded) != size:
                    raise IncrementalProofError("proof lifecycle object size changed")
                value = json.loads(encoded, object_pairs_hook=_reject_duplicates)
                normalized = normalize_clause_record(
                    value,
                    max_variable=_bounded_int(
                        value.get("max_variable"),
                        "proof max variable",
                        1,
                        20_000_000,
                    ),
                )
                if normalized["record_sha256"] != normalized_digest:
                    raise IncrementalProofError("proof lifecycle identity changed")
                if _canonical_json(normalized) != encoded:
                    raise IncrementalProofError("proof lifecycle encoding changed")
                with self._object_parent(normalized_digest, create=False) as (
                    directory,
                    name,
                ):
                    os.unlink(name, dir_fd=directory)
                    os.fsync(directory)
                database.execute(
                    "DELETE FROM proof_events WHERE digest=?", (normalized_digest,)
                )
                database.execute(
                    "DELETE FROM records WHERE digest=?", (normalized_digest,)
                )
                return size

    def synchronize_lifecycle(self, *, max_entries: int) -> dict[str, int | bool]:
        if self.lifecycle is None:
            raise IncrementalProofError("incremental proof lifecycle is disabled")
        limit = _bounded_int(max_entries, "proof lifecycle scan limit", 1, 10_000_000)
        with self._lock(exclusive=False):
            with self._connect() as database:
                total = int(
                    database.execute("SELECT COUNT(*) FROM records").fetchone()[0]
                )
                rows = database.execute(
                    "SELECT digest,encoded_bytes FROM records ORDER BY digest LIMIT ?",
                    (limit,),
                ).fetchall()
            for row in rows:
                record = self.load(str(row["digest"]))
                self._record_lifecycle(record, int(row["encoded_bytes"]))
        return {
            "indexed": len(rows),
            "total": total,
            "complete": len(rows) == total,
        }


def _reject_duplicates(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise IncrementalProofError("proof JSON contains duplicate keys")
        result[key] = value
    return result
