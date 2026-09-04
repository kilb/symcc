#!/usr/bin/env python3
# RUN: %python %s --help >/dev/null
"""Randomized real-CaDiCaL oracle for F436 clause-activity receipts."""

from __future__ import annotations

import argparse
import copy
import hashlib
import itertools
import json
import random
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from qfbv_incremental_proof import (  # noqa: E402
    IncrementalProofChecker,
    IncrementalProofStore,
    make_rup_clause_record,
)
from qfbv_incremental_sat import bitblast_qfbv_query  # noqa: E402
from qfbv_multirank_evaluation import (  # noqa: E402
    MultirankConfig,
    build_random_3sat_plan,
)
from qfbv_realtime_stream import (  # noqa: E402
    CLAUSE_ACTIVITY_PROTOCOL,
    NativeRealtimeCadical,
    RealtimeClauseExchangeSession,
    RealtimeStreamError,
    verify_checked_import_ack,
    verify_clause_activity_receipt,
)


SCHEMA = "symcc-f436-clause-activity-oracle-v1"


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def _content_digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _signed_node(
    expressions: dict[str, dict[str, Any]], name: str, source: str, positive: bool
) -> str:
    if positive:
        return source
    expressions[name] = {
        "op": "lnot", "bits": 1, "children": [source], "attrs": {}
    }
    return name


def _plan(
    case: int,
    a_bit: int,
    x_bit: int,
    b_bit: int,
    a_positive: bool,
    x_positive: bool,
    b_positive: bool,
):
    expressions: dict[str, dict[str, Any]] = {
        "input": {
            "op": "read", "bits": 8, "children": [], "attrs": {"index": 0}
        },
        "one": {
            "op": "constant", "bits": 1, "children": [],
            "attrs": {"value_hex": "1"},
        },
    }
    for name, bit in (("a", a_bit), ("x", x_bit), ("b", b_bit)):
        expressions[f"{name}_bv"] = {
            "op": "extract", "bits": 1, "children": ["input"],
            "attrs": {"index": bit},
        }
        expressions[f"{name}_raw"] = {
            "op": "equal", "bits": 1,
            "children": [f"{name}_bv", "one"], "attrs": {},
        }
    a_node = _signed_node(expressions, "a_signed", "a_raw", a_positive)
    x_node = _signed_node(expressions, "x_signed", "x_raw", x_positive)
    b_node = _signed_node(expressions, "b_signed", "b_raw", b_positive)
    expressions["not_x"] = {
        "op": "lnot", "bits": 1, "children": [x_node], "attrs": {}
    }
    expressions["left"] = {
        "op": "lor", "bits": 1,
        "children": [a_node, x_node, b_node], "attrs": {},
    }
    expressions["right"] = {
        "op": "lor", "bits": 1,
        "children": [a_node, "not_x", b_node], "attrs": {},
    }
    expressions["main"] = {
        "op": "land", "bits": 1,
        "children": ["left", "right"], "attrs": {},
    }
    expressions["not_a"] = {
        "op": "lnot", "bits": 1, "children": [a_node], "attrs": {}
    }
    plan = bitblast_qfbv_query(
        f"f436-live-{case}", ["main", "not_a"], expressions,
        {"incremental": True},
    )
    input_bits = plan.input_literals[0][1]
    a_literal = input_bits[a_bit] if a_positive else -input_bits[a_bit]
    b_literal = input_bits[b_bit] if b_positive else -input_bits[b_bit]
    main_activation = plan.increments[0].activation_literal
    return plan, (-main_activation, a_literal, b_literal), b_literal


def _prepare(native: Any, plan: Any) -> None:
    for clause in plan.clauses:
        for literal in clause:
            native.add(int(literal))
        native.add(0)
    native.observe(plan.max_variable)
    for literal in plan.assumptions:
        native.assume(int(literal))


def _wait_for_import(native: Any, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while (
        native.stats()["imports_enqueued"] == 0
        and time.monotonic() < deadline
    ):
        time.sleep(0.0005)
    if native.stats()["imports_enqueued"] != 1:
        raise RuntimeError("F436 oracle did not enqueue its checked clause")


def _reseal(receipt: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(receipt)
    result.pop("activity_sha256", None)
    result["activity_sha256"] = _content_digest(result)
    return result


def _run_checked_case(
    *,
    case: int,
    specification: tuple[int, int, int, bool, bool, bool],
    owner: NativeRealtimeCadical,
    store: IncrementalProofStore,
    checker: IncrementalProofChecker,
    seed: int,
) -> tuple[dict[str, Any], bool]:
    plan, clause, expected_unit = _plan(case, *specification)
    record = make_rup_clause_record(
        plan,
        clause,
        source_worker="f436-oracle-publisher",
        worker_epoch=seed,
        sequence=case + 1,
    )
    digest, created = store.publish(record)
    if not created:
        raise RuntimeError("F436 oracle proof record was not freshly published")
    native = owner.new_context(
        max_learned_length=0,
        max_imports=1,
        max_import_literals=64,
        max_learned=0,
    )
    session: RealtimeClauseExchangeSession | None = None
    try:
        _prepare(native, plan)
        session = RealtimeClauseExchangeSession(
            plan,
            native,
            store,
            checker,
            native_signature=owner.signature,
            source_worker="f436-oracle-consumer",
            worker_epoch=seed,
            next_sequence=iter(range(1_000_000, 2_000_000)).__next__,
            stream_ordinal=case + 1,
            max_imports=1,
            max_events=8,
            max_learned=0,
            poll_interval_ms=1,
            track_clause_activity=True,
        )
        session.start()
        _wait_for_import(native)
        solve_result = native.solve()
        evidence = session.finish(native.stats()["solve_generation"])
        session = None
        if solve_result != 10:
            raise RuntimeError("F436 checked activity changed SAT status")
        if (
            evidence["backend_realtime_import_delivered"] != 1
            or evidence["backend_realtime_clause_activity_unit"] != 1
            or evidence["backend_realtime_clause_activity_conflict"] != 0
            or evidence["backend_realtime_clause_activity_unactivated"] != 0
        ):
            raise RuntimeError("F436 checked activity accounting changed")
        ack = evidence["backend_realtime_import_acks"][0]
        authorization = verify_checked_import_ack(plan, ack, checker=checker)
        receipt = evidence["backend_realtime_clause_activity_receipts"][0]
        activity = verify_clause_activity_receipt(
            plan, receipt, ack=ack, checker=checker
        )
        if (
            authorization.record_sha256 != digest
            or activity.record_sha256 != digest
            or receipt["unit_literal"] != expected_unit
            or store.event_at(int(ack["event_sequence"]))
            != (authorization.formula_sha256, digest)
        ):
            raise RuntimeError("F436 checked activity identity changed")
        literal_value = native.val(abs(expected_unit)) > 0
        if literal_value != (expected_unit > 0):
            raise RuntimeError("F436 unit literal is false in the SAT model")

        tampered = copy.deepcopy(receipt)
        tampered["falsifying_assignments"] = (
            list(tampered["falsifying_assignments"]) + [expected_unit]
        )
        tamper_rejected = False
        try:
            verify_clause_activity_receipt(
                plan, _reseal(tampered), ack=ack, checker=checker
            )
        except RealtimeStreamError:
            tamper_rejected = True
        return {
            "case": case,
            "formula_sha256": plan.formula_sha256,
            "record_sha256": digest,
            "unit_literal": int(receipt["unit_literal"]),
            "decision_level": int(receipt["decision_level"]),
            "proof_steps": authorization.proof_steps,
            "propagations": authorization.propagation_count,
        }, tamper_rejected
    finally:
        if session is not None:
            session.abort()
        native.close()


def _native_state_case(
    owner: NativeRealtimeCadical,
    *,
    name: str,
    base: tuple[tuple[int, ...], ...],
    assumptions: tuple[int, ...],
    clause: tuple[int, ...],
    expected_kind: str | None,
    expected_code: int,
) -> dict[str, Any]:
    native = owner.new_context(
        max_learned_length=0,
        max_imports=1,
        max_import_literals=16,
        max_learned=0,
    )
    try:
        for item in base:
            for literal in item:
                native.add(literal)
            native.add(0)
        native.observe(2)
        for literal in assumptions:
            native.assume(literal)
        native.enable_activity(True)
        if not native.enqueue(1, clause):
            raise RuntimeError(f"F436 {name} import was rejected")
        result = native.solve()
        ack = native.dequeue_ack()
        activity = native.dequeue_activity(16)
        stats = native.stats()
        kind = activity[4] if activity is not None else None
        if (
            result != expected_code
            or ack != (1, 1, 1)
            or kind != expected_kind
            or stats["imports_delivered"] != 1
            or stats["activity_queued"] != 0
            or stats["imports_tracked"] != 1
        ):
            raise RuntimeError(f"F436 native state case {name} changed")
        return {
            "name": name,
            "solve_result": result,
            "kind": kind or "unactivated",
            "decision_level": int(activity[3]) if activity is not None else -1,
            "unit_literal": int(activity[5]) if activity is not None else 0,
            "witness": list(activity[6]) if activity is not None else [],
        }
    finally:
        native.close()


def _run_lifecycle_fence(
    owner: NativeRealtimeCadical, seed: int
) -> dict[str, Any]:
    config = MultirankConfig(
        world_size=3,
        publishers=1,
        rounds=1,
        seed=seed,
        variables=200,
        clauses=860,
        mode="active",
    )
    plan = build_random_3sat_plan(config, 0)
    native = owner.new_context(
        max_learned_length=0,
        max_imports=0,
        max_import_literals=1,
        max_learned=0,
    )
    solve_result: list[int] = []
    solve_error: list[str] = []

    def solve() -> None:
        try:
            solve_result.append(native.solve())
        except Exception as error:  # pragma: no cover - native boundary
            solve_error.append(f"{type(error).__name__}: {error}")

    thread: threading.Thread | None = None
    try:
        _prepare(native, plan)
        thread = threading.Thread(target=solve, name="f436-lifecycle-solve")
        thread.start()
        deadline = time.monotonic() + 5.0
        while native.stats()["solving"] != 1 and time.monotonic() < deadline:
            time.sleep(0.0005)
        if native.stats()["solving"] != 1:
            raise RuntimeError("F436 lifecycle oracle missed the active solve")
        rejected: list[str] = []
        for name, operation in (
            ("enable-activity", lambda: native.enable_activity(True)),
            ("reset-queues", native.reset_queues),
            ("observe", lambda: native.observe(plan.max_variable)),
        ):
            try:
                operation()
            except RealtimeStreamError:
                rejected.append(name)
        native.terminate()
        thread.join(timeout=5.0)
        if thread.is_alive() or solve_error or solve_result != [0]:
            raise RuntimeError("F436 lifecycle solve did not terminate cleanly")
        native.clear_termination()
        native.enable_activity(True)
        native.enable_activity(False)
        native.reset_queues()
        if rejected != ["enable-activity", "reset-queues", "observe"]:
            raise RuntimeError("F436 lifecycle mutation was not fenced")
        return {
            "active_mutations_rejected": rejected,
            "termination_result": solve_result[0],
            "post_termination_recovery": True,
        }
    finally:
        if thread is not None and thread.is_alive():
            native.terminate()
            thread.join(timeout=5.0)
        native.close()


def run(library: Path, *, cases: int, seed: int) -> dict[str, Any]:
    started = time.monotonic_ns()
    owner = NativeRealtimeCadical(library)
    if owner.activity_protocol != CLAUSE_ACTIVITY_PROTOCOL:
        raise RuntimeError("F436 oracle requires the native activity protocol")
    specifications = list(itertools.product(
        itertools.permutations(range(8), 3),
        itertools.product((False, True), repeat=3),
    ))
    flattened = [bits + signs for bits, signs in specifications]
    random.Random(seed).shuffle(flattened)
    if cases > len(flattened):
        raise ValueError("F436 oracle case count exceeds unique formulas")
    checked_rows: list[dict[str, Any]] = []
    tamper_rejections = 0
    with tempfile.TemporaryDirectory(prefix="symcc-f436-oracle-") as directory:
        store = IncrementalProofStore(Path(directory) / "proofs")
        checker = IncrementalProofChecker(store)
        for case, specification in enumerate(flattened[:cases]):
            row, rejected = _run_checked_case(
                case=case,
                specification=specification,
                owner=owner,
                store=store,
                checker=checker,
                seed=seed,
            )
            checked_rows.append(row)
            tamper_rejections += int(rejected)
        proof_stats = store.stats()

    state_matrix = [
        _native_state_case(
            owner,
            name="assumption-unit",
            base=(), assumptions=(-1,), clause=(1, 2),
            expected_kind="unit", expected_code=10,
        ),
        _native_state_case(
            owner,
            name="root-unit",
            base=((-1,),), assumptions=(), clause=(1, 2),
            expected_kind="unit", expected_code=10,
        ),
        _native_state_case(
            owner,
            name="root-conflict",
            base=((-1,), (-2,)), assumptions=(), clause=(1, 2),
            expected_kind="conflict", expected_code=20,
        ),
        _native_state_case(
            owner,
            name="satisfied-unactivated",
            base=((1,),), assumptions=(), clause=(1, 2),
            expected_kind=None, expected_code=10,
        ),
    ]
    lifecycle_fence = _run_lifecycle_fence(owner, seed)
    body: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "pass",
        "cases": cases,
        "seed": seed,
        "native_signature": owner.signature,
        "activity_protocol": owner.activity_protocol,
        "library": str(library),
        "library_sha256": _sha256(library),
        "checked_imports_delivered": cases,
        "unit_activity_receipts": cases,
        "activity_receipts_replayed": cases,
        "tampered_receipts_rejected": tamper_rejections,
        "minimum_decision_level": min(
            row["decision_level"] for row in checked_rows
        ),
        "maximum_decision_level": max(
            row["decision_level"] for row in checked_rows
        ),
        "proof_records": int(proof_stats["records"]),
        "proof_events": int(proof_stats["events"]),
        "state_matrix": state_matrix,
        "lifecycle_fence": lifecycle_fence,
        "case_digest": _content_digest(checked_rows),
        "elapsed_us": (time.monotonic_ns() - started) // 1000,
        "claim_boundary": (
            "real-CaDiCaL checked-clause activity mechanism oracle; unit/conflict "
            "means semantic activation on the observed native trail, not unique "
            "causal attribution or fuzzing coverage/solver-speedup evidence"
        ),
    }
    body["artifact_sha256"] = _content_digest(body)
    return verify_oracle_result(body)


def verify_oracle_result(raw: Mapping[str, Any]) -> dict[str, Any]:
    if raw.get("schema") != SCHEMA or raw.get("status") != "pass":
        raise ValueError("F436 oracle did not pass")
    body = dict(raw)
    artifact = body.pop("artifact_sha256", None)
    if type(artifact) is not str or _content_digest(body) != artifact:
        raise ValueError("F436 oracle identity changed")
    integer_fields = (
        "cases",
        "checked_imports_delivered",
        "unit_activity_receipts",
        "activity_receipts_replayed",
        "tampered_receipts_rejected",
        "proof_records",
        "proof_events",
    )
    if any(type(raw.get(field)) is not int for field in integer_fields):
        raise ValueError("F436 oracle counters must be exact integers")
    cases = int(raw["cases"])
    if cases <= 0 or any(int(raw[field]) != cases for field in integer_fields[1:]):
        raise ValueError("F436 oracle did not preserve checked activity counts")
    matrix = raw.get("state_matrix")
    if not isinstance(matrix, list) or {
        (row.get("name"), row.get("kind"))
        for row in matrix if isinstance(row, Mapping)
    } != {
        ("assumption-unit", "unit"),
        ("root-unit", "unit"),
        ("root-conflict", "conflict"),
        ("satisfied-unactivated", "unactivated"),
    }:
        raise ValueError("F436 native state matrix changed")
    if raw.get("activity_protocol") != CLAUSE_ACTIVITY_PROTOCOL:
        raise ValueError("F436 activity protocol changed")
    lifecycle = raw.get("lifecycle_fence")
    if not isinstance(lifecycle, Mapping) or (
        lifecycle.get("active_mutations_rejected")
        != ["enable-activity", "reset-queues", "observe"]
        or lifecycle.get("termination_result") != 0
        or lifecycle.get("post_termination_recovery") is not True
    ):
        raise ValueError("F436 lifecycle fence changed")
    return dict(raw)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", required=True, type=Path)
    parser.add_argument("--cases", type=int, default=128)
    parser.add_argument("--seed", type=int, default=0xF436)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    cases = args.cases
    if type(cases) is not int or not 1 <= cases <= 1024:
        parser.error("--cases must be in [1, 1024]")
    result = run(args.library.resolve(strict=True), cases=cases, seed=args.seed)
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(encoded, encoding="ascii")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
