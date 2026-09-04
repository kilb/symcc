#!/usr/bin/env python3
"""Reproduce F357 heartbeat and completion-clock containment invariants."""

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


def failed_result(generation):
    return qualification.ClusterLockQualificationResult(
        clean=False,
        verified=False,
        capability=None,
        members=(),
        representatives=(),
        rounds=0,
        contention_checks=0,
        release_checks=0,
        elapsed=0.25,
        qualification_generation=generation,
    )


def controller(epoch):
    return qualification.ClusterLockRenewalController(
        epoch=epoch,
        interval=5.0,
        timeout=2.0,
        completed_at=0.0,
        require_configuration_consensus=False,
    )


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
        "qualification_generation": result.qualification_generation,
        "proof_transcript": result.proof_transcript,
    }


def main():
    epoch = hashlib.sha256(
        b"F357-renewal-heartbeat-clock-containment"
    ).hexdigest()
    first = hashlib.sha256(b"lost-one").hexdigest()
    second = hashlib.sha256(b"lost-two").hexdigest()
    provider_calls = []

    def clean_provider():
        provider_calls.append("clean")
        return first, second

    def failed_provider():
        provider_calls.append("failed")
        raise LookupError("injected heartbeat adapter failure")

    clean_heartbeat = qualification.observe_work_lease_heartbeat(
        clean_provider)
    failed_heartbeat = qualification.observe_work_lease_heartbeat(
        failed_provider)
    invalid_heartbeat = qualification.observe_work_lease_heartbeat(
        lambda: (first, first)
    )
    bounded_heartbeat = qualification.observe_work_lease_heartbeat(
        raising(LookupError("x\n\x00" * 300))
    )
    rendered_heartbeat = qualification.observe_work_lease_heartbeat(
        raising(BrokenTextError())
    )
    heartbeat_signal = ""
    try:
        qualification.observe_work_lease_heartbeat(
            raising(KeyboardInterrupt("heartbeat process control"))
        )
    except BaseException as error:
        heartbeat_signal = f"{type(error).__name__}: {error}"

    completed_controller = controller(epoch)
    completed_generation = completed_controller.begin_request()["generation"]
    completion_clock_calls = []

    def completion_clock():
        completion_clock_calls.append("clock")
        return 5.0

    completed_success, completed_error = (
        qualification.complete_cluster_lock_renewal(
            completed_controller,
            completed_generation,
            failed_result(completed_generation),
            monotonic=completion_clock,
        )
    )
    completed_snapshot = completed_controller.snapshot()

    clock_failure_controller = controller(epoch)
    clock_failure_generation = (
        clock_failure_controller.begin_request()["generation"]
    )
    clock_failure_before = clock_failure_controller.snapshot()
    clock_success, clock_error = qualification.complete_cluster_lock_renewal(
        clock_failure_controller,
        clock_failure_generation,
        failed_result(clock_failure_generation),
        monotonic=raising(LookupError("injected completion clock failure")),
    )
    clock_failure_after = clock_failure_controller.snapshot()
    completion_signal = ""
    try:
        qualification.complete_cluster_lock_renewal(
            clock_failure_controller,
            clock_failure_generation,
            failed_result(clock_failure_generation),
            monotonic=raising(
                KeyboardInterrupt("completion process control")
            ),
        )
    except BaseException as error:
        completion_signal = f"{type(error).__name__}: {error}"

    with tempfile.TemporaryDirectory() as temporary:
        capability = probe_shared_state_filesystem(temporary, timeout=2.0)
        rank_heartbeats = (
            qualification.observe_work_lease_heartbeat(lambda: ()),
            qualification.observe_work_lease_heartbeat(
                raising(LookupError("injected heartbeat adapter failure"))
            ),
        )
        observations = tuple(
            qualification.observe_cluster_lock_qualification_inputs(
                capability,
                processor_name_probe=lambda rank=rank: f"node-{rank}",
                local_error=(
                    f"pre-renewal {rank_heartbeats[rank].error}"
                    if rank_heartbeats[rank].error
                    else ""
                ),
            )
            for rank in (0, 1)
        )
        bus = MessageBus(2)
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
                    qualification_generation=1,
                    local_error=observation.error,
                    timeout=1.0,
                    monotonic=lambda: 0.0,
                    sleep=lambda _delay: time.sleep(0.0001),
                )
            except BaseException as error:
                thread_errors.append(f"{type(error).__name__}: {error}")

        threads = [
            threading.Thread(target=run, args=(rank,), daemon=True)
            for rank in (0, 1)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2.0)
        threads_alive = sum(thread.is_alive() for thread in threads)

    result_summaries = [
        summarize_result(result) if result is not None else None
        for result in results
    ]
    collective_completions = []
    for rank, result in enumerate(results):
        if result is None:
            collective_completions.append(None)
            continue
        rank_controller = controller(epoch)
        generation = rank_controller.begin_request()["generation"]
        successful, error = qualification.complete_cluster_lock_renewal(
            rank_controller,
            generation,
            result,
            monotonic=lambda rank=rank: 10.0 + rank,
        )
        collective_completions.append({
            "successful": successful,
            "error": error,
            "snapshot": rank_controller.snapshot(),
        })

    expected_collective_error = (
        "master 1 local probe failed: pre-renewal work lease heartbeat "
        "failed: injected heartbeat adapter failure"
    )
    checks = {
        "clean_heartbeat_is_preserved": bool(
            clean_heartbeat.lost_lease_count == 2
            and clean_heartbeat.error == ""
        ),
        "heartbeat_exception_is_contained": bool(
            failed_heartbeat.lost_lease_count == 0
            and failed_heartbeat.error
            == "work lease heartbeat failed: "
            "injected heartbeat adapter failure"
        ),
        "heartbeat_providers_are_called_once": provider_calls
        == ["clean", "failed"],
        "invalid_heartbeat_is_rejected": bool(
            invalid_heartbeat.lost_lease_count == 0
            and invalid_heartbeat.error
            == "work lease heartbeat returned an invalid result"
        ),
        "heartbeat_diagnostic_is_bounded": bool(
            len(bounded_heartbeat.error) == 512
            and "\n" not in bounded_heartbeat.error
            and "\x00" not in bounded_heartbeat.error
        ),
        "heartbeat_rendering_failure_uses_type_name": bool(
            rendered_heartbeat.error
            == "work lease heartbeat failed: BrokenTextError"
        ),
        "heartbeat_base_exception_propagates": heartbeat_signal
        == "KeyboardInterrupt: heartbeat process control",
        "completion_clock_is_called_once": completion_clock_calls == ["clock"],
        "proof_rejection_commits_one_failure": bool(
            completed_success is False
            and completed_error == ""
            and completed_snapshot["attempts"] == 1
            and completed_snapshot["failures"] == 1
            and completed_snapshot["in_flight_generation"] == 0
        ),
        "completion_clock_exception_is_contained": bool(
            clock_success is False
            and clock_error == "injected completion clock failure"
        ),
        "completion_clock_failure_keeps_prestate": (
            clock_failure_after == clock_failure_before
        ),
        "completion_base_exception_propagates": completion_signal
        == "KeyboardInterrupt: completion process control",
        "collective_heartbeat_failure_converges": bool(
            threads_alive == 0
            and not thread_errors
            and all(result is not None for result in result_summaries)
            and all(
                result["clean"] is False
                and result["verified"] is False
                and result["members"]
                == ((0, "node-0"), (1, "node-1"))
                and result["rounds"] == 0
                and result["proof_transcript"] == ""
                and result["error"] == expected_collective_error
                for result in result_summaries
            )
        ),
        "collective_completion_is_single_attempt": bool(
            all(completion is not None for completion in collective_completions)
            and all(
                completion["successful"] is False
                and completion["error"] == ""
                and completion["snapshot"]["attempts"] == 1
                and completion["snapshot"]["failures"] == 1
                and completion["snapshot"]["in_flight_generation"] == 0
                for completion in collective_completions
            )
        ),
    }
    artifact = {
        "schema": "symcc-f357-renewal-heartbeat-clock-evidence-v1",
        "feature": "F357",
        "actual_mpi_transport": False,
        "synthetic_collective_transport": True,
        "actual_multihost_filesystem": False,
        "actual_local_filesystem_probe": True,
        "solver_or_campaign_executed": False,
        "epoch": epoch,
        "checks_passed": sum(checks.values()),
        "checks_total": len(checks),
        "checks": checks,
        "clean_heartbeat": clean_heartbeat.__dict__,
        "failed_heartbeat": failed_heartbeat.__dict__,
        "invalid_heartbeat": invalid_heartbeat.__dict__,
        "bounded_heartbeat": bounded_heartbeat.__dict__,
        "rendered_heartbeat": rendered_heartbeat.__dict__,
        "heartbeat_provider_order": provider_calls,
        "heartbeat_base_exception": heartbeat_signal,
        "completion_clock_calls": completion_clock_calls,
        "completed_result": {
            "successful": completed_success,
            "error": completed_error,
            "snapshot": completed_snapshot,
        },
        "clock_failure_result": {
            "successful": clock_success,
            "error": clock_error,
            "prestate_retained": clock_failure_after == clock_failure_before,
            "before": clock_failure_before,
            "after": clock_failure_after,
        },
        "completion_base_exception": completion_signal,
        "collective_results": result_summaries,
        "collective_completions": collective_completions,
        "collective_thread_errors": thread_errors,
        "collective_threads_alive": threads_alive,
        "claim_boundary": (
            "Local heartbeat/completion and synthetic collective evidence "
            "only; no retry guarantee after partial heartbeat side effects, "
            "real MPI, multi-host filesystem, ULFM recovery, solver, coverage, "
            "campaign, bug, or LAVA-M uplift claim."
        ),
    }
    print(json.dumps(artifact, indent=2, sort_keys=True, allow_nan=False))
    return 0 if all(checks.values()) and len(checks) == 14 else 1


if __name__ == "__main__":
    raise SystemExit(main())
