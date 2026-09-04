#!/usr/bin/env python3
"""Reproduce F355 public qualification containment invariants."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
from unittest import mock


ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "util"))

import mpi_filesystem_qualification as qualification  # noqa: E402
from distributed_state import probe_shared_state_filesystem  # noqa: E402


class SingleCommunicator:
    def Get_rank(self):
        return 0

    def Get_size(self):
        return 1


class UnexpectedCommunicator:
    def Get_rank(self):
        raise LookupError("injected communicator adapter failure")

    def Get_size(self):
        return 2


class BrokenTextError(Exception):
    def __str__(self):
        raise RuntimeError("broken exception rendering")


class DeterministicClock:
    def __init__(self):
        self.ticks = 0

    def __call__(self):
        self.ticks += 1
        return self.ticks / 1000.0


def qualify(comm, capability, root, epoch, **overrides):
    arguments = {
        "root": root,
        "epoch": epoch,
        "global_rank": 0,
        "expected_master_ranks": (0, 1),
        "processor_name": "node-a",
        "qualification_generation": 7,
        "timeout": 0.025,
    }
    arguments.update(overrides)
    return qualification.qualify_mpi_cluster_advisory_lock(
        comm,
        capability,
        **arguments,
    )


def summarize(result, capability, *, expected_capability=None):
    if expected_capability is None:
        expected_capability = capability
    return {
        "clean": result.clean,
        "verified": result.verified,
        "capability_retained": result.capability is capability,
        "capability_matches_expected": result.capability == expected_capability,
        "capability_is_none": result.capability is None,
        "members": result.members,
        "representatives": result.representatives,
        "rounds": result.rounds,
        "contention_checks": result.contention_checks,
        "release_checks": result.release_checks,
        "identity_checks": result.identity_checks,
        "elapsed": result.elapsed,
        "error": result.error,
        "qualification_generation": result.qualification_generation,
        "proof_transcript": result.proof_transcript,
    }


def main():
    epoch = hashlib.sha256(b"F355-total-qualification-boundary").hexdigest()
    with tempfile.TemporaryDirectory() as temporary:
        capability = probe_shared_state_filesystem(temporary, timeout=2.0)
        clock = DeterministicClock()
        clean = qualification.qualify_mpi_cluster_advisory_lock(
            SingleCommunicator(),
            capability,
            root=temporary,
            epoch=epoch,
            global_rank=0,
            expected_master_ranks=(0,),
            processor_name="node-a",
            qualification_generation=7,
            timeout=0.025,
            monotonic=clock,
        )
        communicator = qualify(
            UnexpectedCommunicator(), capability, temporary, epoch)

        def broken_clock():
            raise LookupError("injected monotonic failure")

        clock = qualify(
            SingleCommunicator(),
            capability,
            temporary,
            epoch,
            monotonic=broken_clock,
        )

        with mock.patch.object(
            qualification,
            "_qualify_mpi_cluster_advisory_lock",
            side_effect=LookupError(("x\n\0" * 2048)),
        ):
            bounded = qualify(object(), capability, temporary, epoch)

        with mock.patch.object(
            qualification,
            "_qualify_mpi_cluster_advisory_lock",
            side_effect=BrokenTextError(),
        ):
            rendered = qualify(object(), capability, temporary, epoch)

        with mock.patch.object(
            qualification,
            "_qualify_mpi_cluster_advisory_lock",
            side_effect=ValueError("invalid internal input"),
        ):
            normalized = qualify(
                object(),
                object(),
                temporary,
                epoch,
                qualification_generation=True,
            )

        base_exception = ""
        with mock.patch.object(
            qualification,
            "_qualify_mpi_cluster_advisory_lock",
            side_effect=KeyboardInterrupt("injected process control"),
        ):
            try:
                qualify(object(), capability, temporary, epoch)
            except BaseException as error:
                base_exception = f"{type(error).__name__}: {error}"

    expected_clean_capability = replace(
        capability,
        cluster_lock_members=((0, "node-a"),),
        cluster_lock_representatives=(0,),
    )
    clean_summary = summarize(
        clean,
        capability,
        expected_capability=expected_clean_capability,
    )
    communicator_summary = summarize(communicator, capability)
    clock_summary = summarize(clock, capability)
    bounded_summary = summarize(bounded, capability)
    rendered_summary = summarize(rendered, capability)
    normalized_summary = summarize(normalized, capability)
    prefix = "cluster lock qualification raised: "
    checks = {
        "clean_protocol_semantics_are_preserved": bool(
            clean_summary["clean"] is True
            and clean_summary["verified"] is False
            and clean_summary["capability_matches_expected"] is True
            and clean_summary["members"] == ((0, "node-a"),)
            and clean_summary["qualification_generation"] == 7
            and clean_summary["elapsed"] == 0.001
        ),
        "communicator_exception_is_contained": bool(
            communicator_summary["clean"] is False
            and communicator_summary["verified"] is False
            and communicator_summary["error"]
            == prefix + "injected communicator adapter failure"
        ),
        "generation_is_preserved": bool(
            communicator_summary["qualification_generation"] == 7
        ),
        "capability_is_retained": bool(
            communicator_summary["capability_retained"] is True
        ),
        "clock_exception_is_contained": bool(
            clock_summary["error"] == prefix + "injected monotonic failure"
            and clock_summary["elapsed"] == 0.0
        ),
        "diagnostic_is_bounded_and_sanitized": bool(
            len(bounded_summary["error"]) == 512
            and "\n" not in bounded_summary["error"]
            and "\0" not in bounded_summary["error"]
            and bounded_summary["error"].startswith(prefix)
        ),
        "rendering_failure_uses_type_name": bool(
            rendered_summary["error"] == prefix + "BrokenTextError"
        ),
        "invalid_capability_is_normalized": bool(
            normalized_summary["capability_is_none"] is True
        ),
        "invalid_generation_is_normalized": bool(
            normalized_summary["qualification_generation"] == 0
        ),
        "base_exception_is_not_swallowed": bool(
            base_exception == "KeyboardInterrupt: injected process control"
        ),
    }
    artifact = {
        "schema": "symcc-f355-total-qualification-boundary-evidence-v1",
        "feature": "F355",
        "actual_mpi_transport": False,
        "actual_multihost_filesystem": False,
        "solver_or_campaign_executed": False,
        "epoch": epoch,
        "checks_passed": sum(checks.values()),
        "checks_total": len(checks),
        "checks": checks,
        "clean_protocol": clean_summary,
        "communicator_exception": communicator_summary,
        "clock_exception": clock_summary,
        "bounded_diagnostic": bounded_summary,
        "rendering_exception": rendered_summary,
        "normalized_inputs": normalized_summary,
        "base_exception": base_exception,
        "claim_boundary": (
            "Local public-API containment evidence only; no real MPI, "
            "multi-host filesystem, solver, coverage, campaign, bug, or "
            "LAVA-M uplift claim."
        ),
    }
    print(json.dumps(artifact, indent=2, sort_keys=True, allow_nan=False))
    return 0 if all(checks.values()) and len(checks) == 10 else 1


if __name__ == "__main__":
    raise SystemExit(main())
