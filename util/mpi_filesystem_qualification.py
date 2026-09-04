"""Bounded, fail-closed cross-host qualification for shared advisory locks.

The local filesystem probe can only observe another process on the same host.
This module uses an MPI communicator containing masters only as an independent
control plane while those masters contend on one stable lock inode.  No
filesystem marker is used to order the experiment whose filesystem semantics
are under test.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import errno
import fcntl
import hashlib
import math
import os
import stat
import struct
import time
import typing

from mpi4py import MPI

try:
    from .distributed_state import (
        FULL_SHARED_FILESYSTEM_REQUIREMENTS,
        SharedFilesystemCapabilities,
        qualify_shared_filesystem_cluster_lock,
    )
except ImportError:
    from distributed_state import (
        FULL_SHARED_FILESYSTEM_REQUIREMENTS,
        SharedFilesystemCapabilities,
        qualify_shared_filesystem_cluster_lock,
    )


_TAG_CLUSTER_GATHER = 31
_TAG_CLUSTER_GATHER_RESULT = 32
CLUSTER_LOCK_RENEWAL_TAG = 33
_TOKEN_LENGTH = 64
_LOCK_FILENAME = ".cluster-filesystem.lock"
_MAX_QUALIFICATION_GENERATION = (1 << 63) - 1


@dataclass(frozen=True)
class ClusterLockQualificationResult:
    """One master's view of a consensus-qualified lock domain."""

    clean: bool
    verified: bool
    capability: SharedFilesystemCapabilities | None
    members: tuple[tuple[int, str], ...]
    representatives: tuple[int, ...]
    rounds: int
    contention_checks: int
    release_checks: int
    elapsed: float
    error: str = ""
    identity_checks: int = 0
    qualification_generation: int = 0
    proof_transcript: str = ""


@dataclass(frozen=True)
class ClusterLockQualificationObservation:
    """Rank-local inputs collected before the bounded MPI protocol."""

    capability: SharedFilesystemCapabilities | None
    processor_name: str
    error: str = ""


@dataclass(frozen=True)
class WorkLeaseHeartbeatObservation:
    """Bounded outcome of one work-lease heartbeat attempt."""

    lost_lease_count: int
    error: str = ""


@dataclass(frozen=True)
class ClusterLockRenewalDeliveryObservation:
    """One-shot classification of nonblocking renewal request delivery."""

    total_count: int
    completed_count: int
    incomplete_count: int
    uncertain_count: int
    error: str = ""


def _bounded_text(value: typing.Any, limit: int = 512) -> str:
    text = str(value).replace("\x00", "?").replace("\n", " ")
    return text[:limit]


def _bounded_exception_detail(error: Exception, limit: int = 512) -> str:
    """Render an ordinary exception without trusting its text or type name."""
    try:
        type_name = type(error).__name__
    except Exception:
        type_name = "Exception"
    if type(type_name) is not str:
        type_name = "Exception"
    type_name = type_name.replace("\x00", "?").replace("\n", " ")
    type_name = type_name[:limit] or "Exception"
    try:
        detail = _bounded_text(error, limit)
    except Exception:
        detail = type_name
    if type(detail) is not str or not detail:
        return type_name
    detail = detail.replace("\x00", "?").replace("\n", " ")
    return detail[:limit] or type_name


def _bounded_exception_message(
    prefix: str,
    error: Exception,
    limit: int = 512,
) -> str:
    """Prefix an exception diagnostic while preserving one hard size bound."""
    bounded_prefix = _bounded_text(prefix, limit)
    if len(bounded_prefix) >= limit:
        return bounded_prefix
    return bounded_prefix + _bounded_exception_detail(
        error,
        limit - len(bounded_prefix),
    )


def observe_cluster_lock_qualification_inputs(
    capability: SharedFilesystemCapabilities | None = None,
    *,
    capability_probe: typing.Callable[
        [], SharedFilesystemCapabilities
    ] | None = None,
    processor_name_probe: typing.Callable[[], str] | None = None,
    local_error: str = "",
) -> ClusterLockQualificationObservation:
    """Totalize ordinary failures while collecting rank-local proof inputs."""
    errors: list[str] = []
    if type(local_error) is str:
        if local_error:
            errors.append(_bounded_text(local_error))
    else:
        errors.append("invalid local qualification observation error")

    observed_capability = capability
    if capability_probe is not None:
        try:
            observed_capability = capability_probe()
        except Exception as error:
            observed_capability = None
            errors.append(
                "filesystem capability probe raised: "
                f"{_bounded_exception_detail(error)}"
            )
    if observed_capability is not None and not isinstance(
        observed_capability,
        SharedFilesystemCapabilities,
    ):
        observed_capability = None
        errors.append(
            "filesystem capability probe returned an invalid result"
            if capability_probe is not None
            else "filesystem capability input is invalid"
        )
    elif observed_capability is None and not errors:
        errors.append("local filesystem capability is unavailable")

    if processor_name_probe is None:
        processor_name_probe = MPI.Get_processor_name
    processor_probe_failed = False
    try:
        processor_name = processor_name_probe()
    except Exception as error:
        processor_probe_failed = True
        processor_name = ""
        errors.append(
            "processor identity probe raised: "
            f"{_bounded_exception_detail(error)}"
        )
    if (
        not isinstance(processor_name, str)
        or not processor_name
        or len(processor_name) > 255
        or "\x00" in processor_name
    ):
        processor_name = ""
        if not processor_probe_failed:
            errors.append("processor identity probe returned an invalid result")

    return ClusterLockQualificationObservation(
        capability=observed_capability,
        processor_name=processor_name,
        error=_bounded_text("; ".join(errors)),
    )


def observe_work_lease_heartbeat(
    heartbeat: typing.Callable[[], tuple[str, ...]],
) -> WorkLeaseHeartbeatObservation:
    """Call one heartbeat once and totalize ordinary provider failures."""
    try:
        lost_leases = heartbeat()
    except Exception as error:
        prefix = "work lease heartbeat failed: "
        detail = _bounded_exception_detail(error, 512 - len(prefix))
        return WorkLeaseHeartbeatObservation(0, prefix + detail)
    if (
        type(lost_leases) is not tuple
        or any(
            type(work_hash) is not str or not _valid_token(work_hash)
            for work_hash in lost_leases
        )
        or len(set(lost_leases)) != len(lost_leases)
    ):
        return WorkLeaseHeartbeatObservation(
            0,
            "work lease heartbeat returned an invalid result",
        )
    return WorkLeaseHeartbeatObservation(len(lost_leases))


def _valid_token(value: typing.Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _TOKEN_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _qualification_exchange_token(
    lock_token: str,
    generation: int,
) -> str:
    if not _valid_token(lock_token):
        raise ValueError("invalid cluster lock token")
    if (
        type(generation) is not int
        or generation < 0
        or generation > _MAX_QUALIFICATION_GENERATION
    ):
        raise ValueError("invalid cluster lock qualification generation")
    return hashlib.sha256(
        b"symcc-cluster-lock-exchange-v2\0"
        + bytes.fromhex(lock_token)
        + generation.to_bytes(8, "big")
    ).hexdigest()


def _qualification_proof_transcript(
    epoch: str,
    generation: int,
    members: typing.Iterable[tuple[int, str]],
    representatives: typing.Iterable[int],
    rounds: int,
    contention_checks: int,
    release_checks: int,
    identity_checks: int,
) -> str:
    """Bind one successful qualification to its generation and evidence."""
    if not _valid_token(epoch):
        raise ValueError("invalid cluster lock proof epoch")

    def add_integer(digest: typing.Any, value: typing.Any, label: str) -> None:
        if (
            type(value) is not int
            or value < 0
            or value > _MAX_QUALIFICATION_GENERATION
        ):
            raise ValueError(f"invalid cluster lock proof {label}")
        digest.update(struct.pack(">Q", value))

    normalized_members = tuple(members)
    normalized_representatives = tuple(representatives)
    if not normalized_members:
        raise ValueError("empty cluster lock proof membership")
    digest = hashlib.sha256()
    digest.update(b"symcc-cluster-lock-proof-transcript-v1\0")
    digest.update(bytes.fromhex(epoch))
    add_integer(digest, generation, "generation")
    for value, label in (
        (len(normalized_members), "member count"),
        (len(normalized_representatives), "representative count"),
        (rounds, "round count"),
        (contention_checks, "contention count"),
        (release_checks, "release count"),
        (identity_checks, "identity count"),
    ):
        add_integer(digest, value, label)
    for rank, processor in normalized_members:
        add_integer(digest, rank, "member rank")
        if not isinstance(processor, str):
            raise ValueError("invalid cluster lock proof processor")
        encoded = processor.encode("utf-8")
        digest.update(struct.pack(">I", len(encoded)))
        digest.update(encoded)
    for representative in normalized_representatives:
        add_integer(digest, representative, "representative rank")
    return digest.hexdigest()


def _qualification_result_matches_request(
    epoch: str,
    generation: int,
    result: ClusterLockQualificationResult,
    expected_master_ranks: tuple[int, ...] = (),
    expected_capability: SharedFilesystemCapabilities | None = None,
) -> bool:
    """Validate a result as one coherent proof for the current request."""
    if (
        not isinstance(result, ClusterLockQualificationResult)
        or result.clean is not True
        or result.verified is not True
        or not isinstance(result.error, str)
        or result.error != ""
        or type(result.qualification_generation) is not int
        or result.qualification_generation != generation
        or not _valid_token(result.proof_transcript)
        or result.capability is None
        or not isinstance(result.capability, SharedFilesystemCapabilities)
        or type(result.members) is not tuple
        or type(result.representatives) is not tuple
    ):
        return False
    try:
        capability = result.capability
        members = result.members
        representatives = result.representatives
        if (
            expected_master_ranks
            and tuple(rank for rank, _ in members) != expected_master_ranks
        ):
            return False
        if expected_capability is not None and capability != expected_capability:
            return False
        validated = qualify_shared_filesystem_cluster_lock(
            capability,
            members=members,
            representatives=representatives,
            rounds=result.rounds,
            contention_checks=result.contention_checks,
            release_checks=result.release_checks,
            identity_checks=result.identity_checks,
        )
        transcript = _qualification_proof_transcript(
            epoch,
            generation,
            members,
            representatives,
            result.rounds,
            result.contention_checks,
            result.release_checks,
            result.identity_checks,
        )
    except Exception:
        # Result admission is a total boundary: malformed dataclass field values
        # are a failed proof, not an exception that may split controller state.
        return False
    return bool(
        result.proof_transcript == transcript
        and capability.cluster_lock_verified
        and capability.probe_scope == "cross-host-mpi-lock-v2"
        and capability.cluster_lock_members == validated.cluster_lock_members
        and capability.cluster_lock_representatives
        == validated.cluster_lock_representatives
        and capability.cluster_lock_rounds == validated.cluster_lock_rounds
        and capability.cluster_lock_contention_checks
        == validated.cluster_lock_contention_checks
        and capability.cluster_lock_release_checks
        == validated.cluster_lock_release_checks
        and capability.cluster_lock_identity_checks
        == validated.cluster_lock_identity_checks
    )


def _renewal_configuration_fingerprint(
    epoch: str,
    interval: float,
    timeout: float,
    jitter_fraction: float,
) -> str:
    """Commit one epoch's normalized binary64 renewal configuration."""
    if not _valid_token(epoch):
        raise ValueError("invalid cluster lock renewal epoch")
    base = float(interval)
    timeout_value = float(timeout)
    jitter = float(jitter_fraction)
    if (
        not math.isfinite(base)
        or base < 0.0
        or not math.isfinite(timeout_value)
        or timeout_value <= 0.0
        or not math.isfinite(jitter)
        or jitter < 0.0
        or jitter > 0.5
        or not math.isfinite(base * (1.0 + jitter))
    ):
        raise ValueError("invalid cluster lock renewal configuration")
    if jitter == 0.0:
        jitter = 0.0
    return hashlib.sha256(
        b"symcc-cluster-lock-renewal-config-v1\0"
        + bytes.fromhex(epoch)
        + struct.pack(">ddd", base, timeout_value, jitter)
    ).hexdigest()


def _renewal_request_token(
    epoch: str,
    generation: int,
    configuration_fingerprint: str,
) -> str:
    if not _valid_token(epoch):
        raise ValueError("invalid cluster lock renewal epoch")
    if not _valid_token(configuration_fingerprint):
        raise ValueError("invalid cluster lock renewal configuration fingerprint")
    if (
        type(generation) is not int
        or generation < 1
        or generation > _MAX_QUALIFICATION_GENERATION
    ):
        raise ValueError("invalid cluster lock renewal generation")
    return hashlib.sha256(
        b"symcc-cluster-lock-renewal-v2\0"
        + bytes.fromhex(epoch)
        + generation.to_bytes(8, "big")
        + bytes.fromhex(configuration_fingerprint)
    ).hexdigest()


def _scheduled_renewal_interval(
    epoch: str,
    generation: int,
    interval: float,
    jitter_fraction: float,
) -> float:
    """Return a deterministic delay-only jitter for one renewal generation."""
    if not _valid_token(epoch):
        raise ValueError("invalid cluster lock renewal epoch")
    if (
        type(generation) is not int
        or generation < 1
        or generation > _MAX_QUALIFICATION_GENERATION
    ):
        raise ValueError("invalid cluster lock renewal generation")
    base = float(interval)
    jitter = float(jitter_fraction)
    if (
        not math.isfinite(base)
        or base < 0.0
        or not math.isfinite(jitter)
        or jitter < 0.0
        or jitter > 0.5
        or not math.isfinite(base * (1.0 + jitter))
    ):
        raise ValueError("invalid cluster lock renewal jitter schedule")
    maximum = base * (1.0 + jitter)
    if base == 0.0 or jitter == 0.0 or maximum == base:
        return base
    digest = hashlib.sha256(
        b"symcc-cluster-lock-renewal-jitter-v1\0"
        + bytes.fromhex(epoch)
        + generation.to_bytes(8, "big")
    ).digest()
    # A direct uint64 / 2**64 conversion can round the largest integers to
    # exactly 1.0 in binary64.  Keeping the high 53 bits makes every quotient
    # exactly representable in [0, 1); the final arithmetic is clamped below.
    mantissa = int.from_bytes(digest[:8], "big") >> 11
    unit = mantissa / float(1 << 53)
    scheduled = base * (1.0 + jitter * unit)
    # The final multiply/add may still round the largest unit values to the
    # open upper endpoint.  Clamp only that boundary case to the immediately
    # preceding binary64 value; all other hash-derived values remain unchanged.
    if scheduled >= maximum:
        return math.nextafter(maximum, base)
    return scheduled


@dataclass
class ClusterLockRenewalController:
    """Generation-fenced schedule and metrics for runtime qualification."""

    epoch: str
    interval: float
    timeout: float
    completed_at: float
    jitter_fraction: float = 0.0
    require_configuration_consensus: bool = field(default=True, kw_only=True)
    expected_master_ranks: tuple[int, ...] = field(default=(), kw_only=True)
    expected_capability: SharedFilesystemCapabilities | None = field(
        default=None, kw_only=True, repr=False)
    generation: int = 0
    in_flight_generation: int = 0
    attempts: int = 0
    successes: int = 0
    failures: int = 0
    total_elapsed: float = 0.0
    last_elapsed: float = 0.0
    _configuration_consensus_required: bool = field(init=False, repr=False)
    _configuration_consensus_fingerprint: str = field(init=False, repr=False)
    _configuration_fingerprint: str = field(init=False, repr=False)
    _last_scheduled_interval: float = field(init=False, repr=False)
    _next_scheduled_interval: float = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not _valid_token(self.epoch):
            raise ValueError("invalid cluster lock renewal epoch")
        for name in ("interval", "timeout", "completed_at"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(f"invalid cluster lock renewal {name}")
            setattr(self, name, value)
        if self.timeout <= 0.0:
            raise ValueError("cluster lock renewal timeout must be positive")
        if type(self.require_configuration_consensus) is not bool:
            raise ValueError(
                "invalid cluster lock renewal consensus requirement")
        self.expected_master_ranks = tuple(self.expected_master_ranks)
        if (
            self.expected_master_ranks
            and (
                len(self.expected_master_ranks) < 2
                or any(
                    type(rank) is not int
                    or rank < 0
                    or rank > _MAX_QUALIFICATION_GENERATION
                    for rank in self.expected_master_ranks
                )
                or tuple(sorted(set(self.expected_master_ranks)))
                != self.expected_master_ranks
            )
        ):
            raise ValueError("invalid cluster lock renewal master topology")
        if (
            self.expected_capability is not None
            and not isinstance(
                self.expected_capability, SharedFilesystemCapabilities)
        ):
            raise ValueError("invalid cluster lock renewal expected capability")
        self._configuration_consensus_required = (
            self.require_configuration_consensus)
        try:
            self.jitter_fraction = float(self.jitter_fraction)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError(
                "invalid cluster lock renewal jitter fraction"
            ) from error
        if (
            not math.isfinite(self.jitter_fraction)
            or self.jitter_fraction < 0.0
            or self.jitter_fraction > 0.5
            or not math.isfinite(
                self.interval * (1.0 + self.jitter_fraction)
            )
        ):
            raise ValueError("invalid cluster lock renewal jitter fraction")
        if self.jitter_fraction == 0.0:
            self.jitter_fraction = 0.0
        if (
            type(self.generation) is not int
            or self.generation < 0
            or self.generation > _MAX_QUALIFICATION_GENERATION
        ):
            raise ValueError("invalid initial cluster lock generation")
        if self.in_flight_generation != 0:
            raise ValueError("cluster lock renewal cannot start in flight")
        self._configuration_fingerprint = (
            _renewal_configuration_fingerprint(
                self.epoch,
                self.interval,
                self.timeout,
                self.jitter_fraction,
            )
        )
        self._configuration_consensus_fingerprint = ""
        self._last_scheduled_interval = (
            _scheduled_renewal_interval(
                self.epoch,
                self.generation,
                self.interval,
                self.jitter_fraction,
            )
            if self.generation
            else 0.0
        )
        self._next_scheduled_interval = (
            _scheduled_renewal_interval(
                self.epoch,
                self.generation + 1,
                self.interval,
                self.jitter_fraction,
            )
            if self.generation < _MAX_QUALIFICATION_GENERATION
            else 0.0
        )

    @property
    def enabled(self) -> bool:
        return self.interval > 0.0

    @property
    def scheduled_interval(self) -> float:
        """Delay from the last clean completion to the next generation."""
        if self.generation >= _MAX_QUALIFICATION_GENERATION:
            raise RuntimeError("cluster lock renewal generation is exhausted")
        return self._next_scheduled_interval

    @property
    def configuration_fingerprint(self) -> str:
        return self._configuration_fingerprint

    @property
    def configuration_consensus_established(self) -> bool:
        return (
            self._configuration_consensus_fingerprint
            == self.configuration_fingerprint
            and self._configuration_is_current()
        )

    def _record_configuration_consensus(self, fingerprint: str) -> None:
        if not self._configuration_is_current():
            raise RuntimeError("cluster lock renewal configuration changed")
        if fingerprint != self.configuration_fingerprint:
            raise RuntimeError(
                "cluster lock renewal configuration consensus mismatch")
        self._configuration_consensus_fingerprint = fingerprint

    def _configuration_consensus_is_ready(self) -> bool:
        return (
            not self._configuration_consensus_required
            or self._configuration_consensus_fingerprint
            == self.configuration_fingerprint
        )

    def configuration_snapshot(self) -> dict[str, typing.Any]:
        return {
            "schema": "symcc-cluster-lock-renewal-config-v1",
            "epoch": self.epoch,
            "interval": self.interval,
            "timeout": self.timeout,
            "jitter_fraction": self.jitter_fraction,
            "maximum_interval": self.interval * (1.0 + self.jitter_fraction),
            "fingerprint": self.configuration_fingerprint,
        }

    def _configuration_is_current(self) -> bool:
        try:
            observed = _renewal_configuration_fingerprint(
                self.epoch,
                self.interval,
                self.timeout,
                self.jitter_fraction,
            )
        except (TypeError, ValueError, OverflowError):
            return False
        return observed == self.configuration_fingerprint

    def due(
        self,
        now: float,
        *,
        safe_to_start: bool = True,
        wall_remaining: float | None = None,
    ) -> bool:
        try:
            current = float(now)
        except (TypeError, ValueError, OverflowError):
            return False
        if not math.isfinite(current):
            return False
        if (
            not self.enabled
            or self.in_flight_generation
            or not safe_to_start
            or not self._configuration_consensus_is_ready()
            or self.generation >= _MAX_QUALIFICATION_GENERATION
            or current - self.completed_at < self.scheduled_interval
        ):
            return False
        if wall_remaining is not None:
            try:
                remaining = float(wall_remaining)
            except (TypeError, ValueError, OverflowError):
                return False
            if (
                not math.isfinite(remaining)
                or remaining <= self.timeout + 1.0
            ):
                return False
        return True

    def begin_request(self) -> dict[str, typing.Any]:
        if self.in_flight_generation:
            raise RuntimeError("cluster lock renewal is already in flight")
        if not self._configuration_is_current():
            raise RuntimeError("cluster lock renewal configuration changed")
        if not self._configuration_consensus_is_ready():
            raise RuntimeError(
                "cluster lock renewal configuration consensus is not established")
        generation = self.generation + 1
        token = _renewal_request_token(
            self.epoch,
            generation,
            self.configuration_fingerprint,
        )
        self.in_flight_generation = generation
        return {
            "schema": "symcc-cluster-lock-renewal-v2",
            "epoch": self.epoch,
            "generation": generation,
            "configuration": self.configuration_fingerprint,
            "token": token,
        }

    def accept_request(
        self,
        value: typing.Any,
    ) -> tuple[int, str]:
        if self.in_flight_generation:
            return 0, "cluster lock renewal is already in flight"
        if not self._configuration_is_current():
            return 0, "cluster lock renewal configuration changed"
        if not self._configuration_consensus_is_ready():
            return 0, (
                "cluster lock renewal configuration consensus is not established"
            )
        if (
            not isinstance(value, dict)
            or set(value) != {
                "schema", "epoch", "generation", "configuration", "token"
            }
            or value.get("schema") != "symcc-cluster-lock-renewal-v2"
            or value.get("epoch") != self.epoch
            or type(value.get("generation")) is not int
            or not _valid_token(value.get("configuration"))
            or not _valid_token(value.get("token"))
        ):
            return 0, "malformed cluster lock renewal request"
        generation = value["generation"]
        if generation != self.generation + 1:
            return 0, "stale or out-of-order cluster lock renewal request"
        if value["configuration"] != self.configuration_fingerprint:
            return 0, "cluster lock renewal configuration mismatch"
        try:
            expected = _renewal_request_token(
                self.epoch,
                generation,
                self.configuration_fingerprint,
            )
        except ValueError as error:
            return 0, str(error)
        if value["token"] != expected:
            return 0, "cluster lock renewal token mismatch"
        self.in_flight_generation = generation
        return generation, ""

    def complete(
        self,
        generation: int,
        result: ClusterLockQualificationResult,
        *,
        completed_at: float,
    ) -> bool:
        if (
            type(generation) is not int
            or generation != self.in_flight_generation
        ):
            raise RuntimeError("cluster lock renewal generation mismatch")
        if not isinstance(result, ClusterLockQualificationResult):
            raise TypeError("invalid cluster lock renewal result")
        if not self._configuration_is_current():
            raise RuntimeError(
                "cluster lock renewal configuration changed in flight")
        if not self._configuration_consensus_is_ready():
            raise RuntimeError(
                "cluster lock renewal configuration consensus changed in flight")
        try:
            completed = float(completed_at)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError(
                "invalid cluster lock renewal completion time"
            ) from error
        if not math.isfinite(completed) or completed < self.completed_at:
            raise ValueError("invalid cluster lock renewal completion time")
        elapsed_valid = type(result.elapsed) in {int, float}
        try:
            elapsed = float(result.elapsed) if elapsed_valid else 0.0
        except (TypeError, ValueError, OverflowError):
            elapsed = 0.0
            elapsed_valid = False
        if not math.isfinite(elapsed) or elapsed < 0.0:
            elapsed = 0.0
            elapsed_valid = False

        # Finish every operation that can reject or raise before changing any
        # state. This preserves the in-flight typestate on internal failures.
        successful = elapsed_valid and _qualification_result_matches_request(
            self.epoch,
            generation,
            result,
            self.expected_master_ranks,
            self.expected_capability,
        )
        next_scheduled_interval = (
            _scheduled_renewal_interval(
                self.epoch,
                generation + 1,
                self.interval,
                self.jitter_fraction,
            )
            if generation < _MAX_QUALIFICATION_GENERATION
            else 0.0
        )
        total_elapsed = self.total_elapsed + elapsed
        if not math.isfinite(total_elapsed):
            raise OverflowError("cluster lock renewal elapsed total overflow")

        self.attempts += 1
        self.generation = generation
        self.in_flight_generation = 0
        self._last_scheduled_interval = self._next_scheduled_interval
        self._next_scheduled_interval = next_scheduled_interval
        self.last_elapsed = elapsed
        self.total_elapsed = total_elapsed
        if successful:
            self.successes += 1
            self.completed_at = completed
        else:
            self.failures += 1
        return successful

    def snapshot(self) -> dict[str, typing.Any]:
        return {
            "schema": "symcc-cluster-lock-renewal-metrics-v2",
            "interval": self.interval,
            "jitter_fraction": self.jitter_fraction,
            "maximum_interval": self.interval * (1.0 + self.jitter_fraction),
            "last_scheduled_interval": self._last_scheduled_interval,
            "next_scheduled_interval": (
                self.scheduled_interval
                if self.generation < _MAX_QUALIFICATION_GENERATION
                else None
            ),
            "timeout": self.timeout,
            "generation": self.generation,
            "in_flight_generation": self.in_flight_generation,
            "attempts": self.attempts,
            "successes": self.successes,
            "failures": self.failures,
            "total_elapsed": self.total_elapsed,
            "last_elapsed": self.last_elapsed,
        }


def complete_cluster_lock_renewal(
    controller: ClusterLockRenewalController,
    generation: int,
    result: ClusterLockQualificationResult,
    *,
    completed_at: float | None = None,
    monotonic: typing.Callable[[], float] = time.monotonic,
) -> tuple[bool, str]:
    """Observe completion time and contain ordinary failures on one rank."""
    if not isinstance(controller, ClusterLockRenewalController):
        return False, "invalid cluster lock renewal controller"
    try:
        observed_completion = monotonic() if completed_at is None else completed_at
        return controller.complete(
            generation,
            result,
            completed_at=observed_completion,
        ), ""
    except Exception as error:
        # A per-rank exception must enter the shared fatal-control path instead
        # of unwinding one rank while peers remain inside the MPI protocol.
        return False, _bounded_exception_detail(error)


def begin_cluster_lock_renewal(
    comm: typing.Any,
    controller: ClusterLockRenewalController,
) -> tuple[int, tuple[typing.Any, ...], str]:
    """Broadcast one root-authorized renewal request without blocking."""
    if not isinstance(controller, ClusterLockRenewalController):
        return 0, (), "invalid cluster lock renewal controller"
    try:
        rank = int(comm.Get_rank())
        size = int(comm.Get_size())
    except Exception as error:
        return 0, (), _bounded_exception_message(
            "cluster lock renewal communicator failed: ", error)
    if rank != 0 or size < 2:
        return 0, (), "cluster lock renewal must begin on a multi-master root"
    prior_in_flight = controller.in_flight_generation
    try:
        request = controller.begin_request()
        generation_value = request["generation"]
        if (
            type(generation_value) is not int
            or generation_value <= 0
            or generation_value > _MAX_QUALIFICATION_GENERATION
            or generation_value != controller.in_flight_generation
        ):
            raise ValueError("invalid cluster lock renewal request generation")
        generation = generation_value
    except Exception as error:
        current_in_flight = controller.in_flight_generation
        recoverable_generation = (
            current_in_flight
            if prior_in_flight == 0
            and type(current_in_flight) is int
            and current_in_flight > 0
            else 0
        )
        return recoverable_generation, (), _bounded_exception_message(
            "cluster lock renewal request preparation failed: ", error)
    sends: list[typing.Any] = []
    failure = ""
    for peer in range(1, size):
        try:
            sends.append(comm.isend(
                request,
                dest=peer,
                tag=CLUSTER_LOCK_RENEWAL_TAG,
            ))
        except Exception as error:
            failure = failure or _bounded_exception_message(
                "cluster lock renewal request send failed: ",
                error,
            )
    return generation, tuple(sends), failure


def poll_cluster_lock_renewal(
    comm: typing.Any,
    controller: ClusterLockRenewalController,
) -> tuple[int, str, bool]:
    """Accept one root request; report whether it was safely classified."""
    if not isinstance(controller, ClusterLockRenewalController):
        return 0, "invalid cluster lock renewal controller", False
    try:
        rank = int(comm.Get_rank())
        size = int(comm.Get_size())
    except Exception as error:
        return 0, _bounded_exception_message(
            "cluster lock renewal communicator failed: ", error), False
    if rank <= 0 or rank >= size:
        return 0, "cluster lock renewal polling requires a peer master", False
    try:
        if not comm.iprobe(source=0, tag=CLUSTER_LOCK_RENEWAL_TAG):
            return 0, "", False
        request = comm.recv(source=0, tag=CLUSTER_LOCK_RENEWAL_TAG)
    except Exception as error:
        return 0, _bounded_exception_message(
            "cluster lock renewal request receive failed: ", error), False
    try:
        generation, error = controller.accept_request(request)
    except Exception as exception:
        # The message was consumed but could not be safely classified.  Report
        # a local control failure instead of treating it as quarantinable input.
        return 0, _bounded_exception_message(
            "cluster lock renewal request admission failed: ", exception), False
    return generation, error, True


def _request_completed(request: typing.Any) -> bool:
    result = request.Test()
    if isinstance(result, tuple):
        result = result[0]
    return bool(result)


def observe_cluster_lock_renewal_delivery(
    requests: tuple[typing.Any, ...],
) -> ClusterLockRenewalDeliveryObservation:
    """Classify each nonblocking send once and contain ordinary failures."""
    if type(requests) is not tuple:
        return ClusterLockRenewalDeliveryObservation(
            0, 0, 0, 0, "invalid cluster lock renewal delivery requests")
    total = len(requests)
    if len({id(request) for request in requests}) != total:
        return ClusterLockRenewalDeliveryObservation(
            total,
            0,
            0,
            total,
            "duplicate cluster lock renewal delivery request",
        )

    completed = 0
    incomplete = 0
    uncertain = 0
    failure = ""
    for index, request in enumerate(requests):
        try:
            if _request_completed(request):
                completed += 1
            else:
                incomplete += 1
        except Exception as error:
            uncertain += 1
            failure = failure or _bounded_exception_message(
                f"cluster lock renewal delivery request {index} failed: ",
                error,
            )
    return ClusterLockRenewalDeliveryObservation(
        total,
        completed,
        incomplete,
        uncertain,
        failure,
    )


def _valid_exchange_envelope(
    value: typing.Any,
    *,
    token: str,
    phase: str,
    comm_rank: int,
) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {
            "schema", "token", "phase", "comm_rank", "payload"
        }
        and value.get("schema") == "symcc-cluster-lock-exchange-v1"
        and value.get("token") == token
        and value.get("phase") == phase
        and type(value.get("comm_rank")) is int
        and value.get("comm_rank") == comm_rank
        and isinstance(value.get("payload"), dict)
    )


def _bounded_master_exchange(
    comm: typing.Any,
    payload: dict[str, typing.Any],
    *,
    token: str,
    phase: str,
    deadline: float,
    monotonic: typing.Callable[[], float] = time.monotonic,
    sleep: typing.Callable[[float], None] = time.sleep,
) -> tuple[tuple[dict[str, typing.Any], ...], str]:
    """All-gather Python objects through rank 0 without an unbounded wait."""
    try:
        rank = int(comm.Get_rank())
        size = int(comm.Get_size())
    except (AttributeError, MPI.Exception, OSError, RuntimeError, ValueError) as error:
        return (), f"MPI communicator query failed: {_bounded_text(error)}"
    if size < 1 or rank < 0 or rank >= size:
        return (), "invalid master communicator topology"

    envelope = {
        "schema": "symcc-cluster-lock-exchange-v1",
        "token": token,
        "phase": phase,
        "comm_rank": rank,
        "payload": payload,
    }
    if rank == 0:
        records: dict[int, dict[str, typing.Any]] = {0: envelope}
        failure = ""
        poll_delay = 0.0005
        first_poll = True
        while first_poll or monotonic() < deadline:
            first_poll = False
            progressed = False
            for peer in range(1, size):
                if peer in records:
                    continue
                try:
                    if not comm.iprobe(source=peer, tag=_TAG_CLUSTER_GATHER):
                        continue
                    candidate = comm.recv(
                        source=peer, tag=_TAG_CLUSTER_GATHER)
                except (AttributeError, MPI.Exception, OSError, RuntimeError) as error:
                    failure = f"MPI receive failed: {_bounded_text(error)}"
                    break
                progressed = True
                if not _valid_exchange_envelope(
                    candidate,
                    token=token,
                    phase=phase,
                    comm_rank=peer,
                ):
                    failure = f"malformed cluster exchange from master {peer}"
                    break
                records[peer] = candidate
            if failure or len(records) == size:
                break
            remaining = deadline - monotonic()
            if remaining <= 0.0:
                break
            poll_delay = 0.0005 if progressed else min(0.01, poll_delay * 2.0)
            sleep(min(poll_delay, remaining))

        if not failure and len(records) != size:
            missing = sorted(set(range(size)) - set(records))
            failure = f"cluster exchange timed out waiting for masters {missing}"
        ordered = tuple(records[index] for index in sorted(records))
        response = {
            "schema": "symcc-cluster-lock-exchange-result-v1",
            "token": token,
            "phase": phase,
            "ok": not failure,
            "error": failure,
            "records": list(ordered),
        }
        requests: list[typing.Any] = []
        for peer in range(1, size):
            try:
                requests.append(comm.isend(
                    response, dest=peer, tag=_TAG_CLUSTER_GATHER_RESULT))
            except (AttributeError, MPI.Exception, OSError, RuntimeError) as error:
                failure = f"MPI response send failed: {_bounded_text(error)}"
                break
        first_poll = True
        poll_delay = 0.0005
        while requests and (first_poll or monotonic() < deadline):
            first_poll = False
            remaining_requests = []
            for request in requests:
                try:
                    if not _request_completed(request):
                        remaining_requests.append(request)
                except (AttributeError, MPI.Exception, OSError, RuntimeError) as error:
                    failure = f"MPI response completion failed: {_bounded_text(error)}"
            requests = remaining_requests
            if not requests:
                break
            remaining = deadline - monotonic()
            if remaining <= 0.0:
                break
            poll_delay = min(0.01, poll_delay * 2.0)
            sleep(min(poll_delay, remaining))
        if requests and not failure:
            failure = "cluster exchange response timed out"
        return tuple(item["payload"] for item in ordered), failure

    try:
        request = comm.isend(envelope, dest=0, tag=_TAG_CLUSTER_GATHER)
    except (AttributeError, MPI.Exception, OSError, RuntimeError) as error:
        return (), f"MPI exchange send failed: {_bounded_text(error)}"
    sent = False
    response_records: tuple[dict[str, typing.Any], ...] | None = None
    response_error = ""
    poll_delay = 0.0005
    first_poll = True
    while first_poll or monotonic() < deadline:
        first_poll = False
        try:
            if not sent:
                sent = _request_completed(request)
            if (
                response_records is None
                and not response_error
                and comm.iprobe(source=0, tag=_TAG_CLUSTER_GATHER_RESULT)
            ):
                response = comm.recv(
                    source=0, tag=_TAG_CLUSTER_GATHER_RESULT)
                if (
                    not isinstance(response, dict)
                    or set(response) != {
                        "schema", "token", "phase", "ok", "error", "records"
                    }
                    or response.get("schema")
                    != "symcc-cluster-lock-exchange-result-v1"
                    or response.get("token") != token
                    or response.get("phase") != phase
                    or type(response.get("ok")) is not bool
                    or not isinstance(response.get("error"), str)
                    or not isinstance(response.get("records"), list)
                ):
                    return (), "malformed cluster exchange response"
                if not response["ok"]:
                    response_error = _bounded_text(
                        response["error"] or "cluster exchange failed")
                else:
                    records = response["records"]
                    if len(records) != size or any(
                        not _valid_exchange_envelope(
                            item,
                            token=token,
                            phase=phase,
                            comm_rank=index,
                        )
                        for index, item in enumerate(records)
                    ):
                        return (), "incomplete cluster exchange response"
                    response_records = tuple(records)
            if sent and (response_records is not None or response_error):
                if response_error:
                    return (), response_error
                assert response_records is not None
                return tuple(
                    item["payload"] for item in response_records), ""
        except (AttributeError, MPI.Exception, OSError, RuntimeError) as error:
            return (), f"MPI exchange failed: {_bounded_text(error)}"
        remaining = deadline - monotonic()
        if remaining <= 0.0:
            break
        poll_delay = min(0.01, poll_delay * 2.0)
        sleep(min(poll_delay, remaining))
    if response_records is not None or response_error:
        return (), "cluster exchange send did not complete before the deadline"
    return (), "cluster exchange timed out waiting for rank 0"


def _valid_renewal_configuration_record(value: typing.Any) -> bool:
    if (
        not isinstance(value, dict)
        or set(value) != {
            "schema",
            "epoch",
            "interval",
            "timeout",
            "jitter_fraction",
            "maximum_interval",
            "fingerprint",
        }
        or value.get("schema") != "symcc-cluster-lock-renewal-config-v1"
        or not _valid_token(value.get("epoch"))
        or type(value.get("interval")) is not float
        or type(value.get("timeout")) is not float
        or type(value.get("jitter_fraction")) is not float
        or type(value.get("maximum_interval")) is not float
        or not _valid_token(value.get("fingerprint"))
    ):
        return False
    interval = value["interval"]
    timeout = value["timeout"]
    jitter = value["jitter_fraction"]
    maximum = value["maximum_interval"]
    if (
        not math.isfinite(interval)
        or interval <= 0.0
        or not math.isfinite(timeout)
        or timeout <= 0.0
        or not math.isfinite(jitter)
        or jitter < 0.0
        or jitter > 0.5
        or not math.isfinite(maximum)
        or maximum != interval * (1.0 + jitter)
    ):
        return False
    try:
        expected = _renewal_configuration_fingerprint(
            value["epoch"],
            interval,
            timeout,
            jitter,
        )
    except (TypeError, ValueError, OverflowError):
        return False
    return value["fingerprint"] == expected


def qualify_cluster_lock_renewal_configuration(
    comm: typing.Any,
    controller: ClusterLockRenewalController,
    *,
    timeout: float = 5.0,
    monotonic: typing.Callable[[], float] = time.monotonic,
    sleep: typing.Callable[[float], None] = time.sleep,
) -> tuple[str, str]:
    """Reach bounded exact agreement on one runtime renewal configuration."""
    if not isinstance(controller, ClusterLockRenewalController):
        return "", "invalid cluster lock renewal controller"
    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError, OverflowError):
        return "", "invalid cluster lock renewal configuration timeout"
    if not math.isfinite(timeout_value) or timeout_value <= 0.0:
        return "", "invalid cluster lock renewal configuration timeout"
    timeout_value = min(60.0, timeout_value)
    local = controller.configuration_snapshot()
    if not _valid_renewal_configuration_record(local):
        return "", "invalid local cluster lock renewal configuration"
    token = hashlib.sha256(
        b"symcc-cluster-lock-renewal-config-exchange-v1\0"
        + bytes.fromhex(controller.epoch)
    ).hexdigest()
    records, error = _bounded_master_exchange(
        comm,
        local,
        token=token,
        phase="renewal-configuration",
        deadline=monotonic() + timeout_value,
        monotonic=monotonic,
        sleep=sleep,
    )
    if error:
        return "", error
    if not records:
        return "", "empty cluster lock renewal configuration exchange"
    for rank, record in enumerate(records):
        if not _valid_renewal_configuration_record(record):
            return "", (
                f"malformed cluster lock renewal configuration from master {rank}"
            )
    fingerprints = tuple(record["fingerprint"] for record in records)
    if len(set(fingerprints)) != 1 or any(record != local for record in records):
        summary = ",".join(
            f"{rank}:{fingerprint[:12]}"
            for rank, fingerprint in enumerate(fingerprints)
        )
        return "", f"cluster lock renewal configuration mismatch ({summary})"
    try:
        controller._record_configuration_consensus(fingerprints[0])
    except RuntimeError as error:
        return "", str(error)
    return fingerprints[0], ""


def _lock_file_content(token: str) -> bytes:
    return f"symcc-cross-host-lock-v1\n{token}\n".encode("ascii")


def _lock_open_flags(*, create: bool = False) -> int:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise OSError(
            errno.EOPNOTSUPP,
            "O_NOFOLLOW is required for cluster lock identity",
        )
    flags = os.O_RDWR
    if create:
        flags |= os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= no_follow
    return flags


def _open_lock_root(path: str) -> int:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if no_follow is None or directory is None:
        raise OSError(
            errno.EOPNOTSUPP,
            "descriptor-anchored cluster qualification requires "
            "O_NOFOLLOW and O_DIRECTORY",
            path,
        )
    descriptor = os.open(
        path,
        os.O_RDONLY | no_follow | directory | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        metadata = os.fstat(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        raise OSError(
            errno.ENOTDIR,
            "cluster lock root is not a directory",
            path,
        )
    return descriptor


def _directory_identity(descriptor: int, path: str) -> tuple[int, int]:
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        raise OSError(errno.ENOTDIR, "cluster lock root is not a directory", path)
    return int(metadata.st_dev), int(metadata.st_ino)


def _regular_identity(descriptor: int, path: str) -> tuple[int, int]:
    _verify_regular_descriptor(descriptor, path)
    metadata = os.fstat(descriptor)
    return int(metadata.st_dev), int(metadata.st_ino)


def _capability_binding_error(
    capability: SharedFilesystemCapabilities,
) -> str:
    """Check that both qualified roots still name the observed filesystems."""
    bindings = (
        (
            "state root",
            capability.root,
            capability.device,
            capability.filesystem_id,
        ),
        (
            "publication root",
            capability.publication_root,
            capability.publication_device,
            capability.publication_filesystem_id,
        ),
    )
    for label, path, expected_device, expected_filesystem_id in bindings:
        descriptor: int | None = None
        try:
            descriptor = _open_lock_root(path)
            observed_device = int(os.fstat(descriptor).st_dev)
            observed_filesystem_id = int(
                getattr(os.fstatvfs(descriptor), "f_fsid", 0)
            )
        except (OSError, TypeError, ValueError) as error:
            return f"{label} capability binding failed: {_bounded_text(error)}"
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if (
            observed_device != expected_device
            or observed_filesystem_id != expected_filesystem_id
        ):
            return (
                f"{label} filesystem identity changed: "
                f"expected dev/fsid={expected_device}/{expected_filesystem_id}, "
                f"observed={observed_device}/{observed_filesystem_id}"
            )
    return ""


def _read_descriptor(descriptor: int, limit: int = 4096) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    content = os.read(descriptor, limit + 1)
    if len(content) > limit:
        raise OSError(errno.EFBIG, "cluster lock record is oversized")
    return content


def _write_descriptor(descriptor: int, content: bytes) -> None:
    os.ftruncate(descriptor, 0)
    os.lseek(descriptor, 0, os.SEEK_SET)
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise OSError(errno.EIO, "short cluster lock record write")
        offset += written
    os.fsync(descriptor)


def _verify_regular_descriptor(descriptor: int, path: str) -> None:
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        raise OSError(errno.EINVAL, "cluster lock path is not regular", path)


def _prepare_lock_file(
    root_descriptor: int,
    leaf: str,
    public_path: str,
    expected: bytes,
) -> None:
    descriptor = os.open(
        leaf,
        _lock_open_flags(create=True),
        0o600,
        dir_fd=root_descriptor,
    )
    created_content = False
    try:
        _verify_regular_descriptor(descriptor, public_path)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        observed = _read_descriptor(descriptor)
        if not observed:
            _write_descriptor(descriptor, expected)
            created_content = True
        elif observed != expected:
            raise OSError(errno.EINVAL, "cluster lock record identity mismatch")
    finally:
        os.close(descriptor)
    if created_content:
        os.fsync(root_descriptor)


def _open_verified_lock_file(
    root_descriptor: int,
    leaf: str,
    public_path: str,
    expected: bytes,
) -> int:
    descriptor = os.open(
        leaf,
        _lock_open_flags(),
        dir_fd=root_descriptor,
    )
    try:
        _verify_regular_descriptor(descriptor, public_path)
        if _read_descriptor(descriptor) != expected:
            raise OSError(errno.EINVAL, "cluster lock record identity mismatch")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _verify_lock_namespace_anchor(
    root: str,
    root_descriptor: int,
    lock_descriptor: int,
    leaf: str,
    expected: bytes,
) -> None:
    """Close the proof over the public root and lock pathname identities."""
    public_path = os.path.join(root, leaf)
    expected_root_identity = _directory_identity(root_descriptor, root)
    expected_lock_identity = _regular_identity(lock_descriptor, public_path)
    reopened_root = _open_lock_root(root)
    reopened_lock: int | None = None
    try:
        if _directory_identity(reopened_root, root) != expected_root_identity:
            raise OSError(
                getattr(errno, "ESTALE", errno.EIO),
                "cluster lock root identity changed",
                root,
            )
        reopened_lock = _open_verified_lock_file(
            reopened_root,
            leaf,
            public_path,
            expected,
        )
        if _regular_identity(reopened_lock, public_path) != expected_lock_identity:
            raise OSError(
                getattr(errno, "ESTALE", errno.EIO),
                "cluster lock path identity changed",
                public_path,
            )
    finally:
        if reopened_lock is not None:
            os.close(reopened_lock)
        os.close(reopened_root)


def _try_exclusive_lock(descriptor: int) -> tuple[str, str]:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return "acquired", ""
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EAGAIN}:
            return "blocked", ""
        return "error", f"flock errno={error.errno}: {_bounded_text(error)}"


def _phase_ok(
    records: tuple[dict[str, typing.Any], ...],
    expected_ranks: tuple[int, ...],
) -> tuple[bool, str]:
    if len(records) != len(expected_ranks):
        return False, "incomplete cluster phase records"
    for expected_rank, record in zip(expected_ranks, records):
        if (
            not isinstance(record, dict)
            or type(record.get("global_rank")) is not int
            or record.get("global_rank") != expected_rank
            or type(record.get("ok")) is not bool
            or not isinstance(record.get("error", ""), str)
        ):
            return False, "malformed cluster phase record"
        if not record["ok"]:
            return False, (
                f"master {expected_rank}: "
                f"{_bounded_text(record.get('error') or 'phase failed')}"
            )
    return True, ""


def _commit_qualification_transcript(
    comm: typing.Any,
    *,
    global_rank: int,
    masters: tuple[int, ...],
    generation: int,
    transcript: str,
    local_error: str,
    token: str,
    deadline: float,
    monotonic: typing.Callable[[], float] = time.monotonic,
    sleep: typing.Callable[[float], None] = time.sleep,
) -> str:
    """Require every master to commit the same generation-bound transcript."""
    records, exchange_error = _bounded_master_exchange(
        comm,
        {
            "global_rank": global_rank,
            "ok": not local_error,
            "error": _bounded_text(local_error),
            "generation": generation,
            "transcript": transcript,
        },
        token=token,
        phase="qualification-transcript-commit",
        deadline=deadline,
        monotonic=monotonic,
        sleep=sleep,
    )
    phase_clean, phase_error = _phase_ok(records, masters)
    if exchange_error or not phase_clean:
        return exchange_error or phase_error
    for expected_rank, record in zip(masters, records):
        if (
            set(record) != {
                "global_rank", "ok", "error", "generation", "transcript"
            }
            or record["global_rank"] != expected_rank
            or record["generation"] != generation
            or not _valid_token(record["transcript"])
        ):
            return "malformed cluster qualification transcript record"
        if record["transcript"] != transcript:
            return "cluster qualification transcript mismatch"
    return ""


def _exercise_cluster_lock_namespace(
    comm: typing.Any,
    *,
    root: str,
    global_rank: int,
    masters: tuple[int, ...],
    representatives: tuple[int, ...],
    token: str,
    lock_token: str,
    deadline: float,
    monotonic: typing.Callable[[], float],
    sleep: typing.Callable[[float], None],
) -> tuple[int, int, int, int, str]:
    """Run the lock litmus while retaining root and leaf capabilities."""
    public_path = os.path.join(root, _LOCK_FILENAME)
    expected_content = _lock_file_content(lock_token)
    root_descriptor: int | None = None
    lock_anchor: int | None = None
    root_error = ""
    try:
        root_descriptor = _open_lock_root(root)
    except OSError as error:
        root_error = _bounded_text(error)
    records, exchange_error = _bounded_master_exchange(
        comm,
        {
            "global_rank": global_rank,
            "ok": root_descriptor is not None,
            "error": root_error,
        },
        token=token,
        phase="root-anchor-open",
        deadline=deadline,
        monotonic=monotonic,
        sleep=sleep,
    )
    phase_clean, phase_error = _phase_ok(records, masters)
    if exchange_error or not phase_clean:
        if root_descriptor is not None:
            os.close(root_descriptor)
        return 0, 0, 0, 0, exchange_error or phase_error
    assert root_descriptor is not None

    try:
        prepare_error = ""
        if global_rank == masters[0]:
            try:
                _prepare_lock_file(
                    root_descriptor,
                    _LOCK_FILENAME,
                    public_path,
                    expected_content,
                )
            except OSError as error:
                prepare_error = _bounded_text(error)
        records, exchange_error = _bounded_master_exchange(
            comm,
            {
                "global_rank": global_rank,
                "ok": not prepare_error,
                "error": prepare_error,
            },
            token=token,
            phase="prepare",
            deadline=deadline,
            monotonic=monotonic,
            sleep=sleep,
        )
        phase_clean, phase_error = _phase_ok(records, masters)
        if exchange_error or not phase_clean:
            return 0, 0, 0, 0, exchange_error or phase_error

        anchor_error = ""
        try:
            lock_anchor = _open_verified_lock_file(
                root_descriptor,
                _LOCK_FILENAME,
                public_path,
                expected_content,
            )
        except OSError as error:
            anchor_error = _bounded_text(error)
        records, exchange_error = _bounded_master_exchange(
            comm,
            {
                "global_rank": global_rank,
                "ok": lock_anchor is not None,
                "error": anchor_error,
            },
            token=token,
            phase="lock-anchor-open",
            deadline=deadline,
            monotonic=monotonic,
            sleep=sleep,
        )
        phase_clean, phase_error = _phase_ok(records, masters)
        if exchange_error or not phase_clean:
            return 0, 0, 0, 0, exchange_error or phase_error
        assert lock_anchor is not None

        completed_rounds = 0
        contention_checks = 0
        release_checks = 0
        for round_index, holder in enumerate(representatives):
            verifier = representatives[(round_index + 1) % len(representatives)]
            descriptor: int | None = None
            open_error = ""
            try:
                descriptor = _open_verified_lock_file(
                    root_descriptor,
                    _LOCK_FILENAME,
                    public_path,
                    expected_content,
                )
            except OSError as error:
                open_error = _bounded_text(error)
            records, exchange_error = _bounded_master_exchange(
                comm,
                {
                    "global_rank": global_rank,
                    "ok": descriptor is not None,
                    "error": open_error,
                },
                token=token,
                phase=f"round-{round_index}-open",
                deadline=deadline,
                monotonic=monotonic,
                sleep=sleep,
            )
            phase_clean, phase_error = _phase_ok(records, masters)
            if exchange_error or not phase_clean:
                if descriptor is not None:
                    os.close(descriptor)
                return (
                    completed_rounds,
                    contention_checks,
                    release_checks,
                    0,
                    exchange_error or phase_error,
                )

            hold_error = ""
            if global_rank == holder:
                assert descriptor is not None
                state, detail = _try_exclusive_lock(descriptor)
                if state != "acquired":
                    hold_error = detail or f"holder lock state was {state}"
            records, exchange_error = _bounded_master_exchange(
                comm,
                {
                    "global_rank": global_rank,
                    "ok": not hold_error,
                    "error": hold_error,
                },
                token=token,
                phase=f"round-{round_index}-held",
                deadline=deadline,
                monotonic=monotonic,
                sleep=sleep,
            )
            phase_clean, phase_error = _phase_ok(records, masters)
            if exchange_error or not phase_clean:
                if descriptor is not None:
                    os.close(descriptor)
                return (
                    completed_rounds,
                    contention_checks,
                    release_checks,
                    0,
                    exchange_error or phase_error,
                )

            contention_error = ""
            if global_rank != holder:
                assert descriptor is not None
                state, detail = _try_exclusive_lock(descriptor)
                if state == "acquired":
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    except OSError:
                        pass
                    contention_error = (
                        "remote master acquired a held cluster lock"
                    )
                elif state != "blocked":
                    contention_error = (
                        detail or f"contender lock state was {state}"
                    )
            records, exchange_error = _bounded_master_exchange(
                comm,
                {
                    "global_rank": global_rank,
                    "ok": not contention_error,
                    "error": contention_error,
                },
                token=token,
                phase=f"round-{round_index}-excluded",
                deadline=deadline,
                monotonic=monotonic,
                sleep=sleep,
            )
            phase_clean, phase_error = _phase_ok(records, masters)
            if descriptor is not None:
                os.close(descriptor)
                descriptor = None
            if exchange_error or not phase_clean:
                return (
                    completed_rounds,
                    contention_checks,
                    release_checks,
                    0,
                    exchange_error or phase_error,
                )
            contention_checks += len(masters) - 1

            records, exchange_error = _bounded_master_exchange(
                comm,
                {"global_rank": global_rank, "ok": True, "error": ""},
                token=token,
                phase=f"round-{round_index}-released",
                deadline=deadline,
                monotonic=monotonic,
                sleep=sleep,
            )
            phase_clean, phase_error = _phase_ok(records, masters)
            if exchange_error or not phase_clean:
                return (
                    completed_rounds,
                    contention_checks,
                    release_checks,
                    0,
                    exchange_error or phase_error,
                )

            release_error = ""
            if global_rank == verifier:
                verifier_descriptor: int | None = None
                try:
                    verifier_descriptor = _open_verified_lock_file(
                        root_descriptor,
                        _LOCK_FILENAME,
                        public_path,
                        expected_content,
                    )
                    state, detail = _try_exclusive_lock(verifier_descriptor)
                    if state != "acquired":
                        release_error = detail or (
                            f"released lock state was {state}"
                        )
                except OSError as error:
                    release_error = _bounded_text(error)
                finally:
                    if verifier_descriptor is not None:
                        os.close(verifier_descriptor)
            records, exchange_error = _bounded_master_exchange(
                comm,
                {
                    "global_rank": global_rank,
                    "ok": not release_error,
                    "error": release_error,
                },
                token=token,
                phase=f"round-{round_index}-reacquired",
                deadline=deadline,
                monotonic=monotonic,
                sleep=sleep,
            )
            phase_clean, phase_error = _phase_ok(records, masters)
            if exchange_error or not phase_clean:
                return (
                    completed_rounds,
                    contention_checks,
                    release_checks,
                    0,
                    exchange_error or phase_error,
                )
            completed_rounds += 1
            release_checks += 1

        identity_error = ""
        try:
            _verify_lock_namespace_anchor(
                root,
                root_descriptor,
                lock_anchor,
                _LOCK_FILENAME,
                expected_content,
            )
        except OSError as error:
            identity_error = _bounded_text(error)
        records, exchange_error = _bounded_master_exchange(
            comm,
            {
                "global_rank": global_rank,
                "ok": not identity_error,
                "error": identity_error,
            },
            token=token,
            phase="namespace-identity-closed",
            deadline=deadline,
            monotonic=monotonic,
            sleep=sleep,
        )
        phase_clean, phase_error = _phase_ok(records, masters)
        if exchange_error or not phase_clean:
            return (
                completed_rounds,
                contention_checks,
                release_checks,
                0,
                exchange_error or phase_error,
            )
        return (
            completed_rounds,
            contention_checks,
            release_checks,
            len(masters),
            "",
        )
    finally:
        if lock_anchor is not None:
            os.close(lock_anchor)
        os.close(root_descriptor)


def _qualify_mpi_cluster_advisory_lock(
    comm: typing.Any,
    capability: SharedFilesystemCapabilities | None,
    *,
    root: str,
    epoch: str,
    global_rank: int,
    expected_master_ranks: typing.Iterable[int],
    processor_name: str,
    qualification_generation: int = 0,
    timeout: float = 30.0,
    local_error: str = "",
    monotonic: typing.Callable[[], float] = time.monotonic,
    sleep: typing.Callable[[float], None] = time.sleep,
) -> ClusterLockQualificationResult:
    """Execute the bounded lock protocol on every actual MPI master host.

    Every representative host holds the inode once. All other masters must see
    nonblocking contention, then a representative on a different host must
    acquire the inode after descriptor-close release. Any missing, malformed,
    contradictory, or late observation fails the whole qualification.
    """
    started = monotonic()
    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError, OverflowError):
        timeout_value = 30.0
    if not math.isfinite(timeout_value):
        timeout_value = 30.0
    timeout_value = min(3600.0, max(0.001, timeout_value))
    deadline = started + timeout_value
    generation_valid = (
        type(qualification_generation) is int
        and 0 <= qualification_generation <= _MAX_QUALIFICATION_GENERATION
    )
    normalized_generation = qualification_generation if generation_valid else 0

    masters = tuple(expected_master_ranks)
    if (
        not masters
        or any(
            type(rank) is not int
            or rank < 0
            or rank > _MAX_QUALIFICATION_GENERATION
            for rank in masters
        )
        or tuple(sorted(set(masters))) != masters
        or type(global_rank) is not int
        or global_rank not in masters
    ):
        return ClusterLockQualificationResult(
            False, False, capability, (), (), 0, 0, 0,
            max(0.0, monotonic() - started),
            "invalid expected master topology",
            qualification_generation=normalized_generation,
        )
    processor_valid = (
        isinstance(processor_name, str)
        and bool(processor_name)
        and len(processor_name) <= 255
        and "\x00" not in processor_name
    )
    epoch_valid = _valid_token(epoch)
    lock_token = hashlib.sha256(
        b"symcc-cluster-lock-v1\0" + (
            epoch.encode("ascii") if epoch_valid else b"invalid-epoch")
    ).hexdigest()
    token = _qualification_exchange_token(
        lock_token,
        normalized_generation,
    )
    root = os.path.realpath(os.path.abspath(root))
    local_failure = _bounded_text(local_error) if local_error else ""
    if not generation_valid:
        local_failure = "invalid cluster lock qualification generation"
    if not processor_valid:
        local_failure = local_failure or "invalid MPI processor identity"
    if not epoch_valid:
        local_failure = "invalid cluster lock epoch"
    if capability is None:
        local_failure = local_failure or "local filesystem capability is unavailable"
    elif not isinstance(capability, SharedFilesystemCapabilities):
        local_failure = "invalid local filesystem capability"
    elif os.path.realpath(root) != capability.root:
        local_failure = "local capability root does not match cluster lock root"
    elif (
        capability.advisory_lock_exclusion is not True
        or capability.advisory_lock_release is not True
        or set(FULL_SHARED_FILESYSTEM_REQUIREMENTS.required_operations)
        - set(capability.required_operations)
    ):
        local_failure = "local full filesystem capability is incomplete"
    else:
        local_failure = local_failure or _capability_binding_error(capability)

    membership_payload = {
        "global_rank": global_rank,
        "processor": processor_name if processor_valid else "",
        "local_probe_ok": not local_failure,
        "local_error": local_failure,
    }
    membership_records, exchange_error = _bounded_master_exchange(
        comm,
        membership_payload,
        token=token,
        phase="membership",
        deadline=deadline,
        monotonic=monotonic,
        sleep=sleep,
    )
    if exchange_error:
        return ClusterLockQualificationResult(
            False, False, capability, (), (), 0, 0, 0,
            max(0.0, monotonic() - started), exchange_error,
            qualification_generation=normalized_generation,
        )

    members: list[tuple[int, str]] = []
    for expected_rank, record in zip(masters, membership_records):
        if (
            not isinstance(record, dict)
            or set(record) != {
                "global_rank", "processor", "local_probe_ok", "local_error"
            }
            or type(record.get("global_rank")) is not int
            or record.get("global_rank") != expected_rank
            or not isinstance(record.get("processor"), str)
            or (
                record.get("local_probe_ok") is True
                and not record.get("processor")
            )
            or len(record.get("processor", "")) > 255
            or "\x00" in record.get("processor", "")
            or type(record.get("local_probe_ok")) is not bool
            or not isinstance(record.get("local_error"), str)
        ):
            return ClusterLockQualificationResult(
                False, False, capability, (), (), 0, 0, 0,
                max(0.0, monotonic() - started),
                "malformed cluster membership record",
                qualification_generation=normalized_generation,
            )
        members.append((expected_rank, record["processor"]))
        if not record["local_probe_ok"]:
            return ClusterLockQualificationResult(
                False, False, capability, tuple(members), (), 0, 0, 0,
                max(0.0, monotonic() - started),
                f"master {expected_rank} local probe failed: "
                f"{_bounded_text(record['local_error'])}",
                qualification_generation=normalized_generation,
            )
    if len(membership_records) != len(masters):
        return ClusterLockQualificationResult(
            False, False, capability, tuple(members), (), 0, 0, 0,
            max(0.0, monotonic() - started),
            "incomplete cluster membership",
            qualification_generation=normalized_generation,
        )

    representative_by_processor: dict[str, int] = {}
    for rank, processor in members:
        representative_by_processor.setdefault(processor, rank)
    representatives = tuple(representative_by_processor.values())
    if len(representatives) < 2:
        assert capability is not None
        observed_capability = replace(
            capability,
            cluster_lock_members=tuple(members),
            cluster_lock_representatives=representatives,
        )
        return ClusterLockQualificationResult(
            True, False, observed_capability, tuple(members), representatives,
            0, 0, 0, max(0.0, monotonic() - started),
            qualification_generation=normalized_generation,
        )

    (
        completed_rounds,
        contention_checks,
        release_checks,
        identity_checks,
        exercise_error,
    ) = _exercise_cluster_lock_namespace(
        comm,
        root=root,
        global_rank=global_rank,
        masters=masters,
        representatives=representatives,
        token=token,
        lock_token=lock_token,
        deadline=deadline,
        monotonic=monotonic,
        sleep=sleep,
    )
    if exercise_error:
        return ClusterLockQualificationResult(
            False, False, capability, tuple(members), representatives,
            completed_rounds, contention_checks, release_checks,
            max(0.0, monotonic() - started), exercise_error,
            identity_checks,
            qualification_generation=normalized_generation,
        )

    assert capability is not None
    try:
        upgraded = qualify_shared_filesystem_cluster_lock(
            capability,
            members=members,
            representatives=representatives,
            rounds=completed_rounds,
            contention_checks=contention_checks,
            release_checks=release_checks,
            identity_checks=identity_checks,
        )
    except (TypeError, ValueError) as error:
        return ClusterLockQualificationResult(
            False, False, capability, tuple(members), representatives,
            completed_rounds, contention_checks, release_checks,
            max(0.0, monotonic() - started), _bounded_text(error),
            identity_checks,
            qualification_generation=normalized_generation,
        )
    transcript = ""
    transcript_error = ""
    try:
        transcript = _qualification_proof_transcript(
            epoch,
            normalized_generation,
            members,
            representatives,
            completed_rounds,
            contention_checks,
            release_checks,
            identity_checks,
        )
    except (TypeError, UnicodeError, ValueError, OverflowError) as error:
        transcript_error = _bounded_text(error)
    transcript_error = _commit_qualification_transcript(
        comm,
        global_rank=global_rank,
        masters=masters,
        generation=normalized_generation,
        transcript=transcript,
        local_error=transcript_error,
        token=token,
        deadline=deadline,
        monotonic=monotonic,
        sleep=sleep,
    )
    if transcript_error:
        return ClusterLockQualificationResult(
            False, False, capability, tuple(members), representatives,
            completed_rounds, contention_checks, release_checks,
            max(0.0, monotonic() - started), transcript_error,
            identity_checks,
            qualification_generation=normalized_generation,
            proof_transcript=transcript,
        )
    return ClusterLockQualificationResult(
        True, True, upgraded, tuple(members), representatives,
        completed_rounds, contention_checks, release_checks,
        max(0.0, monotonic() - started), "", identity_checks,
        qualification_generation=normalized_generation,
        proof_transcript=transcript,
    )


def qualify_mpi_cluster_advisory_lock(
    comm: typing.Any,
    capability: SharedFilesystemCapabilities | None,
    *,
    root: str,
    epoch: str,
    global_rank: int,
    expected_master_ranks: typing.Iterable[int],
    processor_name: str,
    qualification_generation: int = 0,
    timeout: float = 30.0,
    local_error: str = "",
    monotonic: typing.Callable[[], float] = time.monotonic,
    sleep: typing.Callable[[float], None] = time.sleep,
) -> ClusterLockQualificationResult:
    """Return a fail-closed result for every ordinary protocol exception."""
    try:
        return _qualify_mpi_cluster_advisory_lock(
            comm,
            capability,
            root=root,
            epoch=epoch,
            global_rank=global_rank,
            expected_master_ranks=expected_master_ranks,
            processor_name=processor_name,
            qualification_generation=qualification_generation,
            timeout=timeout,
            local_error=local_error,
            monotonic=monotonic,
            sleep=sleep,
        )
    except Exception as error:
        prefix = "cluster lock qualification raised: "
        detail_limit = 512 - len(prefix)
        detail = _bounded_exception_detail(error, detail_limit)
        generation = (
            qualification_generation
            if type(qualification_generation) is int
            and 0 <= qualification_generation <= _MAX_QUALIFICATION_GENERATION
            else 0
        )
        return ClusterLockQualificationResult(
            clean=False,
            verified=False,
            capability=(
                capability
                if isinstance(capability, SharedFilesystemCapabilities)
                else None
            ),
            members=(),
            representatives=(),
            rounds=0,
            contention_checks=0,
            release_checks=0,
            elapsed=0.0,
            error=prefix + detail,
            qualification_generation=generation,
        )
