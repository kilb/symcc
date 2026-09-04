from __future__ import annotations

# RUN: env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -p no:cacheprovider %s

import copy
import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from qfbv_incremental_proof import (  # noqa: E402
    IncrementalProofStore,
    make_rup_clause_record,
)
from qfbv_multirank_evaluation import (  # noqa: E402
    PROTOCOL,
    RANK_REPORT_SCHEMA,
    MultirankConfig,
    MultirankEvaluationError,
    aggregate_rank_reports,
    build_random_3sat_plan,
    content_digest,
    exchange_clauses,
    percentile_summary,
)
from qfbv_utility_pairing import (  # noqa: E402
    UTILITY_PAIRING_PROTOCOL,
    UtilityPairingCandidate,
    UtilityPairingController,
    UtilityPairingPolicy,
    formula_family_sha256,
)

sys.path.insert(0, str(ROOT / "benchmark"))
from check_qfbv_realtime_multirank_oracles import verify_trial  # noqa: E402
from check_qfbv_utility_pairing_oracles import verify_pair  # noqa: E402


LIBRARY_SHA256 = "a" * 64


def _named_digest(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _report(config: MultirankConfig, rank: int) -> dict:
    role = config.role(rank)
    rounds = []
    for ordinal in range(config.rounds):
        formula = f"{ordinal + 1:064x}"
        common = {
            "round": ordinal,
            "formula_sha256": formula,
            "cnf_sha256": f"{ordinal + 101:064x}",
        }
        if role == "coordinator":
            rounds.append({**common, "epoch_makespan_us": 100 + ordinal})
        elif role == "publisher":
            rounds.append({
                **common,
                "record_sha256": f"{ordinal + 201:064x}",
                "event_sequence": ordinal + 1,
                "publish_elapsed_us": 10 + ordinal,
                "created": True,
            })
        else:
            record = f"{ordinal + 201:064x}"
            ack = {"record_sha256": record}
            activity_fields = {}
            if config.track_clause_activity:
                ack["ack_sha256"] = f"{ordinal + 301:064x}"
                activity_fields = {
                    "activity_protocol": (
                        "symcc-qfbv-native-clause-activity-v1"
                    ),
                    "activity_unit": 1,
                    "activity_conflict": 0,
                    "activity_unactivated": 0,
                    "activity_receipts": [{
                        "record_sha256": record,
                        "ack_sha256": ack["ack_sha256"],
                        "kind": "unit",
                    }],
                }
            rounds.append({
                **common,
                "solve_result": 10,
                "solve_elapsed_us": 80 + ordinal,
                "notification_to_finish_us": 70 + ordinal,
                "checker_elapsed_us": 5 + ordinal,
                "imports_enqueued_before_wait_deadline": True,
                "imports_expected": 1,
                "imports_delivered": 1,
                "delivered_record_sha256": [record],
                "acks": [ack],
                "active_at_publication": config.mode == "active",
                "timed_out": False,
                "stream_error": "",
                "solve_error": "",
                "backpressure": 0,
                **activity_fields,
            })
    return {
        "schema": RANK_REPORT_SCHEMA,
        "protocol": PROTOCOL,
        "config_sha256": config.sha256,
        "rank": rank,
        "world_size": config.world_size,
        "role": role,
        "processor": "node-a",
        "library_sha256": LIBRARY_SHA256,
        "native_signature": "symcc-qfbv-realtime-v1|cadical-3.0.1-test",
        "proof_store_identity_sha256": "b" * 64,
        "rounds": rounds,
        "error": "",
    }


def _aggregate(config: MultirankConfig, reports: list[dict]) -> dict:
    return aggregate_rank_reports(
        config,
        reports,
        filesystem_qualification={
            "clean": True,
            "verified": False,
            "accepted": True,
            "scope": "same-host-subprocess-v1",
        },
        library_sha256=LIBRARY_SHA256,
    )


def _pairing_reports(config: MultirankConfig) -> list[dict]:
    assert config.utility_pairing and config.publishers == 2
    plans = [build_random_3sat_plan(config, ordinal)
             for ordinal in range(config.rounds)]
    families = [formula_family_sha256(plan.certificate) for plan in plans]
    assert len(set(families)) == 1
    records = [
        [
            _named_digest(f"record-{ordinal}-{publisher}")
            for publisher in range(config.publishers)
        ]
        for ordinal in range(config.rounds)
    ]
    controllers = {
        rank: UtilityPairingController(UtilityPairingPolicy())
        for rank in config.consumer_ranks
    }
    reports: list[dict] = []
    for rank in range(config.world_size):
        role = config.role(rank)
        rounds: list[dict] = []
        for ordinal, plan in enumerate(plans):
            common = {
                "round": ordinal,
                "formula_sha256": plan.formula_sha256,
                "cnf_sha256": str(plan.certificate["cnf_sha256"]),
            }
            if role == "coordinator":
                rounds.append({**common, "epoch_makespan_us": 100 + ordinal})
                continue
            if role == "publisher":
                publisher = config.publisher_ranks.index(rank)
                rounds.append({
                    **common,
                    "record_sha256": records[ordinal][publisher],
                    "event_sequence": ordinal * config.publishers + publisher + 1,
                    "publish_elapsed_us": 10 + ordinal,
                    "created": True,
                })
                continue

            controller = controllers[rank]
            stream_id = _named_digest(f"stream-{rank}-{ordinal}")
            decisions = []
            for publisher, record in enumerate(records[ordinal]):
                decisions.append(controller.consider(UtilityPairingCandidate(
                    record_sha256=record,
                    stream_id=stream_id,
                    publisher_worker=(
                        f"f434-publisher-rank-"
                        f"{config.publisher_ranks[publisher]}"
                    ),
                    consumer_worker=f"f434-consumer-rank-{rank}",
                    formula_family_sha256=families[ordinal],
                    event_sequence=(
                        ordinal * config.publishers + publisher + 1
                    ),
                    event_lag=config.publishers - publisher - 1,
                    checker_elapsed_us=0,
                )))
            outcomes = []
            delivered = []
            acks = []
            receipts = []
            unactivated = 0
            for publisher, decision in enumerate(decisions):
                if decision["action"] == "suppress":
                    continue
                record = records[ordinal][publisher]
                kind = "unit" if publisher == 0 else "unactivated"
                outcomes.append(controller.observe(
                    decision["decision_sha256"], outcome=kind
                ))
                delivered.append(record)
                ack = {
                    "record_sha256": record,
                    "ack_sha256": _named_digest(
                        f"ack-{rank}-{ordinal}-{publisher}"
                    ),
                }
                acks.append(ack)
                if kind == "unit":
                    receipts.append({
                        "record_sha256": record,
                        "ack_sha256": ack["ack_sha256"],
                        "kind": kind,
                    })
                else:
                    unactivated += 1
            pairing_evidence = {
                "backend_realtime_pairing_protocol": UTILITY_PAIRING_PROTOCOL,
                "backend_realtime_pairing_policy": controller.policy.as_dict(),
                "backend_realtime_pairing_policy_sha256": (
                    controller.policy.sha256
                ),
                "backend_realtime_pairing_formula_family_sha256": (
                    families[ordinal]
                ),
                "backend_realtime_pairing_consumer_worker": (
                    f"f434-consumer-rank-{rank}"
                ),
                "backend_realtime_pairing_decisions": decisions,
                "backend_realtime_pairing_outcomes": outcomes,
                "backend_realtime_pairing_action_counts": {
                    action: sum(item["action"] == action for item in decisions)
                    for action in ("admit", "suppress")
                },
                "backend_realtime_pairing_phase_counts": {
                    phase: sum(item["phase"] == phase for item in decisions)
                    for phase in ("explore", "exploit")
                },
                "backend_realtime_pairing_outcome_counts": {
                    name: sum(item["outcome"] == name for item in outcomes)
                    for name in (
                        "unit", "conflict", "unactivated",
                        "backpressure", "expired",
                    )
                },
                "backend_realtime_pairing_controller_snapshot": (
                    controller.snapshot()
                ),
            }
            rounds.append({
                **common,
                "solve_result": 10,
                "solve_elapsed_us": 80 + ordinal,
                "notification_to_finish_us": 70 + ordinal,
                "checker_elapsed_us": 5 + ordinal,
                "imports_enqueued_before_wait_deadline": True,
                "imports_expected": len(delivered),
                "imports_delivered": len(delivered),
                "delivered_record_sha256": delivered,
                "acks": acks,
                "activity_protocol": "symcc-qfbv-native-clause-activity-v1",
                "activity_unit": len(receipts),
                "activity_conflict": 0,
                "activity_unactivated": unactivated,
                "activity_receipts": receipts,
                "active_at_publication": True,
                "timed_out": False,
                "stream_error": "",
                "solve_error": "",
                "backpressure": 0,
                "stream_id": stream_id,
                "pairing_evidence": pairing_evidence,
            })
        reports.append({
            "schema": RANK_REPORT_SCHEMA,
            "protocol": PROTOCOL,
            "config_sha256": config.sha256,
            "rank": rank,
            "world_size": config.world_size,
            "role": role,
            "processor": "node-a",
            "library_sha256": LIBRARY_SHA256,
            "native_signature": "symcc-qfbv-realtime-v1|cadical-3.0.1-test",
            "proof_store_identity_sha256": "b" * 64,
            "rounds": rounds,
            "error": "",
        })
    return reports


def _seal_trial(result: dict) -> dict:
    result.update({
        "run_nonce": "1" * 32,
        "proof_root": "/tmp/proofs",
        "native_signature": "symcc-qfbv-realtime-v1|cadical-3.0.1",
        "acks_independently_replayed": result["delivered_imports"],
        "activity_receipts_independently_replayed": result[
            "clause_activity_receipts"
        ],
    })
    result["artifact_sha256"] = content_digest({
        key: value for key, value in result.items()
        if key != "artifact_sha256"
    })
    return result


def test_config_assigns_disjoint_roles_and_stable_identity() -> None:
    config = MultirankConfig(6, 2, 3, 41, 24, 104, "active")
    assert config.publisher_ranks == (1, 2)
    assert config.consumer_ranks == (3, 4, 5)
    assert [config.role(rank) for rank in range(6)] == [
        "coordinator", "publisher", "publisher",
        "consumer", "consumer", "consumer",
    ]
    assert len(config.sha256) == 64
    assert config.sha256 == MultirankConfig(
        6, 2, 3, 41, 24, 104, "active"
    ).sha256
    assert not config.track_clause_activity
    tracked = MultirankConfig(
        6, 2, 3, 41, 24, 104, "active",
        track_clause_activity=True,
    )
    assert tracked.track_clause_activity
    assert tracked.sha256 != config.sha256
    paired = MultirankConfig(
        6, 2, 3, 41, 24, 104, "active",
        track_clause_activity=True,
        utility_pairing=True,
    )
    assert paired.utility_pairing
    assert paired.sha256 != tracked.sha256
    with pytest.raises(MultirankEvaluationError):
        MultirankConfig(
            6, 2, 3, 41, 24, 104, "active",
            track_clause_activity="yes",
        )
    with pytest.raises(MultirankEvaluationError, match="activity"):
        MultirankConfig(
            6, 2, 3, 41, 24, 104, "active",
            utility_pairing=True,
        )
    with pytest.raises(MultirankEvaluationError, match="three rounds"):
        MultirankConfig(
            6, 2, 2, 41, 24, 104, "active",
            track_clause_activity=True,
            utility_pairing=True,
        )


@pytest.mark.parametrize(
    "arguments",
    [
        (2, 1, 1, 1, 24, 104, "active"),
        (4, 3, 1, 1, 24, 104, "active"),
        (4, 1, 0, 1, 24, 104, "active"),
        (4, 1, 1, 1, 24, 10, "active"),
        (4, 1, 1, 1, 24, 104, "unknown"),
    ],
)
def test_config_rejects_incomplete_experiments(arguments: tuple) -> None:
    with pytest.raises(MultirankEvaluationError):
        MultirankConfig(*arguments)


def test_random_3sat_plan_is_reproducible_and_round_scoped() -> None:
    config = MultirankConfig(4, 1, 2, 0xF434, 24, 104, "active")
    first = build_random_3sat_plan(config, 0)
    replay = build_random_3sat_plan(config, 0)
    second = build_random_3sat_plan(config, 1)
    assert first.certificate == replay.certificate
    assert first.clauses == replay.clauses
    assert first.formula_sha256 != second.formula_sha256
    assert formula_family_sha256(first.certificate) == formula_family_sha256(
        second.certificate
    )
    paired_config = MultirankConfig(
        4, 1, 3, 0xF434, 24, 104, "active",
        track_clause_activity=True,
        utility_pairing=True,
    )
    assert build_random_3sat_plan(paired_config, 0).formula_sha256 == (
        first.formula_sha256
    )
    clauses = exchange_clauses(first, 3)
    assert len(clauses) == len(set(clauses)) == 3
    assert all(clause in first.clauses for clause in clauses)


def test_proof_store_exposes_exact_event_and_uses_rollback_journal(tmp_path: Path) -> None:
    config = MultirankConfig(4, 1, 1, 9, 24, 104, "preloaded")
    plan = build_random_3sat_plan(config, 0)
    store = IncrementalProofStore(tmp_path / "proofs")
    record = make_rup_clause_record(
        plan,
        exchange_clauses(plan, 1)[0],
        source_worker="test-publisher",
        worker_epoch=0,
        sequence=1,
    )
    digest, created = store.publish(record)
    assert created
    sequence, formula = store.event_for_record(digest) or (0, "")
    assert sequence == 1
    assert formula == plan.formula_sha256
    assert store.event_at(sequence) == (formula, digest)
    assert store.event_for_record("f" * 64) is None
    with sqlite3.connect(store.db_path) as database:
        assert database.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert database.execute(
            "SELECT value FROM metadata WHERE key='sqlite_journal_contract'"
        ).fetchone()[0] == "delete-flock-v1"


def test_percentiles_are_nearest_rank_and_integer_bounded() -> None:
    assert percentile_summary([])["samples"] == 0
    summary = percentile_summary([100, 1, 3, 2, 4])
    assert summary == {
        "samples": 5,
        "minimum_us": 1,
        "median_us": 3,
        "p95_us": 100,
        "maximum_us": 100,
        "total_us": 110,
    }
    with pytest.raises(MultirankEvaluationError):
        percentile_summary([-1])


def test_aggregate_requires_exact_rank_event_and_ack_sets() -> None:
    config = MultirankConfig(4, 1, 2, 7, 24, 104, "active")
    reports = [_report(config, rank) for rank in range(config.world_size)]
    result = _aggregate(config, reports)
    assert result["status"] == "pass"
    assert result["expected_imports"] == 4
    assert result["delivered_imports"] == 4
    assert result["active_delivery_rate"] == 1.0
    assert result["scope"] == "local-host-mpi-mechanism"
    assert len(result["artifact_sha256"]) == 64

    mutations = []
    duplicate_rank = copy.deepcopy(reports)
    duplicate_rank[-1]["rank"] = 2
    mutations.append(duplicate_rank)
    wrong_role = copy.deepcopy(reports)
    wrong_role[2]["role"] = "publisher"
    mutations.append(wrong_role)
    missing_ack = copy.deepcopy(reports)
    missing_ack[2]["rounds"][0]["delivered_record_sha256"] = []
    mutations.append(missing_ack)
    wrong_formula = copy.deepcopy(reports)
    wrong_formula[3]["rounds"][1]["formula_sha256"] = "f" * 64
    mutations.append(wrong_formula)
    rank_error = copy.deepcopy(reports)
    rank_error[1]["error"] = "publisher failed"
    mutations.append(rank_error)
    for mutated in mutations:
        with pytest.raises(MultirankEvaluationError):
            _aggregate(config, mutated)


def test_aggregate_rejects_unqualified_shared_state() -> None:
    config = MultirankConfig(4, 1, 1, 7, 24, 104, "preloaded")
    reports = [_report(config, rank) for rank in range(config.world_size)]
    with pytest.raises(MultirankEvaluationError):
        aggregate_rank_reports(
            config,
            reports,
            filesystem_qualification={
                "clean": True,
                "verified": False,
                "accepted": False,
            },
            library_sha256=LIBRARY_SHA256,
        )


def test_aggregate_conserves_clause_activity_and_rejects_tamper() -> None:
    config = MultirankConfig(
        4, 1, 2, 7, 24, 104, "active", track_clause_activity=True
    )
    reports = [_report(config, rank) for rank in range(config.world_size)]
    result = _aggregate(config, reports)
    assert result["clause_activity_enabled"]
    assert result["clause_activity_unit"] == 4
    assert result["clause_activity_conflict"] == 0
    assert result["clause_activity_unactivated"] == 0
    assert result["clause_activity_receipts"] == 4
    assert result["clause_activation_rate"] == 1.0

    tampered = copy.deepcopy(reports)
    tampered[2]["rounds"][0]["activity_receipts"][0]["ack_sha256"] = (
        "f" * 64
    )
    with pytest.raises(MultirankEvaluationError, match="activity identity"):
        _aggregate(config, tampered)


def test_aggregate_replays_three_round_utility_pairing_and_suppression() -> None:
    config = MultirankConfig(
        5, 2, 3, 0xF437, 24, 104, "active",
        track_clause_activity=True,
        utility_pairing=True,
    )
    reports = _pairing_reports(config)
    result = _aggregate(config, reports)
    assert result["utility_pairing_enabled"]
    assert result["utility_pairing_opportunities"] == 12
    assert result["utility_pairing_admitted"] == 10
    assert result["utility_pairing_suppressed"] == 2
    assert result["utility_pairing_explore"] == 8
    assert result["utility_pairing_exploit"] == 4
    assert result["utility_pairing_outcomes"] == {
        "unit": 6,
        "conflict": 0,
        "unactivated": 4,
        "backpressure": 0,
        "expired": 0,
    }
    assert result["delivered_imports"] == 10
    assert result["opportunity_delivery_rate"] == pytest.approx(5 / 6)

    tampered = copy.deepcopy(reports)
    decision = tampered[3]["rounds"][2]["pairing_evidence"][
        "backend_realtime_pairing_decisions"
    ][1]
    decision["action"] = "admit"
    with pytest.raises(MultirankEvaluationError, match="pairing evidence"):
        _aggregate(config, tampered)


def test_paired_ablation_verifier_requires_identical_formula_and_activation() -> None:
    paired_config = MultirankConfig(
        5, 2, 3, 0xF437, 24, 104, "active",
        track_clause_activity=True,
        utility_pairing=True,
    )
    pairing_reports = _pairing_reports(paired_config)
    pairing = _seal_trial(_aggregate(paired_config, pairing_reports))

    baseline_config = MultirankConfig(
        5, 2, 3, 0xF437, 24, 104, "active",
        track_clause_activity=True,
    )
    baseline_reports = copy.deepcopy(pairing_reports)
    for report in baseline_reports:
        report["config_sha256"] = baseline_config.sha256
        if report["role"] != "consumer":
            continue
        for ordinal, row in enumerate(report["rounds"]):
            row.pop("pairing_evidence")
            row.pop("stream_id")
            publisher_record = baseline_reports[2]["rounds"][ordinal][
                "record_sha256"
            ]
            if publisher_record not in row["delivered_record_sha256"]:
                ack_sha = _named_digest(
                    f"baseline-ack-{report['rank']}-{ordinal}"
                )
                row["delivered_record_sha256"].append(publisher_record)
                row["acks"].append({
                    "record_sha256": publisher_record,
                    "ack_sha256": ack_sha,
                })
                row["activity_unactivated"] += 1
            row["imports_expected"] = 2
            row["imports_delivered"] = 2
    baseline = _seal_trial(_aggregate(baseline_config, baseline_reports))
    verified = verify_pair(baseline, pairing)
    assert verified["opportunities"] == 12
    assert verified["suppressed"] == 2
    assert verified["baseline_activated"] == verified["pairing_activated"] == 6
    assert verified["baseline_unactivated"] == 6
    assert verified["pairing_unactivated"] == 4

    tampered = copy.deepcopy(pairing)
    tampered["rank_reports"][0]["rounds"][0]["formula_sha256"] = "f" * 64
    tampered["artifact_sha256"] = content_digest({
        key: value for key, value in tampered.items()
        if key != "artifact_sha256"
    })
    with pytest.raises(MultirankEvaluationError, match="formulas"):
        verify_pair(baseline, tampered)


def test_oracle_trial_verifier_recomputes_sealed_identity() -> None:
    config = MultirankConfig(4, 1, 1, 7, 24, 104, "active")
    result = _aggregate(
        config, [_report(config, rank) for rank in range(config.world_size)]
    )
    result.update({
        "run_nonce": "1" * 32,
        "proof_root": "/tmp/proofs",
        "native_signature": "symcc-qfbv-realtime-v1|cadical-3.0.1",
        "acks_independently_replayed": result["expected_imports"],
    })
    result["artifact_sha256"] = __import__(
        "qfbv_multirank_evaluation"
    ).content_digest({
        key: value for key, value in result.items()
        if key != "artifact_sha256"
    })
    verified = verify_trial(result, mode="active")
    assert verified["expected_imports"] == 2
    tampered = copy.deepcopy(result)
    tampered["delivered_imports"] = 0
    with pytest.raises(MultirankEvaluationError):
        verify_trial(tampered, mode="active")
