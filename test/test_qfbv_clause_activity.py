from __future__ import annotations

# RUN: env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -p no:cacheprovider %s

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))
sys.path.insert(0, str(ROOT / "benchmark"))

from check_qfbv_clause_activity_oracles import (  # noqa: E402
    CLAUSE_ACTIVITY_PROTOCOL,
    SCHEMA,
    verify_oracle_result,
)


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")).hexdigest()


def _result(cases: int = 4) -> dict[str, object]:
    body: dict[str, object] = {
        "schema": SCHEMA,
        "status": "pass",
        "cases": cases,
        "seed": 0xF436,
        "native_signature": "symcc-qfbv-realtime-v1|cadical-3.0.1-test",
        "activity_protocol": CLAUSE_ACTIVITY_PROTOCOL,
        "library": "/tmp/libactivity.so",
        "library_sha256": "a" * 64,
        "checked_imports_delivered": cases,
        "unit_activity_receipts": cases,
        "activity_receipts_replayed": cases,
        "tampered_receipts_rejected": cases,
        "minimum_decision_level": 2,
        "maximum_decision_level": 2,
        "proof_records": cases,
        "proof_events": cases,
        "state_matrix": [
            {"name": "assumption-unit", "kind": "unit"},
            {"name": "root-unit", "kind": "unit"},
            {"name": "root-conflict", "kind": "conflict"},
            {"name": "satisfied-unactivated", "kind": "unactivated"},
        ],
        "lifecycle_fence": {
            "active_mutations_rejected": [
                "enable-activity", "reset-queues", "observe"
            ],
            "termination_result": 0,
            "post_termination_recovery": True,
        },
        "case_digest": "b" * 64,
        "elapsed_us": 100,
        "claim_boundary": "mechanism only",
    }
    body["artifact_sha256"] = _digest(body)
    return body


def test_oracle_summary_is_strict_and_replayable() -> None:
    result = _result()
    assert verify_oracle_result(result)["cases"] == 4

    tampered = copy.deepcopy(result)
    tampered["unit_activity_receipts"] = 3
    with pytest.raises(ValueError, match="identity"):
        verify_oracle_result(tampered)

    tampered = copy.deepcopy(result)
    tampered["cases"] = "4"
    body = dict(tampered)
    body.pop("artifact_sha256")
    tampered["artifact_sha256"] = _digest(body)
    with pytest.raises(ValueError, match="exact integers"):
        verify_oracle_result(tampered)


def test_oracle_rejects_missing_native_state() -> None:
    result = _result()
    result["state_matrix"] = list(result["state_matrix"])[:-1]
    body = dict(result)
    body.pop("artifact_sha256")
    result["artifact_sha256"] = _digest(body)
    with pytest.raises(ValueError, match="state matrix"):
        verify_oracle_result(result)


def test_oracle_rejects_lifecycle_fence_tamper() -> None:
    result = _result()
    result["lifecycle_fence"]["post_termination_recovery"] = False
    body = dict(result)
    body.pop("artifact_sha256")
    result["artifact_sha256"] = _digest(body)
    with pytest.raises(ValueError, match="lifecycle fence"):
        verify_oracle_result(result)
