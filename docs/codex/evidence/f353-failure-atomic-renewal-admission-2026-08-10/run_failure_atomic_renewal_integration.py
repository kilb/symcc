#!/usr/bin/env python3
"""Reproduce F353 renewal completion admission invariants."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from unittest import mock


ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "util"))

import mpi_filesystem_qualification as qualification  # noqa: E402
from distributed_state import probe_shared_state_filesystem  # noqa: E402


def make_controller(epoch, capability):
    return qualification.ClusterLockRenewalController(
        epoch=epoch,
        interval=5.0,
        timeout=2.0,
        completed_at=0.0,
        require_configuration_consensus=False,
        expected_master_ranks=(0, 1),
        expected_capability=capability,
    )


def make_result(epoch, capability):
    members = ((0, "node-a"), (1, "node-b"))
    representatives = (0, 1)
    upgraded = qualification.qualify_shared_filesystem_cluster_lock(
        capability,
        members=members,
        representatives=representatives,
        rounds=2,
        contention_checks=2,
        release_checks=2,
        identity_checks=2,
    )
    transcript = qualification._qualification_proof_transcript(
        epoch,
        1,
        members,
        representatives,
        2,
        2,
        2,
        2,
    )
    return qualification.ClusterLockQualificationResult(
        clean=True,
        verified=True,
        capability=upgraded,
        members=members,
        representatives=representatives,
        rounds=2,
        contention_checks=2,
        release_checks=2,
        elapsed=0.125,
        identity_checks=2,
        qualification_generation=1,
        proof_transcript=transcript,
    )


def complete_once(epoch, capability, result):
    controller = make_controller(epoch, capability)
    request = controller.begin_request()
    before = controller.snapshot()
    returned = None
    error = ""
    try:
        returned = controller.complete(
            request["generation"],
            result,
            completed_at=5.125,
        )
    except Exception as caught:  # evidence records unexpected escapes
        error = f"{type(caught).__name__}: {caught}"
    return {
        "returned": returned,
        "error": error,
        "before": before,
        "after": controller.snapshot(),
    }


def injected_failure(epoch, capability, result, target, message):
    controller = make_controller(epoch, capability)
    controller.begin_request()
    before = controller.snapshot()
    error = ""
    with mock.patch.object(
        qualification,
        target,
        side_effect=RuntimeError(message),
    ):
        try:
            controller.complete(1, result, completed_at=5.125)
        except Exception as caught:
            error = f"{type(caught).__name__}: {caught}"
    return {
        "error": error,
        "state_unchanged": controller.snapshot() == before,
        "before": before,
        "after": controller.snapshot(),
    }


def main():
    epoch = hashlib.sha256(b"F353-failure-atomic-renewal").hexdigest()
    with tempfile.TemporaryDirectory() as temporary:
        local = probe_shared_state_filesystem(temporary, timeout=2.0)
        valid = make_result(epoch, local)
        assert valid.capability is not None
        capability = valid.capability

        accepted = complete_once(epoch, capability, valid)
        malformed = {
            "non_iterable_members": complete_once(
                epoch, capability, replace(valid, members=object())),
            "malformed_member_tuple": complete_once(
                epoch, capability, replace(valid, members=(0,))),
            "non_iterable_representatives": complete_once(
                epoch, capability, replace(valid, representatives=object())),
            "non_numeric_elapsed": complete_once(
                epoch, capability, replace(valid, elapsed=object())),
            "nan_elapsed": complete_once(
                epoch, capability, replace(valid, elapsed=float("nan"))),
        }
        validator_failure = injected_failure(
            epoch,
            capability,
            valid,
            "_qualification_result_matches_request",
            "injected validator failure",
        )
        scheduler_failure = injected_failure(
            epoch,
            capability,
            valid,
            "_scheduled_renewal_interval",
            "injected scheduler failure",
        )

        boolean_controller = make_controller(epoch, capability)
        boolean_controller.begin_request()
        boolean_before = boolean_controller.snapshot()
        boolean_error = ""
        try:
            boolean_controller.complete(True, valid, completed_at=5.125)
        except Exception as caught:
            boolean_error = f"{type(caught).__name__}: {caught}"
        boolean_generation = {
            "error": boolean_error,
            "state_unchanged": boolean_controller.snapshot() == boolean_before,
            "before": boolean_before,
            "after": boolean_controller.snapshot(),
        }

    def rejected_and_conserved(outcome):
        after = outcome["after"]
        return bool(
            outcome["returned"] is False
            and outcome["error"] == ""
            and after["generation"] == 1
            and after["in_flight_generation"] == 0
            and after["attempts"] == 1
            and after["successes"] == 0
            and after["failures"] == 1
            and after["attempts"] == after["successes"] + after["failures"]
        )

    checks = {
        "valid_result_commits_once": bool(
            accepted["returned"] is True
            and accepted["error"] == ""
            and accepted["after"]["attempts"] == 1
            and accepted["after"]["successes"] == 1
            and accepted["after"]["failures"] == 0
        ),
        **{
            f"{name}_is_total_failure": rejected_and_conserved(outcome)
            for name, outcome in malformed.items()
        },
        "validator_exception_is_failure_atomic": bool(
            validator_failure["state_unchanged"]
            and validator_failure["error"]
            == "RuntimeError: injected validator failure"
        ),
        "scheduler_exception_is_failure_atomic": bool(
            scheduler_failure["state_unchanged"]
            and scheduler_failure["error"]
            == "RuntimeError: injected scheduler failure"
        ),
        "boolean_generation_is_rejected_atomically": bool(
            boolean_generation["state_unchanged"]
            and boolean_generation["error"]
            == "RuntimeError: cluster lock renewal generation mismatch"
        ),
        "all_committed_attempts_conserve_outcomes": all(
            outcome["after"]["attempts"]
            == outcome["after"]["successes"] + outcome["after"]["failures"]
            for outcome in (accepted, *malformed.values())
        ),
    }
    artifact = {
        "schema": "symcc-f353-failure-atomic-renewal-evidence-v1",
        "feature": "F353",
        "actual_mpi_transport": False,
        "actual_multihost_filesystem": False,
        "solver_or_campaign_executed": False,
        "epoch": epoch,
        "proof_transcript": valid.proof_transcript,
        "checks_passed": sum(checks.values()),
        "checks_total": len(checks),
        "checks": checks,
        "valid_completion": accepted,
        "malformed_completions": malformed,
        "unexpected_failures": {
            "validator": validator_failure,
            "scheduler": scheduler_failure,
            "boolean_generation": boolean_generation,
        },
        "claim_boundary": (
            "Local production-controller mechanism evidence only; no real MPI, "
            "multi-host filesystem, solver, coverage, campaign, bug, or LAVA-M "
            "uplift claim."
        ),
    }
    print(json.dumps(artifact, indent=2, sort_keys=True, allow_nan=False))
    return 0 if all(checks.values()) and len(checks) == 10 else 1


if __name__ == "__main__":
    raise SystemExit(main())
