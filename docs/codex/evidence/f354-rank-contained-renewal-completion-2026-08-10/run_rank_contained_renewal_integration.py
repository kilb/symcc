#!/usr/bin/env python3
"""Reproduce F354 rank-contained renewal completion invariants."""

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


def complete_once(controller, result):
    request = controller.begin_request()
    before = controller.snapshot()
    successful, error = qualification.complete_cluster_lock_renewal(
        controller,
        request["generation"],
        result,
        completed_at=5.125,
    )
    return {
        "successful": successful,
        "error": error,
        "before": before,
        "after": controller.snapshot(),
    }


def injected_exception(epoch, capability, result, exception):
    controller = make_controller(epoch, capability)
    controller.begin_request()
    before = controller.snapshot()
    with mock.patch.object(
        qualification,
        "_scheduled_renewal_interval",
        side_effect=exception,
    ):
        successful, error = qualification.complete_cluster_lock_renewal(
            controller,
            1,
            result,
            completed_at=5.125,
        )
    return {
        "successful": successful,
        "error": error,
        "state_unchanged": controller.snapshot() == before,
        "before": before,
        "after": controller.snapshot(),
    }


def main():
    epoch = hashlib.sha256(b"F354-rank-contained-renewal").hexdigest()
    with tempfile.TemporaryDirectory() as temporary:
        local = probe_shared_state_filesystem(temporary, timeout=2.0)
        valid = make_result(epoch, local)
        assert valid.capability is not None
        capability = valid.capability

        accepted = complete_once(make_controller(epoch, capability), valid)
        rejected = complete_once(
            make_controller(epoch, capability),
            replace(valid, verified=False),
        )

        overflow_controller = make_controller(epoch, capability)
        overflow_controller.total_elapsed = float.fromhex(
            "0x1.fffffffffffffp+1023")
        overflow = complete_once(
            overflow_controller,
            replace(valid, elapsed=float.fromhex("0x1.fffffffffffffp+1023")),
        )

        unexpected = injected_exception(
            epoch,
            capability,
            valid,
            LookupError("injected rank-local failure"),
        )
        bounded = injected_exception(
            epoch,
            capability,
            valid,
            RuntimeError("x" * 4096),
        )

        base_controller = make_controller(epoch, capability)
        base_controller.begin_request()
        base_before = base_controller.snapshot()
        base_exception = ""
        with mock.patch.object(
            qualification,
            "_scheduled_renewal_interval",
            side_effect=KeyboardInterrupt("injected process control"),
        ):
            try:
                qualification.complete_cluster_lock_renewal(
                    base_controller,
                    1,
                    valid,
                    completed_at=5.125,
                )
            except BaseException as error:
                base_exception = f"{type(error).__name__}: {error}"
        base_signal = {
            "error": base_exception,
            "state_unchanged": base_controller.snapshot() == base_before,
        }

    checks = {
        "valid_proof_commits_success": bool(
            accepted["successful"] is True
            and accepted["error"] == ""
            and accepted["after"]["attempts"] == 1
            and accepted["after"]["successes"] == 1
            and accepted["after"]["failures"] == 0
        ),
        "proof_rejection_commits_failure": bool(
            rejected["successful"] is False
            and rejected["error"] == ""
            and rejected["after"]["attempts"] == 1
            and rejected["after"]["successes"] == 0
            and rejected["after"]["failures"] == 1
        ),
        "arithmetic_exception_is_contained": bool(
            overflow["successful"] is False
            and overflow["error"]
            == "cluster lock renewal elapsed total overflow"
        ),
        "arithmetic_exception_preserves_snapshot": bool(
            overflow["after"] == overflow["before"]
        ),
        "unexpected_exception_is_contained": bool(
            unexpected["successful"] is False
            and unexpected["error"] == "injected rank-local failure"
        ),
        "unexpected_exception_preserves_snapshot": bool(
            unexpected["state_unchanged"]
        ),
        "diagnostic_is_bounded": bool(
            bounded["successful"] is False
            and bounded["state_unchanged"]
            and bounded["error"] == "x" * 512
        ),
        "invalid_controller_is_rejected": bool(
            qualification.complete_cluster_lock_renewal(
                object(), 1, valid, completed_at=5.125)
            == (False, "invalid cluster lock renewal controller")
        ),
        "base_exception_is_not_swallowed": bool(
            base_signal["state_unchanged"]
            and base_signal["error"]
            == "KeyboardInterrupt: injected process control"
        ),
    }
    artifact = {
        "schema": "symcc-f354-rank-contained-renewal-evidence-v1",
        "feature": "F354",
        "actual_mpi_transport": False,
        "actual_multihost_filesystem": False,
        "solver_or_campaign_executed": False,
        "epoch": epoch,
        "proof_transcript": valid.proof_transcript,
        "checks_passed": sum(checks.values()),
        "checks_total": len(checks),
        "checks": checks,
        "valid_completion": accepted,
        "proof_rejection": rejected,
        "arithmetic_exception": overflow,
        "unexpected_exception": unexpected,
        "bounded_diagnostic": bounded,
        "base_exception": base_signal,
        "claim_boundary": (
            "Local production-controller containment evidence only; no real "
            "MPI, multi-host filesystem, solver, coverage, campaign, bug, or "
            "LAVA-M uplift claim."
        ),
    }
    print(json.dumps(artifact, indent=2, sort_keys=True, allow_nan=False))
    return 0 if all(checks.values()) and len(checks) == 9 else 1


if __name__ == "__main__":
    raise SystemExit(main())
