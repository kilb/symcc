#!/usr/bin/env python3
"""Reproduce F356 qualification-input observation invariants."""

from __future__ import annotations

from collections import defaultdict, deque
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time


ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "util"))

import mpi_filesystem_qualification as qualification  # noqa: E402
from distributed_state import probe_shared_state_filesystem  # noqa: E402


class CompletedRequest:
    def Test(self):
        return True


class MessageBus:
    def __init__(self, size):
        self.size = size
        self.lock = threading.Lock()
        self.queues = defaultdict(deque)

    def send(self, source, destination, tag, message):
        with self.lock:
            self.queues[(destination, source, tag)].append(message)

    def probe(self, destination, source, tag):
        with self.lock:
            return bool(self.queues[(destination, source, tag)])

    def receive(self, destination, source, tag):
        with self.lock:
            return self.queues[(destination, source, tag)].popleft()

    def communicator(self, rank):
        return Communicator(self, rank)


class Communicator:
    def __init__(self, bus, rank):
        self.bus = bus
        self.rank = rank

    def Get_rank(self):
        return self.rank

    def Get_size(self):
        return self.bus.size

    def isend(self, message, *, dest, tag):
        self.bus.send(self.rank, dest, tag, message)
        return CompletedRequest()

    def iprobe(self, *, source, tag):
        return self.bus.probe(self.rank, source, tag)

    def recv(self, *, source, tag):
        return self.bus.receive(self.rank, source, tag)


class BrokenTextError(Exception):
    def __str__(self):
        raise RuntimeError("broken exception rendering")


def raising(error):
    def operation():
        raise error

    return operation


def summarize_observation(observation, expected_capability):
    return {
        "capability_present": observation.capability is not None,
        "capability_retained": observation.capability is expected_capability,
        "processor_name": observation.processor_name,
        "error": observation.error,
    }


def summarize_result(result):
    return {
        "clean": result.clean,
        "verified": result.verified,
        "members": result.members,
        "representatives": result.representatives,
        "rounds": result.rounds,
        "contention_checks": result.contention_checks,
        "release_checks": result.release_checks,
        "identity_checks": result.identity_checks,
        "elapsed": result.elapsed,
        "error": result.error,
        "proof_transcript": result.proof_transcript,
    }


def main():
    epoch = hashlib.sha256(
        b"F356-collective-safe-qualification-observation"
    ).hexdigest()
    events = []
    with tempfile.TemporaryDirectory() as temporary:
        capability = probe_shared_state_filesystem(temporary, timeout=2.0)
        clean = qualification.observe_cluster_lock_qualification_inputs(
            capability_probe=lambda: capability,
            processor_name_probe=lambda: "node-a",
        )

        def failed_capability():
            events.append("capability")
            raise LookupError("injected filesystem adapter failure")

        def failed_processor():
            events.append("processor")
            raise LookupError("injected processor adapter failure")

        both_failed = qualification.observe_cluster_lock_qualification_inputs(
            capability_probe=failed_capability,
            processor_name_probe=failed_processor,
        )
        processor_failed = (
            qualification.observe_cluster_lock_qualification_inputs(
                capability,
                processor_name_probe=raising(
                    LookupError("injected processor adapter failure")
                ),
            )
        )
        invalid = qualification.observe_cluster_lock_qualification_inputs(
            object(),
            processor_name_probe=lambda: object(),
        )
        upstream = qualification.observe_cluster_lock_qualification_inputs(
            capability,
            processor_name_probe=lambda: "node-a",
            local_error="pre-renewal heartbeat failed\n\x00",
        )
        bounded = qualification.observe_cluster_lock_qualification_inputs(
            capability,
            processor_name_probe=raising(LookupError("x\n\x00" * 300)),
        )
        rendered = qualification.observe_cluster_lock_qualification_inputs(
            capability,
            processor_name_probe=raising(BrokenTextError()),
        )

        capability_signal = ""
        try:
            qualification.observe_cluster_lock_qualification_inputs(
                capability_probe=raising(
                    KeyboardInterrupt("capability process control")
                ),
                processor_name_probe=lambda: "node-a",
            )
        except BaseException as error:
            capability_signal = f"{type(error).__name__}: {error}"
        processor_signal = ""
        try:
            qualification.observe_cluster_lock_qualification_inputs(
                capability,
                processor_name_probe=raising(
                    KeyboardInterrupt("processor process control")
                ),
            )
        except BaseException as error:
            processor_signal = f"{type(error).__name__}: {error}"

        bus = MessageBus(2)
        observations = (clean, processor_failed)
        results = [None, None]
        thread_errors = []

        def run(rank):
            observation = observations[rank]
            try:
                results[rank] = qualification.qualify_mpi_cluster_advisory_lock(
                    bus.communicator(rank),
                    observation.capability,
                    root=temporary,
                    epoch=epoch,
                    global_rank=rank,
                    expected_master_ranks=(0, 1),
                    processor_name=observation.processor_name,
                    local_error=observation.error,
                    timeout=1.0,
                    monotonic=lambda: 0.0,
                    sleep=lambda _delay: time.sleep(0.0001),
                )
            except BaseException as error:
                thread_errors.append(f"{type(error).__name__}: {error}")

        threads = [threading.Thread(target=run, args=(rank,), daemon=True)
                   for rank in (0, 1)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2.0)
        threads_alive = sum(thread.is_alive() for thread in threads)

    clean_summary = summarize_observation(clean, capability)
    both_summary = summarize_observation(both_failed, capability)
    processor_summary = summarize_observation(processor_failed, capability)
    invalid_summary = summarize_observation(invalid, capability)
    upstream_summary = summarize_observation(upstream, capability)
    bounded_summary = summarize_observation(bounded, capability)
    rendered_summary = summarize_observation(rendered, capability)
    result_summaries = [
        summarize_result(result) if result is not None else None
        for result in results
    ]
    expected_error = (
        "master 1 local probe failed: processor identity probe raised: "
        "injected processor adapter failure"
    )
    checks = {
        "clean_observation_is_preserved": bool(
            clean_summary["capability_retained"] is True
            and clean_summary["processor_name"] == "node-a"
            and clean_summary["error"] == ""
        ),
        "filesystem_exception_is_contained": bool(
            both_summary["capability_present"] is False
            and "filesystem capability probe raised: "
            "injected filesystem adapter failure" in both_summary["error"]
        ),
        "processor_observation_still_runs": events == ["capability", "processor"],
        "processor_exception_is_contained": bool(
            processor_summary["capability_retained"] is True
            and processor_summary["processor_name"] == ""
            and processor_summary["error"]
            == "processor identity probe raised: "
            "injected processor adapter failure"
        ),
        "invalid_results_are_normalized": bool(
            invalid_summary["capability_present"] is False
            and invalid_summary["processor_name"] == ""
            and "invalid" in invalid_summary["error"]
        ),
        "upstream_error_is_sanitized": bool(
            upstream_summary["error"] == "pre-renewal heartbeat failed ?"
        ),
        "diagnostic_is_bounded": bool(
            len(bounded_summary["error"]) == 512
            and "\n" not in bounded_summary["error"]
            and "\x00" not in bounded_summary["error"]
        ),
        "rendering_failure_uses_type_name": bool(
            rendered_summary["error"]
            == "processor identity probe raised: BrokenTextError"
        ),
        "capability_base_exception_propagates": bool(
            capability_signal
            == "KeyboardInterrupt: capability process control"
        ),
        "processor_base_exception_propagates": bool(
            processor_signal
            == "KeyboardInterrupt: processor process control"
        ),
        "collective_failure_converges": bool(
            threads_alive == 0
            and not thread_errors
            and all(result is not None for result in result_summaries)
            and all(result["clean"] is False for result in result_summaries)
            and all(result["error"] == expected_error
                    for result in result_summaries)
        ),
        "failed_processor_record_is_schema_valid": bool(
            all(
                result is not None
                and result["members"] == ((0, "node-a"), (1, ""))
                and result["verified"] is False
                and result["rounds"] == 0
                and result["proof_transcript"] == ""
                for result in result_summaries
            )
        ),
    }
    artifact = {
        "schema": "symcc-f356-collective-safe-observation-evidence-v1",
        "feature": "F356",
        "actual_mpi_transport": False,
        "synthetic_collective_transport": True,
        "actual_multihost_filesystem": False,
        "actual_local_filesystem_probe": True,
        "solver_or_campaign_executed": False,
        "epoch": epoch,
        "checks_passed": sum(checks.values()),
        "checks_total": len(checks),
        "checks": checks,
        "clean_observation": clean_summary,
        "both_observations_failed": both_summary,
        "processor_observation_failed": processor_summary,
        "invalid_observations": invalid_summary,
        "upstream_error": upstream_summary,
        "bounded_diagnostic": bounded_summary,
        "rendering_failure": rendered_summary,
        "capability_base_exception": capability_signal,
        "processor_base_exception": processor_signal,
        "event_order": events,
        "collective_results": result_summaries,
        "collective_thread_errors": thread_errors,
        "collective_threads_alive": threads_alive,
        "claim_boundary": (
            "Local observation and synthetic collective evidence only; no real "
            "MPI, multi-host filesystem, ULFM recovery, solver, coverage, "
            "campaign, bug, or LAVA-M uplift claim."
        ),
    }
    print(json.dumps(artifact, indent=2, sort_keys=True, allow_nan=False))
    return 0 if all(checks.values()) and len(checks) == 12 else 1


if __name__ == "__main__":
    raise SystemExit(main())
