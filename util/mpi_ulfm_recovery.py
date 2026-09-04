#!/usr/bin/env python3
"""Generation-fenced MPI/ULFM communicator and shard recovery.

MPI ranks are transport-local names.  This module keeps durable endpoint and
shard identities separate from those names, then joins a repaired communicator
to the durable state with a content-addressed membership attestation.  Every
in-flight lease is conservatively replayed after repair; messages from the old
communicator generation therefore cannot retire work in the new generation.

The pure :class:`UlfmRecoveryController` is deterministic and replayable.  The
``shrink_and_attest`` adapter is the only part that touches mpi4py and can be
tested with either a real ULFM runtime or a deterministic communicator double.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from array import array
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence


ULFM_RECOVERY_PROTOCOL = "symcc-generation-fenced-ulfm-recovery-v1"
ULFM_POLICY_SCHEMA = "symcc-ulfm-recovery-policy-v1"
ULFM_ATTESTATION_SCHEMA = "symcc-ulfm-endpoint-attestation-v1"
ULFM_PLAN_SCHEMA = "symcc-ulfm-recovery-plan-v1"
ULFM_RECEIPT_SCHEMA = "symcc-ulfm-recovery-receipt-v1"
ULFM_SNAPSHOT_SCHEMA = "symcc-ulfm-recovery-snapshot-v1"
ULFM_CAPABILITY_SCHEMA = "symcc-ulfm-runtime-capability-v1"
ULFM_WORK_FENCE_SCHEMA = "symcc-ulfm-work-fence-v1"
ULFM_WORK_ENVELOPE_SCHEMA = "symcc-ulfm-work-envelope-v1"

MAX_ENDPOINTS = 4096
MAX_SHARDS = 262_144
MAX_RECOVERY_QUEUE = 262_144
MAX_ATTESTATION_BYTES = 4096
_HEX64 = re.compile(r"[0-9a-f]{64}")


class UlfmRecoveryError(ValueError):
    """A recovery policy, state transition, or attestation is invalid."""


class UlfmRuntimeError(RuntimeError):
    """The live MPI runtime could not complete a fail-closed repair."""


class UlfmCollectiveTimeout(UlfmRuntimeError):
    """A nonblocking ULFM collective did not complete before its deadline.

    MPI forbids freeing or cancelling an active nonblocking collective request.
    Retaining both the request and its communicator until process termination is
    therefore intentional.  Callers must treat this exception as fatal for the
    current process instead of retrying another collective on that communicator.
    """

    def __init__(self, message: str, *active_handles: Any) -> None:
        super().__init__(message)
        self.active_handles = tuple(active_handles)
        _QUARANTINED_COLLECTIVE_TIMEOUTS.append(self)

    def retain(self, *active_handles: Any) -> None:
        self.active_handles += tuple(active_handles)


# An unresolved collective cannot be safely destroyed while MPI is still live.
# A timeout is process-fatal in production, so this list remains naturally tiny.
_QUARANTINED_COLLECTIVE_TIMEOUTS: list[UlfmCollectiveTimeout] = []


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def content_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _clone(value: Any) -> Any:
    """Break protocol-boundary aliases while retaining canonical JSON types."""
    return json.loads(canonical_json(value))


def _integer(value: Any, name: str, lower: int, upper: int) -> int:
    if type(value) is not int or not lower <= value <= upper:
        raise UlfmRecoveryError(f"{name} must be in [{lower}, {upper}]")
    return value


def _identity(value: Any, name: str) -> str:
    if type(value) is not str:
        raise UlfmRecoveryError(f"{name} identity is invalid")
    encoded = value.encode("utf-8")
    if (
        not value
        or len(encoded) > 256
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise UlfmRecoveryError(f"{name} identity is invalid")
    return value


def _optional_identity(value: Any, name: str) -> str:
    if value == "":
        return ""
    return _identity(value, name)


def _digest(value: Any, name: str) -> str:
    if type(value) is not str or _HEX64.fullmatch(value) is None:
        raise UlfmRecoveryError(f"{name} must be a lowercase SHA-256")
    return value


def _bounded_sequence(value: Any, name: str, maximum: int) -> list[Any]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or len(value) > maximum
    ):
        raise UlfmRecoveryError(f"{name} must be a bounded list")
    return list(value)


@dataclass(frozen=True)
class UlfmRecoveryPolicy:
    max_endpoints: int = 4096
    max_shards: int = 65_536
    max_recovery_queue: int = 65_536
    max_repair_attempts: int = 3
    collective_timeout_seconds: float = 30.0
    poll_interval_seconds: float = 0.01

    def __post_init__(self) -> None:
        _integer(self.max_endpoints, "ULFM endpoint budget", 2, MAX_ENDPOINTS)
        _integer(self.max_shards, "ULFM shard budget", 1, MAX_SHARDS)
        _integer(
            self.max_recovery_queue,
            "ULFM recovery queue budget",
            1,
            MAX_RECOVERY_QUEUE,
        )
        _integer(self.max_repair_attempts, "ULFM repair attempts", 1, 64)
        for value, name, lower, upper in (
            (
                self.collective_timeout_seconds,
                "ULFM collective timeout",
                0.001,
                3600.0,
            ),
            (self.poll_interval_seconds, "ULFM poll interval", 0.0001, 1.0),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not lower <= float(value) <= upper
            ):
                raise UlfmRecoveryError(f"{name} must be in [{lower}, {upper}]")
        if self.poll_interval_seconds > self.collective_timeout_seconds:
            raise UlfmRecoveryError("ULFM poll interval exceeds collective timeout")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": ULFM_POLICY_SCHEMA,
            "protocol": ULFM_RECOVERY_PROTOCOL,
            "max_endpoints": self.max_endpoints,
            "max_shards": self.max_shards,
            "max_recovery_queue": self.max_recovery_queue,
            "max_repair_attempts": self.max_repair_attempts,
            "collective_timeout_seconds": float(self.collective_timeout_seconds),
            "poll_interval_seconds": float(self.poll_interval_seconds),
        }

    @property
    def sha256(self) -> str:
        return content_digest(self.as_dict())

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "UlfmRecoveryPolicy":
        if not isinstance(raw, Mapping):
            raise UlfmRecoveryError("ULFM recovery policy must be an object")
        defaults = cls()
        allowed = {
            key for key in defaults.as_dict() if key not in {"schema", "protocol"}
        }
        values = {key: getattr(defaults, key) for key in allowed}
        unknown = set(raw) - allowed
        if unknown:
            raise UlfmRecoveryError(
                "unknown ULFM policy option: " + sorted(unknown)[0]
            )
        values.update(raw)
        return cls(**values)

    @classmethod
    def from_sealed(cls, raw: Mapping[str, Any]) -> "UlfmRecoveryPolicy":
        if (
            not isinstance(raw, Mapping)
            or raw.get("schema") != ULFM_POLICY_SCHEMA
            or raw.get("protocol") != ULFM_RECOVERY_PROTOCOL
        ):
            raise UlfmRecoveryError("sealed ULFM policy scope changed")
        policy = cls.from_mapping(
            {
                key: value
                for key, value in raw.items()
                if key not in {"schema", "protocol"}
            }
        )
        if policy.as_dict() != dict(raw):
            raise UlfmRecoveryError("sealed ULFM policy changed")
        return policy


@dataclass(frozen=True)
class EndpointIdentity:
    endpoint_id: str
    initial_rank: int
    incarnation_sha256: str
    host_id: str

    def __post_init__(self) -> None:
        _identity(self.endpoint_id, "ULFM endpoint")
        _integer(self.initial_rank, "ULFM initial rank", 0, MAX_ENDPOINTS - 1)
        _digest(self.incarnation_sha256, "ULFM endpoint incarnation")
        _identity(self.host_id, "ULFM host")

    def as_dict(self) -> dict[str, Any]:
        return {
            "endpoint_id": self.endpoint_id,
            "initial_rank": self.initial_rank,
            "incarnation_sha256": self.incarnation_sha256,
            "host_id": self.host_id,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "EndpointIdentity":
        fields = {"endpoint_id", "initial_rank", "incarnation_sha256", "host_id"}
        if not isinstance(raw, Mapping) or set(raw) != fields:
            raise UlfmRecoveryError("ULFM endpoint identity shape changed")
        return cls(**{field: raw[field] for field in fields})


@dataclass(frozen=True)
class RecoveryShard:
    shard_id: str
    owner_endpoint: str
    checkpoint_sha256: str
    cursor: int = 0

    def __post_init__(self) -> None:
        _identity(self.shard_id, "ULFM shard")
        _identity(self.owner_endpoint, "ULFM shard owner")
        _digest(self.checkpoint_sha256, "ULFM shard checkpoint")
        _integer(self.cursor, "ULFM shard cursor", 0, (1 << 63) - 1)


def _generation_token(
    run_id: str,
    generation: int,
    members: Mapping[str, Mapping[str, Any]],
) -> str:
    return content_digest(
        {
            "protocol": ULFM_RECOVERY_PROTOCOL,
            "run_id": run_id,
            "generation": generation,
            "members": {key: dict(value) for key, value in sorted(members.items())},
        }
    )


def _shard_token(
    run_id: str,
    generation: int,
    shard_id: str,
    owner_endpoint: str,
) -> str:
    return content_digest(
        {
            "protocol": ULFM_RECOVERY_PROTOCOL,
            "run_id": run_id,
            "generation": generation,
            "shard_id": shard_id,
            "owner_endpoint": owner_endpoint,
        }
    )


def _lease_token(shard_token: str, work_id: str, ordinal: int) -> str:
    return content_digest(
        {
            "protocol": ULFM_RECOVERY_PROTOCOL,
            "shard_token": shard_token,
            "work_id": work_id,
            "lease_ordinal": ordinal,
        }
    )


def build_endpoint_attestation(
    *,
    run_id: str,
    base_generation: int,
    base_generation_token: str,
    endpoint: EndpointIdentity,
    old_rank: int,
    new_rank: int,
) -> dict[str, Any]:
    """Build one content-addressed stable-identity membership attestation."""
    run = _identity(run_id, "ULFM run")
    generation = _integer(
        base_generation, "ULFM attestation generation", 0, (1 << 63) - 2
    )
    token = _digest(base_generation_token, "ULFM base generation token")
    old = _integer(old_rank, "ULFM old rank", 0, MAX_ENDPOINTS - 1)
    new = _integer(new_rank, "ULFM new rank", 0, MAX_ENDPOINTS - 1)
    body = {
        "schema": ULFM_ATTESTATION_SCHEMA,
        "protocol": ULFM_RECOVERY_PROTOCOL,
        "run_id": run,
        "base_generation": generation,
        "base_generation_token": token,
        "endpoint_id": endpoint.endpoint_id,
        "old_rank": old,
        "new_rank": new,
        "incarnation_sha256": endpoint.incarnation_sha256,
        "host_id": endpoint.host_id,
    }
    body["attestation_sha256"] = content_digest(body)
    return body


def verify_endpoint_attestation(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise UlfmRecoveryError("ULFM endpoint attestation must be an object")
    body = dict(raw)
    supplied = _digest(
        body.pop("attestation_sha256", ""), "ULFM endpoint attestation"
    )
    if content_digest(body) != supplied:
        raise UlfmRecoveryError("ULFM endpoint attestation identity changed")
    expected = {
        "schema",
        "protocol",
        "run_id",
        "base_generation",
        "base_generation_token",
        "endpoint_id",
        "old_rank",
        "new_rank",
        "incarnation_sha256",
        "host_id",
    }
    if set(body) != expected:
        raise UlfmRecoveryError("ULFM endpoint attestation shape changed")
    if (
        body["schema"] != ULFM_ATTESTATION_SCHEMA
        or body["protocol"] != ULFM_RECOVERY_PROTOCOL
    ):
        raise UlfmRecoveryError("ULFM endpoint attestation scope changed")
    _identity(body["run_id"], "ULFM run")
    _integer(body["base_generation"], "ULFM base generation", 0, (1 << 63) - 2)
    _digest(body["base_generation_token"], "ULFM base generation token")
    _identity(body["endpoint_id"], "ULFM endpoint")
    _integer(body["old_rank"], "ULFM old rank", 0, MAX_ENDPOINTS - 1)
    _integer(body["new_rank"], "ULFM new rank", 0, MAX_ENDPOINTS - 1)
    _digest(body["incarnation_sha256"], "ULFM endpoint incarnation")
    _identity(body["host_id"], "ULFM host")
    body["attestation_sha256"] = supplied
    return body


def _normalize_attestations(
    raw_attestations: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
    generation: int,
    generation_token: str,
    members: Mapping[str, Mapping[str, Any]],
    suspected_failures: set[str],
    maximum: int,
) -> tuple[dict[str, dict[str, Any]], tuple[str, ...]]:
    attestations = _bounded_sequence(
        raw_attestations, "ULFM survivor attestations", maximum
    )
    if not attestations:
        raise UlfmRecoveryError("ULFM repair has no surviving endpoint")
    normalized: dict[str, dict[str, Any]] = {}
    new_ranks: set[int] = set()
    hashes: set[str] = set()
    for raw in attestations:
        attestation = verify_endpoint_attestation(raw)
        endpoint_id = attestation["endpoint_id"]
        if endpoint_id in normalized:
            raise UlfmRecoveryError("duplicate ULFM endpoint attestation")
        if attestation["attestation_sha256"] in hashes:
            raise UlfmRecoveryError("duplicate ULFM attestation identity")
        hashes.add(attestation["attestation_sha256"])
        member = members.get(endpoint_id)
        if member is None:
            raise UlfmRecoveryError("ULFM attestation endpoint is outside the generation")
        if (
            attestation["run_id"] != run_id
            or attestation["base_generation"] != generation
            or attestation["base_generation_token"] != generation_token
            or attestation["old_rank"] != member["rank"]
            or attestation["incarnation_sha256"] != member["incarnation_sha256"]
            or attestation["host_id"] != member["host_id"]
        ):
            raise UlfmRecoveryError("ULFM survivor attestation fence changed")
        new_rank = attestation["new_rank"]
        if new_rank in new_ranks:
            raise UlfmRecoveryError("duplicate repaired communicator rank")
        new_ranks.add(new_rank)
        normalized[endpoint_id] = attestation
    if new_ranks != set(range(len(normalized))):
        raise UlfmRecoveryError("repaired communicator ranks are not dense")
    failed = tuple(sorted(set(members) - set(normalized)))
    if not suspected_failures <= set(failed):
        raise UlfmRecoveryError("suspected failed endpoint reappeared after shrink")
    if not failed:
        raise UlfmRecoveryError("ULFM recovery did not remove an endpoint")
    return normalized, failed


def _rendezvous_owner(
    run_id: str,
    generation: int,
    shard_id: str,
    endpoints: Sequence[str],
) -> str:
    if not endpoints:
        raise UlfmRecoveryError("cannot assign a shard without survivors")
    return max(
        endpoints,
        key=lambda endpoint: (
            hashlib.sha256(
                (
                    f"{ULFM_RECOVERY_PROTOCOL}\0{run_id}\0{generation}\0"
                    f"{shard_id}\0{endpoint}"
                ).encode("utf-8")
            ).digest(),
            endpoint,
        ),
    )


class UlfmRecoveryController:
    """Persistent state machine for communicator repair and shard replay."""

    def __init__(
        self,
        run_id: str,
        endpoints: Sequence[EndpointIdentity],
        shards: Sequence[RecoveryShard],
        policy: UlfmRecoveryPolicy | None = None,
    ) -> None:
        self.run_id = _identity(run_id, "ULFM run")
        self.policy = policy or UlfmRecoveryPolicy()
        endpoint_list = _bounded_sequence(
            endpoints, "ULFM endpoint inventory", self.policy.max_endpoints
        )
        if len(endpoint_list) < 2 or any(
            not isinstance(endpoint, EndpointIdentity) for endpoint in endpoint_list
        ):
            raise UlfmRecoveryError("ULFM endpoint inventory is invalid")
        ids = [endpoint.endpoint_id for endpoint in endpoint_list]
        ranks = [endpoint.initial_rank for endpoint in endpoint_list]
        if len(set(ids)) != len(ids) or set(ranks) != set(range(len(ranks))):
            raise UlfmRecoveryError("ULFM endpoint identities/ranks are not unique and dense")
        shard_list = _bounded_sequence(shards, "ULFM shard inventory", self.policy.max_shards)
        if not shard_list or any(
            not isinstance(shard, RecoveryShard) for shard in shard_list
        ):
            raise UlfmRecoveryError("ULFM shard inventory is invalid")
        shard_ids = [shard.shard_id for shard in shard_list]
        if len(set(shard_ids)) != len(shard_ids):
            raise UlfmRecoveryError("ULFM shard inventory has duplicates")
        if any(shard.owner_endpoint not in set(ids) for shard in shard_list):
            raise UlfmRecoveryError("ULFM shard owner is outside the membership")

        self._generation = 0
        self._members = {
            endpoint.endpoint_id: {
                "rank": endpoint.initial_rank,
                "incarnation_sha256": endpoint.incarnation_sha256,
                "host_id": endpoint.host_id,
            }
            for endpoint in endpoint_list
        }
        self._generation_token = _generation_token(
            self.run_id, self._generation, self._members
        )
        self._shards: dict[str, dict[str, Any]] = {}
        for shard in sorted(shard_list, key=lambda item: item.shard_id):
            self._shards[shard.shard_id] = {
                "owner_endpoint": shard.owner_endpoint,
                "generation": 0,
                "shard_token": _shard_token(
                    self.run_id, 0, shard.shard_id, shard.owner_endpoint
                ),
                "checkpoint_sha256": shard.checkpoint_sha256,
                "cursor": shard.cursor,
                "active_work_id": "",
                "active_lease_token": "",
                "lease_ordinal": 0,
                "completed": 0,
                "completion_chain_sha256": hashlib.sha256(b"").hexdigest(),
            }
        self._recovery_queue: list[dict[str, Any]] = []
        self._pending: dict[str, Any] | None = None
        self._recovery_count = 0

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def generation_token(self) -> str:
        return self._generation_token

    @property
    def pending(self) -> bool:
        return self._pending is not None

    def _fenced_shard(
        self,
        shard_id: Any,
        endpoint_id: Any,
        generation: Any,
        generation_token: Any,
        shard_token: Any,
    ) -> tuple[str, dict[str, Any]]:
        shard_name = _identity(shard_id, "ULFM shard")
        endpoint = _identity(endpoint_id, "ULFM endpoint")
        shard = self._shards.get(shard_name)
        if shard is None:
            raise UlfmRecoveryError("ULFM shard is outside the run")
        supplied_generation = _integer(
            generation, "ULFM message generation", 0, (1 << 63) - 1
        )
        supplied_generation_token = _digest(
            generation_token, "ULFM message generation token"
        )
        supplied_shard_token = _digest(shard_token, "ULFM message shard token")
        if (
            supplied_generation != self._generation
            or supplied_generation_token != self._generation_token
            or endpoint != shard["owner_endpoint"]
            or supplied_shard_token != shard["shard_token"]
        ):
            raise UlfmRecoveryError("stale ULFM communicator/shard fence")
        if self._pending is not None:
            raise UlfmRecoveryError("ULFM recovery is pending")
        return shard_name, shard

    def shard_permission(self, shard_id: str) -> dict[str, Any]:
        shard_name = _identity(shard_id, "ULFM shard")
        shard = self._shards.get(shard_name)
        if shard is None:
            raise UlfmRecoveryError("ULFM shard is outside the run")
        body = {
            "schema": "symcc-ulfm-shard-permission-v1",
            "protocol": ULFM_RECOVERY_PROTOCOL,
            "run_id": self.run_id,
            "generation": self._generation,
            "generation_token": self._generation_token,
            "shard_id": shard_name,
            **dict(shard),
        }
        body["permission_sha256"] = content_digest(body)
        return body

    def _attach_work(
        self,
        shard_id: str,
        endpoint_id: str,
        generation: int,
        generation_token: str,
        shard_token: str,
        work_id: str,
        *,
        allow_queued_recovery: bool,
    ) -> dict[str, Any]:
        shard_name, shard = self._fenced_shard(
            shard_id, endpoint_id, generation, generation_token, shard_token
        )
        work = _identity(work_id, "ULFM work")
        if shard["active_work_id"]:
            raise UlfmRecoveryError("ULFM shard already owns active work")
        if not allow_queued_recovery and any(
            item["shard_id"] == shard_name for item in self._recovery_queue
        ):
            raise UlfmRecoveryError("ULFM shard has priority recovery work")
        shard["lease_ordinal"] += 1
        token = _lease_token(shard["shard_token"], work, shard["lease_ordinal"])
        shard["active_work_id"] = work
        shard["active_lease_token"] = token
        event = {
            "schema": "symcc-ulfm-work-lease-v1",
            "protocol": ULFM_RECOVERY_PROTOCOL,
            "run_id": self.run_id,
            "generation": self._generation,
            "generation_token": self._generation_token,
            "shard_id": shard_name,
            "owner_endpoint": endpoint_id,
            "shard_token": shard["shard_token"],
            "work_id": work,
            "lease_ordinal": shard["lease_ordinal"],
            "lease_token": token,
            "checkpoint_sha256": shard["checkpoint_sha256"],
            "cursor": shard["cursor"],
        }
        event["lease_sha256"] = content_digest(event)
        return event

    def attach_work(
        self,
        shard_id: str,
        endpoint_id: str,
        generation: int,
        generation_token: str,
        shard_token: str,
        work_id: str,
    ) -> dict[str, Any]:
        return self._attach_work(
            shard_id,
            endpoint_id,
            generation,
            generation_token,
            shard_token,
            work_id,
            allow_queued_recovery=False,
        )

    def checkpoint_work(
        self,
        shard_id: str,
        endpoint_id: str,
        generation: int,
        generation_token: str,
        shard_token: str,
        lease_token: str,
        checkpoint_sha256: str,
        cursor: int,
    ) -> dict[str, Any]:
        shard_name, shard = self._fenced_shard(
            shard_id, endpoint_id, generation, generation_token, shard_token
        )
        lease = _digest(lease_token, "ULFM work lease")
        checkpoint = _digest(checkpoint_sha256, "ULFM shard checkpoint")
        new_cursor = _integer(cursor, "ULFM shard cursor", 0, (1 << 63) - 1)
        if not shard["active_work_id"] or lease != shard["active_lease_token"]:
            raise UlfmRecoveryError("stale ULFM work lease")
        if new_cursor <= shard["cursor"]:
            raise UlfmRecoveryError("ULFM shard cursor is not monotonic")
        shard["checkpoint_sha256"] = checkpoint
        shard["cursor"] = new_cursor
        event = {
            "schema": "symcc-ulfm-work-checkpoint-v1",
            "protocol": ULFM_RECOVERY_PROTOCOL,
            "run_id": self.run_id,
            "generation": self._generation,
            "shard_id": shard_name,
            "lease_token": lease,
            "checkpoint_sha256": checkpoint,
            "cursor": new_cursor,
        }
        event["checkpoint_event_sha256"] = content_digest(event)
        return event

    def finish_work(
        self,
        shard_id: str,
        endpoint_id: str,
        generation: int,
        generation_token: str,
        shard_token: str,
        lease_token: str,
        durable_proof_sha256: str,
    ) -> dict[str, Any]:
        shard_name, shard = self._fenced_shard(
            shard_id, endpoint_id, generation, generation_token, shard_token
        )
        lease = _digest(lease_token, "ULFM work lease")
        proof = _digest(durable_proof_sha256, "ULFM durable proof")
        if not shard["active_work_id"] or lease != shard["active_lease_token"]:
            raise UlfmRecoveryError("stale ULFM work lease")
        work = shard["active_work_id"]
        previous_chain = shard["completion_chain_sha256"]
        shard["completion_chain_sha256"] = content_digest(
            {
                "previous": previous_chain,
                "work_id": work,
                "lease_token": lease,
                "durable_proof_sha256": proof,
                "checkpoint_sha256": shard["checkpoint_sha256"],
                "cursor": shard["cursor"],
            }
        )
        shard["completed"] += 1
        shard["active_work_id"] = ""
        shard["active_lease_token"] = ""
        event = {
            "schema": "symcc-ulfm-work-completion-v1",
            "protocol": ULFM_RECOVERY_PROTOCOL,
            "run_id": self.run_id,
            "generation": self._generation,
            "shard_id": shard_name,
            "work_id": work,
            "lease_token": lease,
            "durable_proof_sha256": proof,
            "completion_chain_sha256": shard["completion_chain_sha256"],
            "completed": shard["completed"],
        }
        event["completion_sha256"] = content_digest(event)
        return event

    def cancel_work(
        self,
        shard_id: str,
        endpoint_id: str,
        generation: int,
        generation_token: str,
        shard_token: str,
        lease_token: str,
        reason: str,
    ) -> dict[str, Any]:
        """Retire an uncommitted lease without recording a completion.

        Cancellation is only valid while the exact generation, shard and work
        lease are current.  It is used for locally rejected results and
        unambiguous pre-delivery failures; communicator failures instead go
        through ``prepare_recovery`` so ambiguous work is conservatively
        replayed.
        """
        shard_name, shard = self._fenced_shard(
            shard_id, endpoint_id, generation, generation_token, shard_token
        )
        lease = _digest(lease_token, "ULFM work lease")
        cancellation_reason = _identity(reason, "ULFM cancellation reason")
        if not shard["active_work_id"] or lease != shard["active_lease_token"]:
            raise UlfmRecoveryError("stale ULFM work lease")
        work = shard["active_work_id"]
        shard["active_work_id"] = ""
        shard["active_lease_token"] = ""
        event = {
            "schema": "symcc-ulfm-work-cancellation-v1",
            "protocol": ULFM_RECOVERY_PROTOCOL,
            "run_id": self.run_id,
            "generation": self._generation,
            "generation_token": self._generation_token,
            "shard_id": shard_name,
            "owner_endpoint": endpoint_id,
            "shard_token": shard["shard_token"],
            "work_id": work,
            "lease_token": lease,
            "reason": cancellation_reason,
        }
        event["cancellation_sha256"] = content_digest(event)
        return event

    def prepare_recovery(
        self,
        suspected_failed_endpoints: Sequence[str],
        *,
        generation: int,
        generation_token: str,
    ) -> dict[str, Any]:
        if self._pending is not None:
            raise UlfmRecoveryError("ULFM recovery is already pending")
        if generation != self._generation or (
            _digest(generation_token, "ULFM recovery generation token")
            != self._generation_token
        ):
            raise UlfmRecoveryError("stale ULFM recovery generation fence")
        raw = _bounded_sequence(
            suspected_failed_endpoints,
            "ULFM suspected failures",
            self.policy.max_endpoints,
        )
        failures = sorted({_identity(value, "ULFM failed endpoint") for value in raw})
        if len(failures) != len(raw):
            raise UlfmRecoveryError("ULFM suspected failures contain duplicates")
        if not failures or not set(failures) < set(self._members):
            raise UlfmRecoveryError("ULFM suspected failure set is invalid")
        in_flight = [
            {
                "shard_id": shard_id,
                "owner_endpoint": shard["owner_endpoint"],
                "work_id": shard["active_work_id"],
                "lease_token": shard["active_lease_token"],
                "checkpoint_sha256": shard["checkpoint_sha256"],
                "cursor": shard["cursor"],
            }
            for shard_id, shard in sorted(self._shards.items())
            if shard["active_work_id"]
        ]
        if len(in_flight) + len(self._recovery_queue) > self.policy.max_recovery_queue:
            raise UlfmRecoveryError("ULFM recovery queue budget is exhausted")
        plan = {
            "schema": ULFM_PLAN_SCHEMA,
            "protocol": ULFM_RECOVERY_PROTOCOL,
            "run_id": self.run_id,
            "policy_sha256": self.policy.sha256,
            "base_generation": self._generation,
            "target_generation": self._generation + 1,
            "base_generation_token": self._generation_token,
            "base_members": {
                endpoint: dict(member)
                for endpoint, member in sorted(self._members.items())
            },
            "suspected_failed_endpoints": failures,
            "in_flight": in_flight,
            "preexisting_recovery_queue": [
                dict(item) for item in self._recovery_queue
            ],
        }
        plan["plan_sha256"] = content_digest(plan)
        verified = verify_recovery_plan(plan, policy=self.policy)
        self._pending = _clone(verified)
        return _clone(verified)

    def commit_recovery(
        self,
        plan_sha256: str,
        attestations: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        if self._pending is None:
            raise UlfmRecoveryError("no ULFM recovery is pending")
        plan_digest = _digest(plan_sha256, "ULFM recovery plan")
        if plan_digest != self._pending["plan_sha256"]:
            raise UlfmRecoveryError("stale ULFM recovery plan")
        normalized, failed = _normalize_attestations(
            attestations,
            run_id=self.run_id,
            generation=self._generation,
            generation_token=self._generation_token,
            members=self._members,
            suspected_failures=set(self._pending["suspected_failed_endpoints"]),
            maximum=self.policy.max_endpoints,
        )
        target_generation = self._generation + 1
        survivors = tuple(sorted(normalized))
        in_flight_before = len(self._pending["in_flight"])
        queued_before = len(self._recovery_queue)
        new_members = {
            endpoint: {
                "rank": normalized[endpoint]["new_rank"],
                "incarnation_sha256": normalized[endpoint]["incarnation_sha256"],
                "host_id": normalized[endpoint]["host_id"],
            }
            for endpoint in survivors
        }
        requeued = [dict(item) for item in self._recovery_queue]
        requeued.extend(dict(item) for item in self._pending["in_flight"])
        if len(requeued) > self.policy.max_recovery_queue:
            raise UlfmRecoveryError("ULFM recovery queue budget is exhausted")
        if len({item["shard_id"] for item in requeued}) != len(requeued):
            raise UlfmRecoveryError("ULFM recovery would duplicate a shard lease")

        reassignments: dict[str, dict[str, str]] = {}
        for shard_id, shard in sorted(self._shards.items()):
            old_owner = shard["owner_endpoint"]
            new_owner = (
                old_owner
                if old_owner in new_members
                else _rendezvous_owner(
                    self.run_id, target_generation, shard_id, survivors
                )
            )
            if old_owner != new_owner:
                reassignments[shard_id] = {
                    "old_owner": old_owner,
                    "new_owner": new_owner,
                }
            shard["owner_endpoint"] = new_owner
            shard["generation"] = target_generation
            shard["shard_token"] = _shard_token(
                self.run_id, target_generation, shard_id, new_owner
            )
            shard["active_work_id"] = ""
            shard["active_lease_token"] = ""

        old_members = {
            endpoint: dict(member) for endpoint, member in sorted(self._members.items())
        }
        self._members = new_members
        self._generation = target_generation
        self._generation_token = _generation_token(
            self.run_id, self._generation, self._members
        )
        self._recovery_queue = sorted(
            requeued, key=lambda item: (item["shard_id"], item["work_id"])
        )
        self._pending = None
        self._recovery_count += 1
        post_state = self.snapshot()
        receipt = {
            "schema": ULFM_RECEIPT_SCHEMA,
            "protocol": ULFM_RECOVERY_PROTOCOL,
            "run_id": self.run_id,
            "policy_sha256": self.policy.sha256,
            "plan_sha256": plan_digest,
            "base_generation": target_generation - 1,
            "target_generation": target_generation,
            "new_generation_token": self._generation_token,
            "old_members": old_members,
            "new_members": {
                endpoint: dict(member)
                for endpoint, member in sorted(new_members.items())
            },
            "failed_endpoints": list(failed),
            "survivor_attestations": [
                normalized[endpoint] for endpoint in sorted(normalized)
            ],
            "reassignments": reassignments,
            "shard_assignment_sha256": content_digest(
                {
                    shard_id: {
                        "owner_endpoint": shard["owner_endpoint"],
                        "shard_token": shard["shard_token"],
                    }
                    for shard_id, shard in sorted(self._shards.items())
                }
            ),
            "requeued_work": [dict(item) for item in self._recovery_queue],
            "conservation": {
                "old_endpoints": len(old_members),
                "survivors": len(new_members),
                "failed": len(failed),
                "shards_before": len(self._shards),
                "shards_after": len(self._shards),
                "in_flight_before": in_flight_before,
                "recovery_queue_before": queued_before,
                "recovery_queue_after": len(self._recovery_queue),
            },
            "post_state_sha256": post_state["snapshot_sha256"],
        }
        receipt["receipt_sha256"] = content_digest(receipt)
        return receipt

    def claim_recovery(self, shard_id: str) -> dict[str, Any]:
        shard_name = _identity(shard_id, "ULFM shard")
        if self._pending is not None:
            raise UlfmRecoveryError("ULFM recovery is pending")
        index = next(
            (
                index
                for index, item in enumerate(self._recovery_queue)
                if item["shard_id"] == shard_name
            ),
            -1,
        )
        if index < 0:
            raise UlfmRecoveryError("ULFM shard has no recovery work")
        item = self._recovery_queue[index]
        permission = self.shard_permission(shard_name)
        lease = self._attach_work(
            shard_name,
            permission["owner_endpoint"],
            permission["generation"],
            permission["generation_token"],
            permission["shard_token"],
            item["work_id"],
            allow_queued_recovery=True,
        )
        self._recovery_queue.pop(index)
        return {
            "schema": "symcc-ulfm-recovery-claim-v1",
            "protocol": ULFM_RECOVERY_PROTOCOL,
            "replayed_from": item,
            "new_lease": lease,
        }

    def classify_message(self, raw: Mapping[str, Any]) -> str:
        """Classify a transport envelope without mutating recovery state."""
        if not isinstance(raw, Mapping):
            return "malformed"
        expected = {
            "run_id",
            "generation",
            "generation_token",
            "endpoint_id",
            "shard_id",
            "shard_token",
        }
        if not expected <= set(raw):
            return "malformed"
        try:
            run_id = _identity(raw["run_id"], "ULFM run")
            generation = _integer(
                raw["generation"], "ULFM message generation", 0, (1 << 63) - 1
            )
            generation_token = _digest(
                raw["generation_token"], "ULFM message generation token"
            )
            endpoint = _identity(raw["endpoint_id"], "ULFM endpoint")
            shard_id = _identity(raw["shard_id"], "ULFM shard")
            shard_token = _digest(raw["shard_token"], "ULFM shard token")
        except UlfmRecoveryError:
            return "malformed"
        shard = self._shards.get(shard_id)
        if shard is None or endpoint not in self._members:
            return "unowned"
        if (
            run_id != self.run_id
            or generation != self._generation
            or generation_token != self._generation_token
            or endpoint != shard["owner_endpoint"]
            or shard_token != shard["shard_token"]
        ):
            return "stale"
        return "current"

    def classify_work_message(self, raw: Mapping[str, Any]) -> str:
        """Classify an exact result fence, including work and lease identity."""
        classification = self.classify_message(raw)
        if classification != "current":
            return classification
        try:
            work_id = _identity(raw.get("work_id"), "ULFM work")
            lease_token = _digest(raw.get("lease_token"), "ULFM work lease")
        except UlfmRecoveryError:
            return "malformed"
        shard = self._shards[str(raw["shard_id"])]
        if (
            work_id != shard["active_work_id"]
            or lease_token != shard["active_lease_token"]
        ):
            return "stale"
        return "current"

    def snapshot(self) -> dict[str, Any]:
        body = {
            "schema": ULFM_SNAPSHOT_SCHEMA,
            "protocol": ULFM_RECOVERY_PROTOCOL,
            "run_id": self.run_id,
            "policy": self.policy.as_dict(),
            "policy_sha256": self.policy.sha256,
            "generation": self._generation,
            "generation_token": self._generation_token,
            "members": {
                endpoint: dict(member)
                for endpoint, member in sorted(self._members.items())
            },
            "shards": {
                shard_id: dict(shard)
                for shard_id, shard in sorted(self._shards.items())
            },
            "recovery_queue": [dict(item) for item in self._recovery_queue],
            "pending_recovery": None if self._pending is None else _clone(self._pending),
            "recovery_count": self._recovery_count,
        }
        body["snapshot_sha256"] = content_digest(body)
        return body

    @classmethod
    def from_snapshot(cls, raw: Mapping[str, Any]) -> "UlfmRecoveryController":
        snapshot = verify_recovery_snapshot(raw)
        policy = UlfmRecoveryPolicy.from_sealed(snapshot["policy"])
        endpoints = [
            EndpointIdentity(
                endpoint_id=endpoint,
                initial_rank=member["rank"],
                incarnation_sha256=member["incarnation_sha256"],
                host_id=member["host_id"],
            )
            for endpoint, member in sorted(
                snapshot["members"].items(), key=lambda item: item[1]["rank"]
            )
        ]
        shards = [
            RecoveryShard(
                shard_id=shard_id,
                owner_endpoint=shard["owner_endpoint"],
                checkpoint_sha256=shard["checkpoint_sha256"],
                cursor=shard["cursor"],
            )
            for shard_id, shard in sorted(snapshot["shards"].items())
        ]
        controller = cls(snapshot["run_id"], endpoints, shards, policy)
        controller._generation = snapshot["generation"]
        controller._generation_token = snapshot["generation_token"]
        controller._members = {
            endpoint: dict(member) for endpoint, member in snapshot["members"].items()
        }
        controller._shards = {
            shard_id: dict(shard) for shard_id, shard in snapshot["shards"].items()
        }
        controller._recovery_queue = [
            dict(item) for item in snapshot["recovery_queue"]
        ]
        pending = snapshot["pending_recovery"]
        controller._pending = None if pending is None else _clone(pending)
        controller._recovery_count = snapshot["recovery_count"]
        return controller


def build_work_fence(lease: Mapping[str, Any]) -> dict[str, Any]:
    """Create the minimal content-addressed fence echoed by a worker result."""
    if not isinstance(lease, Mapping):
        raise UlfmRecoveryError("ULFM work lease must be an object")
    required = {
        "run_id",
        "generation",
        "generation_token",
        "shard_id",
        "owner_endpoint",
        "shard_token",
        "work_id",
        "lease_token",
    }
    if not required <= set(lease):
        raise UlfmRecoveryError("ULFM work lease is incomplete")
    body = {
        "schema": ULFM_WORK_FENCE_SCHEMA,
        "protocol": ULFM_RECOVERY_PROTOCOL,
        "run_id": _identity(lease["run_id"], "ULFM run"),
        "generation": _integer(
            lease["generation"], "ULFM message generation", 0, (1 << 63) - 1
        ),
        "generation_token": _digest(
            lease["generation_token"], "ULFM message generation token"
        ),
        "endpoint_id": _identity(lease["owner_endpoint"], "ULFM endpoint"),
        "shard_id": _identity(lease["shard_id"], "ULFM shard"),
        "shard_token": _digest(lease["shard_token"], "ULFM shard token"),
        "work_id": _identity(lease["work_id"], "ULFM work"),
        "lease_token": _digest(lease["lease_token"], "ULFM work lease"),
    }
    body["fence_sha256"] = content_digest(body)
    return body


def verify_work_fence(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Verify a worker-echoed fence without consulting controller state."""
    if not isinstance(raw, Mapping):
        raise UlfmRecoveryError("ULFM work fence must be an object")
    body = dict(raw)
    supplied = _digest(body.pop("fence_sha256", ""), "ULFM work fence")
    expected = {
        "schema",
        "protocol",
        "run_id",
        "generation",
        "generation_token",
        "endpoint_id",
        "shard_id",
        "shard_token",
        "work_id",
        "lease_token",
    }
    if set(body) != expected or content_digest(body) != supplied:
        raise UlfmRecoveryError("ULFM work fence identity changed")
    if (
        body["schema"] != ULFM_WORK_FENCE_SCHEMA
        or body["protocol"] != ULFM_RECOVERY_PROTOCOL
    ):
        raise UlfmRecoveryError("ULFM work fence scope changed")
    normalized = build_work_fence(
        {
            **body,
            "owner_endpoint": body["endpoint_id"],
        }
    )
    if normalized != dict(raw):
        raise UlfmRecoveryError("ULFM work fence is not canonical")
    return normalized


def build_work_envelope(payload: Mapping[str, Any], lease: Mapping[str, Any]) -> dict[str, Any]:
    """Bind one application work payload to an exact communicator lease."""
    if not isinstance(payload, Mapping):
        raise UlfmRecoveryError("ULFM work payload must be an object")
    normalized_payload = _clone(dict(payload))
    body = {
        "schema": ULFM_WORK_ENVELOPE_SCHEMA,
        "protocol": ULFM_RECOVERY_PROTOCOL,
        "payload": normalized_payload,
        "fence": build_work_fence(lease),
    }
    body["envelope_sha256"] = content_digest(body)
    return body


def verify_work_envelope(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Strictly verify a work envelope before a worker consumes its payload."""
    if not isinstance(raw, Mapping):
        raise UlfmRecoveryError("ULFM work envelope must be an object")
    body = dict(raw)
    supplied = _digest(body.pop("envelope_sha256", ""), "ULFM work envelope")
    if set(body) != {"schema", "protocol", "payload", "fence"}:
        raise UlfmRecoveryError("ULFM work envelope shape changed")
    if (
        body["schema"] != ULFM_WORK_ENVELOPE_SCHEMA
        or body["protocol"] != ULFM_RECOVERY_PROTOCOL
        or not isinstance(body["payload"], Mapping)
    ):
        raise UlfmRecoveryError("ULFM work envelope scope changed")
    normalized = {
        "schema": ULFM_WORK_ENVELOPE_SCHEMA,
        "protocol": ULFM_RECOVERY_PROTOCOL,
        "payload": _clone(dict(body["payload"])),
        "fence": verify_work_fence(body["fence"]),
    }
    if content_digest(normalized) != supplied:
        raise UlfmRecoveryError("ULFM work envelope identity changed")
    normalized["envelope_sha256"] = supplied
    if normalized != dict(raw):
        raise UlfmRecoveryError("ULFM work envelope is not canonical")
    return normalized


class DurableUlfmCoordinator:
    """Join the pure recovery state machine to durable QueryStore checkpoints."""

    def __init__(self, controller: UlfmRecoveryController, store: Any) -> None:
        if not isinstance(controller, UlfmRecoveryController):
            raise UlfmRecoveryError("invalid durable ULFM controller")
        for method in (
            "load_ulfm_recovery_snapshot",
            "load_ulfm_recovery_state",
            "commit_ulfm_recovery_snapshot",
        ):
            if not callable(getattr(store, method, None)):
                raise UlfmRecoveryError("invalid durable ULFM snapshot store")
        self.controller = controller
        self.store = store
        existing_state = store.load_ulfm_recovery_state(
            controller.run_id, controller.policy
        )
        current = controller.snapshot()
        if existing_state is None:
            result = store.commit_ulfm_recovery_snapshot(
                controller.run_id,
                controller.policy,
                current,
                state_ordinal=0,
            )
            if result not in {"advanced", "idempotent"}:
                raise UlfmRecoveryError("initial ULFM checkpoint was not persisted")
            self._state_ordinal = 0
        else:
            existing, state_ordinal = existing_state
            if existing["snapshot_sha256"] != current["snapshot_sha256"]:
                raise UlfmRecoveryError(
                    "durable ULFM state differs from controller state"
                )
            self._state_ordinal = state_ordinal

    @classmethod
    def restore(
        cls,
        run_id: str,
        policy: UlfmRecoveryPolicy,
        store: Any,
    ) -> "DurableUlfmCoordinator":
        snapshot = store.load_ulfm_recovery_snapshot(run_id, policy)
        if snapshot is None:
            raise UlfmRecoveryError("durable ULFM state does not exist")
        return cls(UlfmRecoveryController.from_snapshot(snapshot), store)

    def dispatch(self, shard_id: str, work_id: str) -> dict[str, Any]:
        before = self.controller.snapshot()
        snapshot = before
        queued_for_work = [
            item
            for item in snapshot["recovery_queue"]
            if item["work_id"] == work_id
        ]
        if queued_for_work:
            if len(queued_for_work) != 1:
                raise UlfmRecoveryError("ULFM recovery work identity is ambiguous")
            lease = self.controller.claim_recovery(
                queued_for_work[0]["shard_id"]
            )["new_lease"]
        else:
            queued_for_shard = [
                item
                for item in snapshot["recovery_queue"]
                if item["shard_id"] == shard_id
            ]
            if queued_for_shard:
                raise UlfmRecoveryError("ULFM shard has priority recovery work")
            permission = self.controller.shard_permission(shard_id)
            lease = self.controller.attach_work(
                shard_id,
                permission["owner_endpoint"],
                permission["generation"],
                permission["generation_token"],
                permission["shard_token"],
                work_id,
            )
        self._persist_transition(before, self.controller.snapshot())
        return lease

    def classify_result(self, raw_fence: Mapping[str, Any]) -> str:
        try:
            fence = verify_work_fence(raw_fence)
        except UlfmRecoveryError:
            return "malformed"
        return self.controller.classify_work_message(fence)

    def _persist_transition(
        self,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
        *,
        receipt: Mapping[str, Any] | None = None,
    ) -> None:
        """Commit one controller transition or reconcile an ambiguous write."""
        try:
            target_ordinal = self._state_ordinal + 1
            result = self.store.commit_ulfm_recovery_snapshot(
                self.controller.run_id,
                self.controller.policy,
                after,
                receipt=receipt,
                state_ordinal=target_ordinal,
            )
        except Exception:
            try:
                observed_state = self.store.load_ulfm_recovery_state(
                    self.controller.run_id, self.controller.policy
                )
            except Exception:
                observed_state = None
            if (
                observed_state is not None
                and observed_state[0]["snapshot_sha256"]
                == after["snapshot_sha256"]
                and observed_state[1] == target_ordinal
            ):
                self._state_ordinal = target_ordinal
                return
            self.controller = UlfmRecoveryController.from_snapshot(before)
            raise
        if result not in {"advanced", "idempotent"}:
            self.controller = UlfmRecoveryController.from_snapshot(before)
            raise UlfmRecoveryError("ULFM recovery checkpoint was not persisted")
        self._state_ordinal = target_ordinal

    def finish(self, raw_fence: Mapping[str, Any], proof_sha256: str) -> dict[str, Any]:
        fence = verify_work_fence(raw_fence)
        if self.controller.classify_work_message(fence) != "current":
            raise UlfmRecoveryError("stale ULFM result fence")
        before = self.controller.snapshot()
        try:
            event = self.controller.finish_work(
                fence["shard_id"],
                fence["endpoint_id"],
                fence["generation"],
                fence["generation_token"],
                fence["shard_token"],
                fence["lease_token"],
                proof_sha256,
            )
        except Exception:
            self.controller = UlfmRecoveryController.from_snapshot(before)
            raise
        self._persist_transition(before, self.controller.snapshot())
        return event

    def cancel(self, raw_fence: Mapping[str, Any], reason: str) -> dict[str, Any]:
        fence = verify_work_fence(raw_fence)
        if self.controller.classify_work_message(fence) != "current":
            raise UlfmRecoveryError("stale ULFM result fence")
        before = self.controller.snapshot()
        try:
            event = self.controller.cancel_work(
                fence["shard_id"],
                fence["endpoint_id"],
                fence["generation"],
                fence["generation_token"],
                fence["shard_token"],
                fence["lease_token"],
                reason,
            )
        except Exception:
            self.controller = UlfmRecoveryController.from_snapshot(before)
            raise
        self._persist_transition(before, self.controller.snapshot())
        return event

    def prepare_recovery(self, failed_endpoints: Sequence[str]) -> dict[str, Any]:
        before = self.controller.snapshot()
        try:
            plan = self.controller.prepare_recovery(
                failed_endpoints,
                generation=self.controller.generation,
                generation_token=self.controller.generation_token,
            )
        except Exception:
            self.controller = UlfmRecoveryController.from_snapshot(before)
            raise
        self._persist_transition(before, self.controller.snapshot())
        return plan

    def commit_recovery(
        self,
        plan_sha256: str,
        attestations: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        before = self.controller.snapshot()
        try:
            receipt = self.controller.commit_recovery(plan_sha256, attestations)
        except Exception:
            self.controller = UlfmRecoveryController.from_snapshot(before)
            raise
        self._persist_transition(
            before, self.controller.snapshot(), receipt=receipt
        )
        return receipt


def verify_recovery_snapshot(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Strictly verify a persistent recovery state and all conservation bounds."""
    if not isinstance(raw, Mapping):
        raise UlfmRecoveryError("ULFM recovery snapshot must be an object")
    body = dict(raw)
    supplied = _digest(body.pop("snapshot_sha256", ""), "ULFM recovery snapshot")
    if content_digest(body) != supplied:
        raise UlfmRecoveryError("ULFM recovery snapshot identity changed")
    expected = {
        "schema",
        "protocol",
        "run_id",
        "policy",
        "policy_sha256",
        "generation",
        "generation_token",
        "members",
        "shards",
        "recovery_queue",
        "pending_recovery",
        "recovery_count",
    }
    if set(body) != expected:
        raise UlfmRecoveryError("ULFM recovery snapshot shape changed")
    if (
        body["schema"] != ULFM_SNAPSHOT_SCHEMA
        or body["protocol"] != ULFM_RECOVERY_PROTOCOL
    ):
        raise UlfmRecoveryError("ULFM recovery snapshot scope changed")
    run_id = _identity(body["run_id"], "ULFM run")
    policy = UlfmRecoveryPolicy.from_sealed(body["policy"])
    if body["policy_sha256"] != policy.sha256:
        raise UlfmRecoveryError("ULFM recovery policy identity changed")
    generation = _integer(
        body["generation"], "ULFM recovery generation", 0, (1 << 63) - 1
    )
    members_raw = body["members"]
    if (
        not isinstance(members_raw, Mapping)
        or not 1 <= len(members_raw) <= policy.max_endpoints
    ):
        raise UlfmRecoveryError("ULFM membership is invalid")
    members: dict[str, dict[str, Any]] = {}
    ranks: set[int] = set()
    for raw_endpoint, raw_member in members_raw.items():
        endpoint = _identity(raw_endpoint, "ULFM endpoint")
        if not isinstance(raw_member, Mapping) or set(raw_member) != {
            "rank",
            "incarnation_sha256",
            "host_id",
        }:
            raise UlfmRecoveryError("ULFM member shape changed")
        rank = _integer(raw_member["rank"], "ULFM member rank", 0, MAX_ENDPOINTS - 1)
        if rank in ranks:
            raise UlfmRecoveryError("ULFM member ranks contain duplicates")
        ranks.add(rank)
        members[endpoint] = {
            "rank": rank,
            "incarnation_sha256": _digest(
                raw_member["incarnation_sha256"], "ULFM endpoint incarnation"
            ),
            "host_id": _identity(raw_member["host_id"], "ULFM host"),
        }
    if ranks != set(range(len(members))):
        raise UlfmRecoveryError("ULFM member ranks are not dense")
    generation_token = _digest(
        body["generation_token"], "ULFM recovery generation token"
    )
    if generation_token != _generation_token(run_id, generation, members):
        raise UlfmRecoveryError("ULFM generation token changed")

    shards_raw = body["shards"]
    if (
        not isinstance(shards_raw, Mapping)
        or not 1 <= len(shards_raw) <= policy.max_shards
    ):
        raise UlfmRecoveryError("ULFM shard inventory is invalid")
    shards: dict[str, dict[str, Any]] = {}
    active_shards: set[str] = set()
    for raw_shard_id, raw_shard in shards_raw.items():
        shard_id = _identity(raw_shard_id, "ULFM shard")
        fields = {
            "owner_endpoint",
            "generation",
            "shard_token",
            "checkpoint_sha256",
            "cursor",
            "active_work_id",
            "active_lease_token",
            "lease_ordinal",
            "completed",
            "completion_chain_sha256",
        }
        if not isinstance(raw_shard, Mapping) or set(raw_shard) != fields:
            raise UlfmRecoveryError("ULFM shard state shape changed")
        owner = _identity(raw_shard["owner_endpoint"], "ULFM shard owner")
        if owner not in members or raw_shard["generation"] != generation:
            raise UlfmRecoveryError("ULFM shard generation/owner changed")
        token = _digest(raw_shard["shard_token"], "ULFM shard token")
        if token != _shard_token(run_id, generation, shard_id, owner):
            raise UlfmRecoveryError("ULFM shard token changed")
        work = _optional_identity(raw_shard["active_work_id"], "ULFM active work")
        lease = raw_shard["active_lease_token"]
        if work:
            _digest(lease, "ULFM active lease")
            active_shards.add(shard_id)
        elif lease != "":
            raise UlfmRecoveryError("ULFM inactive shard retains a lease")
        shards[shard_id] = {
            "owner_endpoint": owner,
            "generation": generation,
            "shard_token": token,
            "checkpoint_sha256": _digest(
                raw_shard["checkpoint_sha256"], "ULFM shard checkpoint"
            ),
            "cursor": _integer(
                raw_shard["cursor"], "ULFM shard cursor", 0, (1 << 63) - 1
            ),
            "active_work_id": work,
            "active_lease_token": lease,
            "lease_ordinal": _integer(
                raw_shard["lease_ordinal"], "ULFM lease ordinal", 0, (1 << 63) - 1
            ),
            "completed": _integer(
                raw_shard["completed"], "ULFM completed work", 0, (1 << 63) - 1
            ),
            "completion_chain_sha256": _digest(
                raw_shard["completion_chain_sha256"], "ULFM completion chain"
            ),
        }
    queue = _normalize_recovery_queue(
        body["recovery_queue"], policy=policy, shards=shards
    )
    if active_shards & {item["shard_id"] for item in queue}:
        raise UlfmRecoveryError("ULFM shard is both active and queued for recovery")
    pending = body["pending_recovery"]
    if pending is not None:
        pending = _verify_pending_plan(
            pending,
            run_id=run_id,
            policy=policy,
            generation=generation,
            generation_token=generation_token,
            members=members,
            shards=shards,
            recovery_queue=queue,
        )
    recovery_count = _integer(
        body["recovery_count"], "ULFM recovery count", 0, generation
    )
    normalized = {
        **body,
        "members": members,
        "shards": shards,
        "recovery_queue": queue,
        "pending_recovery": pending,
        "recovery_count": recovery_count,
        "snapshot_sha256": supplied,
    }
    return normalized


def _normalize_recovery_queue(
    raw: Any,
    *,
    policy: UlfmRecoveryPolicy,
    shards: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    values = _bounded_sequence(raw, "ULFM recovery queue", policy.max_recovery_queue)
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    fields = {
        "shard_id",
        "owner_endpoint",
        "work_id",
        "lease_token",
        "checkpoint_sha256",
        "cursor",
    }
    for item in values:
        if not isinstance(item, Mapping) or set(item) != fields:
            raise UlfmRecoveryError("ULFM recovery queue item shape changed")
        shard_id = _identity(item["shard_id"], "ULFM recovery shard")
        if shard_id not in shards or shard_id in seen:
            raise UlfmRecoveryError("ULFM recovery queue shard is duplicate or unknown")
        seen.add(shard_id)
        normalized.append(
            {
                "shard_id": shard_id,
                "owner_endpoint": _identity(
                    item["owner_endpoint"], "ULFM previous shard owner"
                ),
                "work_id": _identity(item["work_id"], "ULFM recovery work"),
                "lease_token": _digest(item["lease_token"], "ULFM previous lease"),
                "checkpoint_sha256": _digest(
                    item["checkpoint_sha256"], "ULFM recovery checkpoint"
                ),
                "cursor": _integer(
                    item["cursor"], "ULFM recovery cursor", 0, (1 << 63) - 1
                ),
            }
        )
    if normalized != sorted(
        normalized, key=lambda item: (item["shard_id"], item["work_id"])
    ):
        raise UlfmRecoveryError("ULFM recovery queue order changed")
    return normalized


def _verify_pending_plan(
    raw: Any,
    *,
    run_id: str,
    policy: UlfmRecoveryPolicy,
    generation: int,
    generation_token: str,
    members: Mapping[str, Mapping[str, Any]],
    shards: Mapping[str, Mapping[str, Any]],
    recovery_queue: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise UlfmRecoveryError("ULFM pending recovery must be an object")
    body = dict(raw)
    supplied = _digest(body.pop("plan_sha256", ""), "ULFM recovery plan")
    if content_digest(body) != supplied:
        raise UlfmRecoveryError("ULFM recovery plan identity changed")
    expected = {
        "schema",
        "protocol",
        "run_id",
        "policy_sha256",
        "base_generation",
        "target_generation",
        "base_generation_token",
        "base_members",
        "suspected_failed_endpoints",
        "in_flight",
        "preexisting_recovery_queue",
    }
    if set(body) != expected or (
        body["schema"] != ULFM_PLAN_SCHEMA
        or body["protocol"] != ULFM_RECOVERY_PROTOCOL
        or body["run_id"] != run_id
        or body["policy_sha256"] != policy.sha256
        or body["base_generation"] != generation
        or body["target_generation"] != generation + 1
        or body["base_generation_token"] != generation_token
        or body["base_members"] != dict(members)
    ):
        raise UlfmRecoveryError("ULFM recovery plan scope changed")
    suspected = _bounded_sequence(
        body["suspected_failed_endpoints"],
        "ULFM suspected failures",
        policy.max_endpoints,
    )
    if (
        suspected != sorted(suspected)
        or len(set(suspected)) != len(suspected)
        or not set(suspected) < set(members)
    ):
        raise UlfmRecoveryError("ULFM suspected failure set changed")
    expected_in_flight = [
        {
            "shard_id": shard_id,
            "owner_endpoint": shard["owner_endpoint"],
            "work_id": shard["active_work_id"],
            "lease_token": shard["active_lease_token"],
            "checkpoint_sha256": shard["checkpoint_sha256"],
            "cursor": shard["cursor"],
        }
        for shard_id, shard in sorted(shards.items())
        if shard["active_work_id"]
    ]
    if body["in_flight"] != expected_in_flight:
        raise UlfmRecoveryError("ULFM in-flight recovery snapshot changed")
    if body["preexisting_recovery_queue"] != list(recovery_queue):
        raise UlfmRecoveryError("ULFM preexisting recovery queue changed")
    body["plan_sha256"] = supplied
    return body


def verify_recovery_plan(
    raw: Mapping[str, Any],
    *,
    policy: UlfmRecoveryPolicy,
) -> dict[str, Any]:
    """Verify a standalone recovery plan before any live MPI side effect."""
    if not isinstance(raw, Mapping):
        raise UlfmRecoveryError("ULFM recovery plan must be an object")
    body = dict(raw)
    supplied = _digest(body.pop("plan_sha256", ""), "ULFM recovery plan")
    if content_digest(body) != supplied:
        raise UlfmRecoveryError("ULFM recovery plan identity changed")
    expected = {
        "schema",
        "protocol",
        "run_id",
        "policy_sha256",
        "base_generation",
        "target_generation",
        "base_generation_token",
        "base_members",
        "suspected_failed_endpoints",
        "in_flight",
        "preexisting_recovery_queue",
    }
    if set(body) != expected or (
        body["schema"] != ULFM_PLAN_SCHEMA
        or body["protocol"] != ULFM_RECOVERY_PROTOCOL
        or body["policy_sha256"] != policy.sha256
    ):
        raise UlfmRecoveryError("ULFM recovery plan scope/shape changed")
    run_id = _identity(body["run_id"], "ULFM run")
    base = _integer(
        body["base_generation"], "ULFM plan base generation", 0, (1 << 63) - 2
    )
    target = _integer(
        body["target_generation"],
        "ULFM plan target generation",
        1,
        (1 << 63) - 1,
    )
    if target != base + 1:
        raise UlfmRecoveryError("ULFM recovery plan generation is not consecutive")
    members_raw = body["base_members"]
    if (
        not isinstance(members_raw, Mapping)
        or not 2 <= len(members_raw) <= policy.max_endpoints
    ):
        raise UlfmRecoveryError("ULFM recovery plan membership is invalid")
    members: dict[str, dict[str, Any]] = {}
    ranks: set[int] = set()
    for raw_endpoint, raw_member in members_raw.items():
        endpoint = _identity(raw_endpoint, "ULFM plan endpoint")
        if not isinstance(raw_member, Mapping) or set(raw_member) != {
            "rank",
            "incarnation_sha256",
            "host_id",
        }:
            raise UlfmRecoveryError("ULFM recovery plan member shape changed")
        rank = _integer(raw_member["rank"], "ULFM plan rank", 0, MAX_ENDPOINTS - 1)
        if rank in ranks:
            raise UlfmRecoveryError("ULFM recovery plan ranks contain duplicates")
        ranks.add(rank)
        members[endpoint] = {
            "rank": rank,
            "incarnation_sha256": _digest(
                raw_member["incarnation_sha256"], "ULFM plan incarnation"
            ),
            "host_id": _identity(raw_member["host_id"], "ULFM plan host"),
        }
    if ranks != set(range(len(members))):
        raise UlfmRecoveryError("ULFM recovery plan ranks are not dense")
    token = _digest(body["base_generation_token"], "ULFM plan generation token")
    if token != _generation_token(run_id, base, members):
        raise UlfmRecoveryError("ULFM recovery plan generation token changed")
    failures = _bounded_sequence(
        body["suspected_failed_endpoints"],
        "ULFM suspected failures",
        policy.max_endpoints,
    )
    if (
        not failures
        or failures != sorted(failures)
        or len(set(failures)) != len(failures)
        or not set(failures) < set(members)
    ):
        raise UlfmRecoveryError("ULFM recovery plan failure set changed")

    fields = {
        "shard_id",
        "owner_endpoint",
        "work_id",
        "lease_token",
        "checkpoint_sha256",
        "cursor",
    }

    def normalize_items(value: Any, label: str) -> list[dict[str, Any]]:
        items = _bounded_sequence(value, label, policy.max_recovery_queue)
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw_item in items:
            if not isinstance(raw_item, Mapping) or set(raw_item) != fields:
                raise UlfmRecoveryError(f"{label} item shape changed")
            shard_id = _identity(raw_item["shard_id"], f"{label} shard")
            owner = _identity(raw_item["owner_endpoint"], f"{label} owner")
            if shard_id in seen or owner not in members:
                raise UlfmRecoveryError(f"{label} shard/owner changed")
            seen.add(shard_id)
            normalized.append(
                {
                    "shard_id": shard_id,
                    "owner_endpoint": owner,
                    "work_id": _identity(raw_item["work_id"], f"{label} work"),
                    "lease_token": _digest(
                        raw_item["lease_token"], f"{label} lease"
                    ),
                    "checkpoint_sha256": _digest(
                        raw_item["checkpoint_sha256"], f"{label} checkpoint"
                    ),
                    "cursor": _integer(
                        raw_item["cursor"], f"{label} cursor", 0, (1 << 63) - 1
                    ),
                }
            )
        if normalized != sorted(
            normalized, key=lambda item: (item["shard_id"], item["work_id"])
        ):
            raise UlfmRecoveryError(f"{label} order changed")
        return normalized

    in_flight = normalize_items(body["in_flight"], "ULFM in-flight work")
    queued = normalize_items(
        body["preexisting_recovery_queue"], "ULFM preexisting recovery queue"
    )
    if (
        len(in_flight) + len(queued) > policy.max_recovery_queue
        or {item["shard_id"] for item in in_flight}
        & {item["shard_id"] for item in queued}
    ):
        raise UlfmRecoveryError("ULFM recovery plan work conservation failed")
    body["base_members"] = members
    body["suspected_failed_endpoints"] = failures
    body["in_flight"] = in_flight
    body["preexisting_recovery_queue"] = queued
    body["plan_sha256"] = supplied
    return body


def verify_recovery_receipt(
    raw: Mapping[str, Any],
    *,
    post_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify a recovery receipt, optionally joining it to the post-state."""
    if not isinstance(raw, Mapping):
        raise UlfmRecoveryError("ULFM recovery receipt must be an object")
    body = dict(raw)
    supplied = _digest(body.pop("receipt_sha256", ""), "ULFM recovery receipt")
    if content_digest(body) != supplied:
        raise UlfmRecoveryError("ULFM recovery receipt identity changed")
    expected = {
        "schema",
        "protocol",
        "run_id",
        "policy_sha256",
        "plan_sha256",
        "base_generation",
        "target_generation",
        "new_generation_token",
        "old_members",
        "new_members",
        "failed_endpoints",
        "survivor_attestations",
        "reassignments",
        "shard_assignment_sha256",
        "requeued_work",
        "conservation",
        "post_state_sha256",
    }
    if set(body) != expected or (
        body["schema"] != ULFM_RECEIPT_SCHEMA
        or body["protocol"] != ULFM_RECOVERY_PROTOCOL
    ):
        raise UlfmRecoveryError("ULFM recovery receipt scope/shape changed")
    run_id = _identity(body["run_id"], "ULFM run")
    _digest(body["policy_sha256"], "ULFM policy")
    _digest(body["plan_sha256"], "ULFM recovery plan")
    base = _integer(
        body["base_generation"], "ULFM receipt base generation", 0, (1 << 63) - 2
    )
    target = _integer(
        body["target_generation"],
        "ULFM receipt target generation",
        1,
        (1 << 63) - 1,
    )
    if target != base + 1:
        raise UlfmRecoveryError("ULFM receipt generation is not consecutive")
    new_generation_token = _digest(
        body["new_generation_token"], "ULFM new generation token"
    )

    def normalize_members(value: Any, label: str) -> dict[str, dict[str, Any]]:
        if (
            not isinstance(value, Mapping)
            or not 1 <= len(value) <= MAX_ENDPOINTS
        ):
            raise UlfmRecoveryError(f"{label} membership is invalid")
        normalized: dict[str, dict[str, Any]] = {}
        ranks: set[int] = set()
        for raw_endpoint, raw_member in value.items():
            endpoint = _identity(raw_endpoint, f"{label} endpoint")
            if not isinstance(raw_member, Mapping) or set(raw_member) != {
                "rank",
                "incarnation_sha256",
                "host_id",
            }:
                raise UlfmRecoveryError(f"{label} member shape changed")
            rank = _integer(
                raw_member["rank"], f"{label} rank", 0, MAX_ENDPOINTS - 1
            )
            if rank in ranks:
                raise UlfmRecoveryError(f"{label} ranks contain duplicates")
            ranks.add(rank)
            normalized[endpoint] = {
                "rank": rank,
                "incarnation_sha256": _digest(
                    raw_member["incarnation_sha256"], f"{label} incarnation"
                ),
                "host_id": _identity(raw_member["host_id"], f"{label} host"),
            }
        if ranks != set(range(len(normalized))):
            raise UlfmRecoveryError(f"{label} ranks are not dense")
        return normalized

    old_members = normalize_members(body["old_members"], "old ULFM")
    new_members = normalize_members(body["new_members"], "new ULFM")
    old_generation_token = _generation_token(run_id, base, old_members)
    if new_generation_token != _generation_token(run_id, target, new_members):
        raise UlfmRecoveryError("ULFM receipt new generation token changed")
    attestations, failed = _normalize_attestations(
        body["survivor_attestations"],
        run_id=run_id,
        generation=base,
        generation_token=old_generation_token,
        members=old_members,
        suspected_failures=set(),
        maximum=MAX_ENDPOINTS,
    )
    if list(failed) != body["failed_endpoints"] or set(attestations) != set(
        new_members
    ):
        raise UlfmRecoveryError("ULFM receipt failed/survivor membership changed")
    expected_attestations = [attestations[key] for key in sorted(attestations)]
    if body["survivor_attestations"] != expected_attestations:
        raise UlfmRecoveryError("ULFM receipt attestation order changed")
    for endpoint, member in new_members.items():
        attestation = attestations[endpoint]
        if (
            member["rank"] != attestation["new_rank"]
            or member["incarnation_sha256"] != attestation["incarnation_sha256"]
            or member["host_id"] != attestation["host_id"]
        ):
            raise UlfmRecoveryError("ULFM receipt new membership changed")

    reassignments = body["reassignments"]
    if not isinstance(reassignments, Mapping) or len(reassignments) > MAX_SHARDS:
        raise UlfmRecoveryError("ULFM receipt reassignments are invalid")
    normalized_reassignments: dict[str, dict[str, str]] = {}
    for raw_shard, raw_assignment in reassignments.items():
        shard_id = _identity(raw_shard, "ULFM reassigned shard")
        if not isinstance(raw_assignment, Mapping) or set(raw_assignment) != {
            "old_owner",
            "new_owner",
        }:
            raise UlfmRecoveryError("ULFM reassignment shape changed")
        old_owner = _identity(raw_assignment["old_owner"], "ULFM old shard owner")
        new_owner = _identity(raw_assignment["new_owner"], "ULFM new shard owner")
        if old_owner not in failed or new_owner not in new_members:
            raise UlfmRecoveryError("ULFM reassignment owner is inconsistent")
        normalized_reassignments[shard_id] = {
            "old_owner": old_owner,
            "new_owner": new_owner,
        }
    if dict(reassignments) != normalized_reassignments:
        raise UlfmRecoveryError("ULFM reassignment order/content changed")
    shard_assignment_sha256 = _digest(
        body["shard_assignment_sha256"], "ULFM shard assignment"
    )
    post_state_sha256 = _digest(body["post_state_sha256"], "ULFM post state")

    conservation = body["conservation"]
    conservation_fields = {
        "old_endpoints",
        "survivors",
        "failed",
        "shards_before",
        "shards_after",
        "in_flight_before",
        "recovery_queue_before",
        "recovery_queue_after",
    }
    if not isinstance(conservation, Mapping) or set(conservation) != conservation_fields:
        raise UlfmRecoveryError("ULFM receipt conservation shape changed")
    normalized_conservation = {
        field: _integer(
            conservation[field], f"ULFM conservation {field}", 0, MAX_SHARDS
        )
        for field in conservation_fields
    }
    if (
        normalized_conservation["old_endpoints"] != len(old_members)
        or normalized_conservation["survivors"] != len(new_members)
        or normalized_conservation["failed"] != len(failed)
        or normalized_conservation["old_endpoints"]
        != normalized_conservation["survivors"]
        + normalized_conservation["failed"]
        or normalized_conservation["shards_before"]
        != normalized_conservation["shards_after"]
        or normalized_conservation["recovery_queue_after"]
        != normalized_conservation["recovery_queue_before"]
        + normalized_conservation["in_flight_before"]
    ):
        raise UlfmRecoveryError("ULFM receipt conservation failed")

    snapshot = None
    if post_snapshot is not None:
        snapshot = verify_recovery_snapshot(post_snapshot)
        if (
            snapshot["snapshot_sha256"] != post_state_sha256
            or snapshot["run_id"] != run_id
            or snapshot["policy_sha256"] != body["policy_sha256"]
            or snapshot["generation"] != target
            or snapshot["generation_token"] != new_generation_token
            or snapshot["members"] != new_members
            or snapshot["recovery_queue"] != body["requeued_work"]
            or snapshot["pending_recovery"] is not None
            or len(snapshot["shards"])
            != normalized_conservation["shards_after"]
        ):
            raise UlfmRecoveryError("ULFM receipt does not match the post-state")
        assignments = {
            shard_id: {
                "owner_endpoint": shard["owner_endpoint"],
                "shard_token": shard["shard_token"],
            }
            for shard_id, shard in sorted(snapshot["shards"].items())
        }
        if content_digest(assignments) != shard_assignment_sha256:
            raise UlfmRecoveryError("ULFM receipt shard assignment changed")
        normalized_queue = _normalize_recovery_queue(
            body["requeued_work"],
            policy=UlfmRecoveryPolicy.from_sealed(snapshot["policy"]),
            shards=snapshot["shards"],
        )
        if normalized_queue != body["requeued_work"]:
            raise UlfmRecoveryError("ULFM receipt recovery queue changed")
        for shard_id, assignment in normalized_reassignments.items():
            if (
                shard_id not in snapshot["shards"]
                or snapshot["shards"][shard_id]["owner_endpoint"]
                != assignment["new_owner"]
            ):
                raise UlfmRecoveryError("ULFM receipt reassignment changed")
    elif not isinstance(body["requeued_work"], list):
        raise UlfmRecoveryError("ULFM receipt recovery queue is invalid")

    body["old_members"] = old_members
    body["new_members"] = new_members
    body["reassignments"] = normalized_reassignments
    body["conservation"] = normalized_conservation
    body["receipt_sha256"] = supplied
    return body


def observe_failed_endpoints(
    comm: Any,
    members: Mapping[str, Mapping[str, Any]],
    *,
    mpi: Any | None = None,
) -> tuple[str, ...]:
    """Translate locally known failed communicator ranks to stable endpoints."""
    if mpi is None:
        from mpi4py import MPI as mpi  # type: ignore[no-redef]

    if not isinstance(members, Mapping) or not members:
        raise UlfmRecoveryError("ULFM failure observation membership is invalid")
    rank_to_endpoint: dict[int, str] = {}
    for raw_endpoint, raw_member in members.items():
        endpoint = _identity(raw_endpoint, "ULFM endpoint")
        if not isinstance(raw_member, Mapping) or "rank" not in raw_member:
            raise UlfmRecoveryError("ULFM failure observation member is invalid")
        rank = _integer(
            raw_member["rank"], "ULFM observed member rank", 0, MAX_ENDPOINTS - 1
        )
        if rank in rank_to_endpoint:
            raise UlfmRecoveryError("ULFM observed member ranks contain duplicates")
        rank_to_endpoint[rank] = endpoint
    if set(rank_to_endpoint) != set(range(len(rank_to_endpoint))):
        raise UlfmRecoveryError("ULFM observed member ranks are not dense")
    if int(comm.Get_size()) != len(rank_to_endpoint):
        raise UlfmRecoveryError("ULFM communicator/membership size changed")
    failed_group = None
    comm_group = None
    try:
        failed_group = comm.Get_failed()
        failed_count = int(failed_group.Get_size())
        if failed_count == 0:
            return ()
        comm_group = comm.Get_group()
        translated = failed_group.Translate_ranks(
            list(range(failed_count)), comm_group
        )
        if len(translated) != failed_count or any(
            rank == getattr(mpi, "UNDEFINED", -32766) or rank not in rank_to_endpoint
            for rank in translated
        ):
            raise UlfmRecoveryError("ULFM failed rank translation is incomplete")
        return tuple(sorted(rank_to_endpoint[int(rank)] for rank in translated))
    finally:
        if failed_group is not None:
            _free_quietly(failed_group)
        if comm_group is not None:
            _free_quietly(comm_group)


@dataclass(frozen=True)
class UlfmRepairResult:
    communicator: Any
    attestations: tuple[dict[str, Any], ...]
    failed_endpoints: tuple[str, ...]
    attempts: int


def _request_test(request: Any) -> tuple[bool, Any]:
    result = request.Test()
    if isinstance(result, tuple):
        if not result:
            return False, None
        return bool(result[0]), result[1] if len(result) > 1 else None
    return bool(result), None


def _wait_request(
    request: Any,
    *,
    timeout: float,
    poll_interval: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> Any:
    deadline = monotonic() + timeout
    first = True
    while first or monotonic() < deadline:
        first = False
        completed, payload = _request_test(request)
        if completed:
            return payload
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        sleep(min(poll_interval, remaining))
    raise UlfmCollectiveTimeout("ULFM collective deadline expired", request)


def complete_collective_before_deadline(
    request: Any,
    comm: Any,
    *,
    policy: UlfmRecoveryPolicy,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """Wait for a collective request and release it only after completion."""
    try:
        payload = _wait_request(
            request,
            timeout=policy.collective_timeout_seconds,
            poll_interval=policy.poll_interval_seconds,
            monotonic=monotonic,
            sleep=sleep,
        )
    except UlfmCollectiveTimeout as error:
        error.retain(comm)
        raise
    _free_quietly(request)
    return payload


def deadline_agree(
    comm: Any,
    flag: bool,
    *,
    policy: UlfmRecoveryPolicy,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Run ULFM agreement through ``Iagree`` with the configured deadline."""
    iagree = getattr(comm, "Iagree", None)
    if not callable(iagree):
        raise UlfmRuntimeError(
            "ULFM runtime lacks deadline-capable communicator Iagree"
        )
    agreement = array("i", [1 if flag else 0])
    request = iagree(agreement)
    if not callable(getattr(request, "Test", None)):
        raise UlfmRuntimeError("ULFM Iagree did not return a request")
    complete_collective_before_deadline(
        request,
        comm,
        policy=policy,
        monotonic=monotonic,
        sleep=sleep,
    )
    return bool(agreement[0])


def _bounded_attestation_allgather(
    comm: Any,
    local_attestation: Mapping[str, Any],
    *,
    mpi: Any,
    policy: UlfmRecoveryPolicy,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> list[dict[str, Any]]:
    """Exchange bounded canonical attestations with standard buffer collectives."""
    encoded = canonical_json(local_attestation)
    if not 0 < len(encoded) <= MAX_ATTESTATION_BYTES:
        raise UlfmRecoveryError("ULFM endpoint attestation exceeds its byte budget")
    size = int(comm.Get_size())
    if not 1 <= size <= policy.max_endpoints:
        raise UlfmRecoveryError("repaired communicator size exceeds its budget")
    send_length = array("Q", [len(encoded)])
    lengths = array("Q", [0]) * size
    request = comm.Iallgather(
        [send_length, mpi.UNSIGNED_LONG_LONG],
        [lengths, mpi.UNSIGNED_LONG_LONG],
    )
    complete_collective_before_deadline(
        request,
        comm,
        policy=policy,
        monotonic=monotonic,
        sleep=sleep,
    )
    if any(not 0 < int(length) <= MAX_ATTESTATION_BYTES for length in lengths):
        raise UlfmRecoveryError("ULFM remote attestation length is invalid")

    send_buffer = bytearray(MAX_ATTESTATION_BYTES)
    send_buffer[: len(encoded)] = encoded
    receive_buffer = bytearray(MAX_ATTESTATION_BYTES * size)
    request = comm.Iallgather(
        [send_buffer, mpi.BYTE],
        [receive_buffer, mpi.BYTE],
    )
    complete_collective_before_deadline(
        request,
        comm,
        policy=policy,
        monotonic=monotonic,
        sleep=sleep,
    )
    gathered: list[dict[str, Any]] = []
    for rank, length in enumerate(lengths):
        offset = rank * MAX_ATTESTATION_BYTES
        payload = bytes(receive_buffer[offset : offset + int(length)])
        try:
            decoded = json.loads(payload.decode("ascii"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UlfmRecoveryError("ULFM remote attestation is not canonical JSON") from error
        if not isinstance(decoded, dict) or canonical_json(decoded) != payload:
            raise UlfmRecoveryError("ULFM remote attestation encoding is not canonical")
        gathered.append(decoded)
    return gathered


def _free_quietly(value: Any) -> None:
    try:
        value.Free()
    except Exception:
        pass


def deadline_shrink(
    comm: Any,
    *,
    policy: UlfmRecoveryPolicy,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """Shrink a communicator through a pollable request with a hard deadline.

    A blocking ``Shrink`` cannot be made safely interruptible inside the same
    MPI process.  Live recovery therefore requires mpi4py's ``Ishrink`` tuple
    form, which returns the candidate communicator and its completion request.
    Runtimes without that capability fail closed instead of entering an
    unbounded collective.
    """
    ishrink = getattr(comm, "Ishrink", None)
    if not callable(ishrink):
        raise UlfmRuntimeError(
            "ULFM runtime lacks deadline-capable communicator Ishrink"
        )
    issued = ishrink()
    candidate = None
    request = None
    if isinstance(issued, tuple) and len(issued) == 2:
        candidate, request = issued
    elif callable(getattr(issued, "Test", None)):
        request = issued
    else:
        raise UlfmRuntimeError("ULFM Ishrink returned an unsupported handle")
    if not callable(getattr(request, "Test", None)):
        raise UlfmRuntimeError("ULFM Ishrink did not return a request")
    try:
        payload = complete_collective_before_deadline(
            request,
            candidate if candidate is not None else comm,
            policy=policy,
            monotonic=monotonic,
            sleep=sleep,
        )
    except UlfmCollectiveTimeout as error:
        error.retain(comm)
        if candidate is not None:
            error.retain(candidate)
        raise
    if candidate is None:
        candidate = payload
    if candidate is None or not callable(getattr(candidate, "Get_size", None)):
        raise UlfmRuntimeError("ULFM Ishrink did not produce a communicator")
    return candidate


def shrink_and_attest(
    comm: Any,
    *,
    local_endpoint: EndpointIdentity,
    recovery_plan: Mapping[str, Any],
    policy: UlfmRecoveryPolicy,
    mpi: Any | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> UlfmRepairResult:
    """Revoke, shrink, attest stable membership, and agree on exact survivors.

    Communicator repair and the following two-stage buffer ``Iallgather`` are
    both polled to finite in-process deadlines.  A caller must discard the old
    communicator after success.  No communicator is returned unless every
    survivor validates the same dense membership.
    """
    if mpi is None:
        from mpi4py import MPI as mpi  # type: ignore[no-redef]

    plan = verify_recovery_plan(recovery_plan, policy=policy)
    members = plan.get("base_members")
    if not isinstance(members, Mapping):
        raise UlfmRecoveryError("ULFM recovery membership is missing")
    local_member = members.get(local_endpoint.endpoint_id)
    if not isinstance(local_member, Mapping) or (
        local_member.get("incarnation_sha256") != local_endpoint.incarnation_sha256
        or local_member.get("host_id") != local_endpoint.host_id
    ):
        raise UlfmRecoveryError("local ULFM endpoint is outside the recovery plan")
    suspected = set(plan.get("suspected_failed_endpoints", ()))
    current = comm
    owned_current = False
    last_error = ""
    for attempt in range(1, policy.max_repair_attempts + 1):
        candidate = None
        try:
            current.Set_errhandler(mpi.ERRORS_RETURN)
            current.Revoke()
            candidate = deadline_shrink(
                current,
                policy=policy,
                monotonic=monotonic,
                sleep=sleep,
            )
            candidate.Set_errhandler(mpi.ERRORS_RETURN)
            new_rank = int(candidate.Get_rank())
            attestation = build_endpoint_attestation(
                run_id=plan["run_id"],
                base_generation=plan["base_generation"],
                base_generation_token=plan["base_generation_token"],
                endpoint=local_endpoint,
                old_rank=int(local_member["rank"]),
                new_rank=new_rank,
            )
            gathered = _bounded_attestation_allgather(
                candidate,
                attestation,
                mpi=mpi,
                policy=policy,
                monotonic=monotonic,
                sleep=sleep,
            )
            normalized, failed = _normalize_attestations(
                gathered,
                run_id=plan["run_id"],
                generation=plan["base_generation"],
                generation_token=plan["base_generation_token"],
                members=members,
                suspected_failures=suspected,
                maximum=policy.max_endpoints,
            )
            if int(candidate.Get_size()) != len(normalized):
                raise UlfmRecoveryError("repaired communicator size changed")
            agreed = deadline_agree(
                candidate,
                True,
                policy=policy,
                monotonic=monotonic,
                sleep=sleep,
            )
            if not bool(agreed):
                raise UlfmRuntimeError("ULFM membership agreement was rejected")
            if owned_current and current is not candidate:
                _free_quietly(current)
            return UlfmRepairResult(
                communicator=candidate,
                attestations=tuple(normalized[key] for key in sorted(normalized)),
                failed_endpoints=failed,
                attempts=attempt,
            )
        except UlfmCollectiveTimeout:
            raise
        except (UlfmRecoveryError, UlfmRuntimeError) as error:
            last_error = str(error)
        except Exception as error:
            last_error = f"{type(error).__name__}: {error}"
        if candidate is not None:
            if owned_current and current is not candidate:
                _free_quietly(current)
            current = candidate
            owned_current = True
    if owned_current:
        _free_quietly(current)
    raise UlfmRuntimeError(
        f"ULFM communicator repair failed after {policy.max_repair_attempts} "
        f"attempt(s): {last_error or 'unknown error'}"
    )


def _error_class(error: BaseException, mpi: Any) -> int | None:
    try:
        if hasattr(error, "Get_error_class"):
            return int(error.Get_error_class())
        if hasattr(error, "error_class"):
            return int(error.error_class)
    except Exception:
        return None
    return None


def is_ulfm_failure(error: BaseException, mpi: Any | None = None) -> bool:
    """Return whether an MPI exception is one of the ULFM failure classes."""
    if mpi is None:
        try:
            from mpi4py import MPI as mpi  # type: ignore[no-redef]
        except ImportError:
            return False
    observed = _error_class(error, mpi)
    expected = {
        getattr(mpi, name, None)
        for name in ("ERR_PROC_FAILED", "ERR_PROC_FAILED_PENDING", "ERR_REVOKED")
    }
    return observed is not None and observed in expected


def probe_ulfm_runtime(
    comm: Any,
    *,
    mpi: Any | None = None,
    policy: UlfmRecoveryPolicy | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Exercise ULFM semantics on a duplicate communicator, not just symbols."""
    if mpi is None:
        from mpi4py import MPI as mpi  # type: ignore[no-redef]

    active_policy = policy or UlfmRecoveryPolicy()
    required_methods = (
        "Set_errhandler",
        "Get_failed",
        "Ack_failed",
        "Iagree",
        "Revoke",
        "Is_revoked",
        "Ishrink",
    )
    constants = {
        name: getattr(mpi, name, None)
        for name in ("ERR_PROC_FAILED", "ERR_PROC_FAILED_PENDING", "ERR_REVOKED")
    }
    result: dict[str, Any] = {
        "schema": ULFM_CAPABILITY_SCHEMA,
        "protocol": ULFM_RECOVERY_PROTOCOL,
        "policy_sha256": active_policy.sha256,
        "mpi_standard": list(mpi.Get_version()),
        "mpi_library": str(mpi.Get_library_version()).rstrip("\x00\n"),
        "required_methods": list(required_methods),
        "error_constants": constants,
        "available": False,
        "semantic_checks": {
            "errors_return": False,
            "get_failed": False,
            "agree": False,
            "revoke": False,
            "shrink": False,
            "post_shrink_agree": False,
        },
        "error": "",
    }
    duplicate = None
    repaired = None
    collective_timeout = False
    try:
        missing = [name for name in required_methods if not callable(getattr(comm, name, None))]
        if missing or any(type(value) is not int for value in constants.values()):
            raise UlfmRuntimeError(
                "missing ULFM symbols: " + ",".join(missing or sorted(constants))
            )
        duplicate = comm.Dup()
        duplicate.Set_errhandler(mpi.ERRORS_RETURN)
        result["semantic_checks"]["errors_return"] = True
        group = duplicate.Get_failed()
        try:
            if int(group.Get_size()) != 0:
                raise UlfmRuntimeError("fresh communicator reports failed ranks")
        finally:
            _free_quietly(group)
        result["semantic_checks"]["get_failed"] = True
        if not deadline_agree(
            duplicate,
            True,
            policy=active_policy,
            monotonic=monotonic,
            sleep=sleep,
        ):
            raise UlfmRuntimeError("ULFM Agree rejected a unanimous value")
        result["semantic_checks"]["agree"] = True
        duplicate.Revoke()
        if not bool(duplicate.Is_revoked()):
            raise UlfmRuntimeError("ULFM Revoke did not change communicator state")
        result["semantic_checks"]["revoke"] = True
        repaired = deadline_shrink(
            duplicate,
            policy=active_policy,
            monotonic=monotonic,
            sleep=sleep,
        )
        if int(repaired.Get_size()) != int(comm.Get_size()):
            raise UlfmRuntimeError("no-failure ULFM shrink changed communicator size")
        result["semantic_checks"]["shrink"] = True
        if not deadline_agree(
            repaired,
            True,
            policy=active_policy,
            monotonic=monotonic,
            sleep=sleep,
        ):
            raise UlfmRuntimeError("post-shrink ULFM Agree failed")
        result["semantic_checks"]["post_shrink_agree"] = True
        result["available"] = all(result["semantic_checks"].values())
    except UlfmCollectiveTimeout as error:
        collective_timeout = True
        result["error"] = f"{type(error).__name__}: {error}"[:1024]
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"[:1024]
    finally:
        if repaired is not None and not collective_timeout:
            _free_quietly(repaired)
        if duplicate is not None and not collective_timeout:
            _free_quietly(duplicate)
    result["capability_sha256"] = content_digest(result)
    return result
