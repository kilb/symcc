#!/usr/bin/env python3
"""Reproduce F358 renewal control-transport containment invariants."""

from __future__ import annotations

from collections import defaultdict, deque
import hashlib
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "util"))

import mpi_filesystem_qualification as qualification  # noqa: E402


class InstrumentedRequest:
    def __init__(self, name, outcome):
        self.name = name
        self.outcome = outcome
        self.calls = 0

    def Test(self):
        self.calls += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class MessageBus:
    def __init__(self, size):
        self.size = size
        self.queues = defaultdict(deque)
        self.requests = []

    def send(self, source, destination, tag, message):
        self.queues[(destination, source, tag)].append(message)
        request = InstrumentedRequest(
            f"rank-{source}-to-{destination}", True)
        self.requests.append(request)
        return request

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
        return self.bus.send(self.rank, dest, tag, message)

    def iprobe(self, *, source, tag):
        return bool(self.bus.queues[(self.rank, source, tag)])

    def recv(self, *, source, tag):
        return self.bus.queues[(self.rank, source, tag)].popleft()


class PartialSendCommunicator(Communicator):
    def isend(self, message, *, dest, tag):
        if dest == 2:
            raise LookupError("injected partial send adapter failure")
        return super().isend(message, dest=dest, tag=tag)


class BrokenTextError(Exception):
    def __str__(self):
        raise RuntimeError("broken transport exception rendering")


def controller(epoch):
    return qualification.ClusterLockRenewalController(
        epoch=epoch,
        interval=5.0,
        timeout=2.0,
        completed_at=0.0,
        require_configuration_consensus=False,
    )


def observation_dict(observation):
    return {
        "total_count": observation.total_count,
        "completed_count": observation.completed_count,
        "incomplete_count": observation.incomplete_count,
        "uncertain_count": observation.uncertain_count,
        "error": observation.error,
    }


def main():
    epoch = hashlib.sha256(
        b"F358-renewal-control-transport-containment"
    ).hexdigest()

    class RankFailure:
        def Get_rank(self):
            raise LookupError("injected communicator rank failure")

    pre_begin_controller = controller(epoch)
    pre_begin_before = pre_begin_controller.snapshot()
    pre_begin = qualification.begin_cluster_lock_renewal(
        RankFailure(), pre_begin_controller)
    pre_begin_after = pre_begin_controller.snapshot()

    bus = MessageBus(4)
    root = controller(epoch)
    partial_generation, partial_sends, partial_error = (
        qualification.begin_cluster_lock_renewal(
            PartialSendCommunicator(bus, 0), root)
    )
    partial_delivery = qualification.observe_cluster_lock_renewal_delivery(
        partial_sends)
    peer_results = []
    for rank in (1, 2, 3):
        accepted, error, observed = qualification.poll_cluster_lock_renewal(
            bus.communicator(rank), controller(epoch))
        peer_results.append({
            "rank": rank,
            "generation": accepted,
            "error": error,
            "observed": observed,
        })

    class PollFailure:
        def Get_rank(self):
            return 1

        def Get_size(self):
            return 2

        def iprobe(self, *, source, tag):
            raise LookupError("injected peer poll failure")

    poll_failure = qualification.poll_cluster_lock_renewal(
        PollFailure(), controller(epoch))

    class ReceivedRequest(PollFailure):
        def iprobe(self, *, source, tag):
            return True

        def recv(self, *, source, tag):
            return {}

    admission_controller = controller(epoch)

    def failed_admission(_request):
        raise LookupError("injected request admission failure")

    admission_controller.accept_request = failed_admission
    admission_failure = qualification.poll_cluster_lock_renewal(
        ReceivedRequest(), admission_controller)

    completed = InstrumentedRequest("completed", True)
    incomplete = InstrumentedRequest("incomplete", False)
    uncertain = InstrumentedRequest(
        "uncertain", LookupError("injected completion observation failure"))
    delivery = qualification.observe_cluster_lock_renewal_delivery((
        completed,
        incomplete,
        uncertain,
    ))
    delivery_calls = {
        request.name: request.calls
        for request in (completed, incomplete, uncertain)
    }

    duplicate = InstrumentedRequest("duplicate", True)
    duplicate_delivery = qualification.observe_cluster_lock_renewal_delivery(
        (duplicate, duplicate))
    bounded_delivery = qualification.observe_cluster_lock_renewal_delivery((
        InstrumentedRequest("bounded", LookupError("x\n\x00" * 300)),
    ))
    rendered_delivery = qualification.observe_cluster_lock_renewal_delivery((
        InstrumentedRequest("rendered", BrokenTextError()),
    ))

    signals = []

    class InterruptedSend(Communicator):
        def isend(self, message, *, dest, tag):
            raise KeyboardInterrupt("begin transport signal")

    try:
        qualification.begin_cluster_lock_renewal(
            InterruptedSend(MessageBus(2), 0), controller(epoch))
    except BaseException as error:
        signals.append(f"{type(error).__name__}: {error}")

    class InterruptedPoll(PollFailure):
        def iprobe(self, *, source, tag):
            raise KeyboardInterrupt("poll transport signal")

    try:
        qualification.poll_cluster_lock_renewal(
            InterruptedPoll(), controller(epoch))
    except BaseException as error:
        signals.append(f"{type(error).__name__}: {error}")

    try:
        qualification.observe_cluster_lock_renewal_delivery((
            InstrumentedRequest(
                "interrupted", KeyboardInterrupt("delivery signal")),
        ))
    except BaseException as error:
        signals.append(f"{type(error).__name__}: {error}")

    checks = {
        "pre_begin_exception_is_contained": bool(
            pre_begin[0] == 0
            and pre_begin[1] == ()
            and "injected communicator rank failure" in pre_begin[2]
        ),
        "pre_begin_failure_keeps_controller_state": (
            pre_begin_after == pre_begin_before
        ),
        "partial_send_exception_is_contained": (
            "injected partial send adapter failure" in partial_error
        ),
        "post_begin_failure_retains_generation": bool(
            partial_generation == 1
            and root.in_flight_generation == 1
        ),
        "successful_send_handles_are_retained": bool(
            len(partial_sends) == 2
            and partial_delivery.total_count == 2
            and partial_delivery.completed_count == 2
        ),
        "receiving_peers_accept_exact_generation": bool(
            [result["rank"] for result in peer_results if result["observed"]]
            == [1, 3]
            and all(
                result["generation"] == 1 and result["error"] == ""
                for result in peer_results if result["observed"]
            )
        ),
        "unsent_peer_observes_no_request": peer_results[1] == {
            "rank": 2,
            "generation": 0,
            "error": "",
            "observed": False,
        },
        "poll_exception_is_contained": bool(
            poll_failure[0] == 0
            and poll_failure[2] is False
            and "injected peer poll failure" in poll_failure[1]
        ),
        "admission_exception_is_fatal_classified": bool(
            admission_failure[0] == 0
            and admission_failure[2] is False
            and "injected request admission failure" in admission_failure[1]
        ),
        "delivery_states_are_distinct": bool(
            delivery.total_count == 3
            and delivery.completed_count == 1
            and delivery.incomplete_count == 1
            and delivery.uncertain_count == 1
        ),
        "delivery_handles_are_observed_once": delivery_calls == {
            "completed": 1,
            "incomplete": 1,
            "uncertain": 1,
        },
        "duplicate_handle_is_rejected_before_observation": bool(
            duplicate_delivery.total_count == 2
            and duplicate_delivery.uncertain_count == 2
            and duplicate.calls == 0
        ),
        "delivery_diagnostic_is_bounded": bool(
            len(bounded_delivery.error) == 512
            and "\n" not in bounded_delivery.error
            and "\x00" not in bounded_delivery.error
        ),
        "render_failure_uses_exception_type": (
            rendered_delivery.error
            == "cluster lock renewal delivery request 0 failed: "
            "BrokenTextError"
        ),
        "base_exceptions_propagate": signals == [
            "KeyboardInterrupt: begin transport signal",
            "KeyboardInterrupt: poll transport signal",
            "KeyboardInterrupt: delivery signal",
        ],
    }
    artifact = {
        "schema": "symcc-f358-renewal-control-transport-evidence-v1",
        "feature": "F358",
        "actual_mpi_transport": False,
        "synthetic_point_to_point_transport": True,
        "synthetic_collective_transport": False,
        "actual_multihost_filesystem": False,
        "solver_or_campaign_executed": False,
        "epoch": epoch,
        "checks_passed": sum(checks.values()),
        "checks_total": len(checks),
        "checks": checks,
        "pre_begin": {
            "generation": pre_begin[0],
            "send_count": len(pre_begin[1]),
            "error": pre_begin[2],
            "prestate_retained": pre_begin_after == pre_begin_before,
        },
        "partial_send": {
            "generation": partial_generation,
            "send_count": len(partial_sends),
            "error": partial_error,
            "controller_in_flight_generation": root.in_flight_generation,
            "delivery": observation_dict(partial_delivery),
            "peer_results": peer_results,
        },
        "poll_failure": {
            "generation": poll_failure[0],
            "error": poll_failure[1],
            "observed": poll_failure[2],
        },
        "admission_failure": {
            "generation": admission_failure[0],
            "error": admission_failure[1],
            "observed": admission_failure[2],
        },
        "delivery": observation_dict(delivery),
        "delivery_calls": delivery_calls,
        "duplicate_delivery": observation_dict(duplicate_delivery),
        "bounded_delivery": observation_dict(bounded_delivery),
        "rendered_delivery": observation_dict(rendered_delivery),
        "base_exception_results": signals,
        "claim_boundary": (
            "Local transport containment and deterministic synthetic "
            "point-to-point evidence only; no send rollback, retry, MPI "
            "atomicity, collective convergence, real MPI, multi-host, ULFM, "
            "solver, coverage, campaign, bug, or LAVA-M uplift claim."
        ),
    }
    print(json.dumps(artifact, indent=2, sort_keys=True, allow_nan=False))
    return 0 if all(checks.values()) and len(checks) == 15 else 1


if __name__ == "__main__":
    raise SystemExit(main())
