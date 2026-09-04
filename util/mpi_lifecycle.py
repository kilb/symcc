"""Generation-fenced, bounded MPI process lifecycle primitives.

The helpers in this module intentionally know nothing about SymCC work items.
They provide the common READY -> STOP -> ACK shutdown protocol and bounded
collective exit used by both MPI execution frontends.
"""

import hashlib
import math
import os
import time
import typing

from mpi4py import MPI


TAG_RESULT = 2
TAG_STOP = 3
TAG_READY = 4
TAG_STOP_ACK = 5

_TOKEN_HEX_LENGTH = 64


def _normalize_lifecycle_token(value: typing.Any) -> str:
    if not isinstance(value, str) or len(value) != _TOKEN_HEX_LENGTH:
        return ""
    if any(char not in "0123456789abcdef" for char in value):
        return ""
    return value


def _make_shutdown_token(epoch: typing.Any, worker: int) -> str:
    """Derive one exact shutdown generation for a worker endpoint."""
    canonical_epoch = _normalize_lifecycle_token(epoch)
    worker = int(worker)
    if not canonical_epoch or worker < 1:
        raise ValueError("invalid shutdown token coordinates")
    material = (
        f"symcc-shutdown-v1\0{canonical_epoch}\0{worker}"
    ).encode("ascii")
    return hashlib.sha256(material).hexdigest()


def _shutdown_ack_status(
    expected_token: typing.Any,
    expected_worker: int,
    message: typing.Any,
) -> str:
    """Classify an acknowledgement without trusting its payload rank."""
    expected = _normalize_lifecycle_token(expected_token)
    if not expected:
        return "unowned"
    if not isinstance(message, dict):
        return "malformed"
    if message.get("schema") != "symcc-shutdown-ack-v1":
        return "malformed"
    reported_value = message.get("shutdown_token")
    if reported_value is None or (
            isinstance(reported_value, str) and not reported_value):
        return "missing"
    reported = _normalize_lifecycle_token(reported_value)
    if not reported:
        return "malformed"
    worker_value = message.get("rank")
    if (isinstance(worker_value, bool)
            or not isinstance(worker_value, int)
            or worker_value != int(expected_worker)):
        return "malformed"
    if reported != expected:
        return "stale"
    return "current"


class _ShutdownGenerationGate:
    """Join READY, tokenized STOP, and exact ACK for each worker rank."""

    def __init__(self, workers: typing.Iterable[int], epoch: str) -> None:
        normalized_workers = tuple(sorted({int(worker) for worker in workers}))
        if any(worker < 1 for worker in normalized_workers):
            raise ValueError("shutdown workers must be positive ranks")
        self.tokens = {
            worker: _make_shutdown_token(epoch, worker)
            for worker in normalized_workers
        }
        self.ready: set[int] = set()
        self.sent: set[int] = set()
        self.acknowledged: set[int] = set()

    def observe_ready(self, worker: int) -> str:
        worker = int(worker)
        if worker not in self.tokens:
            return "unowned"
        if worker in self.acknowledged:
            return "acknowledged"
        self.ready.add(worker)
        return "ready"

    def stop_message(self, worker: int) -> dict[str, typing.Any] | None:
        worker = int(worker)
        if (worker not in self.ready or worker in self.sent
                or worker in self.acknowledged):
            return None
        self.sent.add(worker)
        return {
            "schema": "symcc-shutdown-v1",
            "rank": worker,
            "shutdown_token": self.tokens[worker],
        }

    def observe_ack(self, worker: int, message: typing.Any) -> str:
        worker = int(worker)
        if worker not in self.sent:
            return "unowned"
        status = _shutdown_ack_status(
            self.tokens.get(worker, ""), worker, message)
        if status == "current":
            self.acknowledged.add(worker)
            self.ready.discard(worker)
        return status

    @property
    def pending(self) -> tuple[int, ...]:
        return tuple(sorted(set(self.tokens) - self.acknowledged))


def _bounded_mpi_timeout(value: typing.Any, default: float) -> float:
    """Parse a finite MPI lifecycle timeout, preserving explicit zero."""
    try:
        fallback_value = float(default)
    except (TypeError, ValueError, OverflowError):
        fallback_value = 30.0
    if not math.isfinite(fallback_value):
        fallback_value = 30.0
    fallback = min(3600.0, max(0.01, fallback_value))
    try:
        timeout = float(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    if not math.isfinite(timeout):
        return fallback
    return min(3600.0, max(0.0, timeout))


def _cooperative_shutdown_workers(
    comm: typing.Any,
    workers: typing.Iterable[int],
    *,
    initial_ready: typing.Iterable[int] = (),
    grace: float,
    result_callback: typing.Callable[[int, typing.Any], bool | None] | None = None,
    monotonic: typing.Callable[[], float] = time.monotonic,
    sleep: typing.Callable[[float], None] = time.sleep,
) -> dict[str, typing.Any]:
    """Perform a bounded READY -> STOP -> ACK shutdown handshake."""
    worker_tuple = tuple(sorted({int(worker) for worker in workers}))
    gate = _ShutdownGenerationGate(worker_tuple, os.urandom(32).hex())
    for worker in initial_ready:
        gate.observe_ready(int(worker))

    started = monotonic()
    timeout = _bounded_mpi_timeout(grace, grace)
    deadline = started + timeout
    requests: dict[int, typing.Any] = {}
    quarantined: dict[str, int] = {}
    communication_errors: set[int] = set()
    result_errors: set[int] = set()
    drained_results = 0
    first_poll = True

    while gate.pending and (first_poll or monotonic() < deadline):
        first_poll = False
        made_progress = False
        for worker in worker_tuple:
            try:
                # Drain bounded batches so a worker blocked on a large RESULT
                # can advance to READY without an abnormal flood monopolizing
                # shutdown.
                for tag in (TAG_RESULT, TAG_READY, TAG_STOP_ACK):
                    drained = 0
                    while drained < 16 and comm.iprobe(
                            source=worker, tag=tag):
                        message = comm.recv(source=worker, tag=tag)
                        drained += 1
                        made_progress = True
                        if tag == TAG_RESULT:
                            drained_results += 1
                            if result_callback is not None:
                                try:
                                    accepted = result_callback(worker, message)
                                except Exception:
                                    accepted = False
                                if accepted is False:
                                    result_errors.add(worker)
                        elif tag == TAG_READY:
                            gate.observe_ready(worker)
                        elif tag == TAG_STOP_ACK:
                            status = gate.observe_ack(worker, message)
                            if status != "current":
                                quarantined[status] = (
                                    quarantined.get(status, 0) + 1)
            except (MPI.Exception, OSError, RuntimeError):
                communication_errors.add(worker)

        for worker in worker_tuple:
            message = gate.stop_message(worker)
            if message is None:
                continue
            try:
                requests[worker] = comm.isend(
                    message, dest=worker, tag=TAG_STOP)
                made_progress = True
            except (AttributeError, MPI.Exception, OSError, RuntimeError):
                communication_errors.add(worker)

        # Local send completion is not proof of worker cleanup; only ACK is.
        for worker, request in list(requests.items()):
            try:
                completed = request.Test()
                if isinstance(completed, tuple):
                    completed = completed[0]
                if completed:
                    requests.pop(worker, None)
            except (MPI.Exception, OSError, RuntimeError):
                communication_errors.add(worker)
                requests.pop(worker, None)

        if not gate.pending:
            break
        remaining = deadline - monotonic()
        if remaining <= 0.0:
            break
        sleep(min(0.01 if made_progress else 0.05, remaining))

    elapsed = max(0.0, monotonic() - started)
    acknowledged = tuple(sorted(gate.acknowledged))
    pending = gate.pending
    return {
        "clean": not pending and not communication_errors and not result_errors,
        "acknowledged": acknowledged,
        "pending": pending,
        "sent": tuple(sorted(gate.sent)),
        "drained_results": drained_results,
        "result_errors": tuple(sorted(result_errors)),
        "quarantined_acks": dict(sorted(quarantined.items())),
        "communication_errors": tuple(sorted(communication_errors)),
        "elapsed": elapsed,
    }


def _bounded_mpi_barrier(
    comm: typing.Any,
    timeout: float,
    *,
    monotonic: typing.Callable[[], float] = time.monotonic,
    sleep: typing.Callable[[float], None] = time.sleep,
) -> bool:
    """Complete a nonblocking barrier or return after the finite deadline."""
    timeout = _bounded_mpi_timeout(timeout, timeout)
    try:
        request = comm.Ibarrier()
    except (AttributeError, MPI.Exception, OSError, RuntimeError):
        return False
    deadline = monotonic() + timeout
    first_poll = True
    while first_poll or monotonic() < deadline:
        first_poll = False
        try:
            completed = request.Test()
            if isinstance(completed, tuple):
                completed = completed[0]
            if completed:
                return True
        except (MPI.Exception, OSError, RuntimeError):
            return False
        remaining = deadline - monotonic()
        if remaining <= 0.0:
            break
        sleep(min(0.01, remaining))
    return False


def _shutdown_stop_token(
    message: typing.Any,
    expected_worker: int | None = None,
) -> str:
    """Validate the tokenized STOP payload received by a worker."""
    if not isinstance(message, dict):
        return ""
    if message.get("schema") != "symcc-shutdown-v1":
        return ""
    if expected_worker is not None:
        reported_worker = message.get("rank")
        if (isinstance(reported_worker, bool)
                or not isinstance(reported_worker, int)
                or reported_worker != int(expected_worker)):
            return ""
    return _normalize_lifecycle_token(message.get("shutdown_token"))


def _send_shutdown_ack(
    comm: typing.Any,
    worker: int,
    shutdown_token: typing.Any,
) -> None:
    """Acknowledge only the exact STOP generation after local cleanup."""
    token = _normalize_lifecycle_token(shutdown_token)
    if not token:
        raise RuntimeError("worker received an invalid shutdown token")
    comm.send(
        {
            "schema": "symcc-shutdown-ack-v1",
            "rank": int(worker),
            "shutdown_token": token,
        },
        dest=0,
        tag=TAG_STOP_ACK,
    )
