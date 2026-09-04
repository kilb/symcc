#!/usr/bin/env python3
"""Small atomic persistence backend for ULFM recovery snapshots.

ULFM recovery state is a single, bounded state-machine snapshot.  Storing it
in the general query database made communicator repair depend on SQLite WAL,
which is intentionally unsupported on many network filesystems.  This backend
uses the shared-state contract that the MPI driver already qualifies: kernel
advisory locks, same-directory atomic replacement, and durable fsync.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Mapping

try:
    from .distributed_state import (
        bounded_advisory_lock,
        durable_makedirs,
        durable_replace,
        fsync_directory,
    )
    from .mpi_ulfm_recovery import (
        UlfmRecoveryError,
        UlfmRecoveryPolicy,
        canonical_json,
        content_digest,
        verify_recovery_receipt,
        verify_recovery_snapshot,
    )
except ImportError:
    from distributed_state import (
        bounded_advisory_lock,
        durable_makedirs,
        durable_replace,
        fsync_directory,
    )
    from mpi_ulfm_recovery import (
        UlfmRecoveryError,
        UlfmRecoveryPolicy,
        canonical_json,
        content_digest,
        verify_recovery_receipt,
        verify_recovery_snapshot,
    )


ULFM_ATOMIC_STORE_SCHEMA = "symcc-ulfm-atomic-snapshot-store-v1"
_MAX_STORE_BYTES = 512 * 1024 * 1024
_MAX_STATE_ORDINAL = (1 << 63) - 1


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object member {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r} is not supported")


class AtomicUlfmSnapshotStore:
    """Persist one ordered ULFM state machine without a database runtime.

    Every commit is serialized by a bounded advisory lock.  Equal snapshots at
    the same ordinal are idempotent, while a different snapshot at that ordinal
    is a fail-closed fork.  These are the same transition rules used by the
    QueryStore backend, but the on-disk object is purpose-built and portable to
    qualified network filesystems.
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        timeout: float = 30.0,
        file_mode: int = 0o600,
    ):
        self.root = Path(root).resolve()
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 0.001 <= float(timeout) <= 3600.0
        ):
            raise ValueError("ULFM snapshot lock timeout is invalid")
        if (
            isinstance(file_mode, bool)
            or not isinstance(file_mode, int)
            or file_mode & ~0o777
            or file_mode & 0o111
            or file_mode & 0o600 != 0o600
        ):
            raise ValueError("ULFM snapshot file mode is invalid")
        self.timeout = float(timeout)
        self.file_mode = file_mode
        self.path = self.root / "recovery-state.json"
        self.lock_path = self.root / "recovery-state.lock"
        durable_makedirs(str(self.root))
        created_lock = False
        try:
            lock_descriptor = os.open(
                self.lock_path,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                self.file_mode,
            )
            created_lock = True
        except FileExistsError:
            lock_descriptor = os.open(
                self.lock_path,
                os.O_RDWR | os.O_NOFOLLOW,
            )
        try:
            if created_lock:
                os.fchmod(lock_descriptor, self.file_mode)
            os.fsync(lock_descriptor)
        finally:
            os.close(lock_descriptor)
        fsync_directory(str(self.root))

    @staticmethod
    def _validate_scope(run_id: str, policy: UlfmRecoveryPolicy) -> None:
        if not isinstance(policy, UlfmRecoveryPolicy):
            raise ValueError("invalid ULFM recovery persistence policy")
        if not isinstance(run_id, str) or not run_id or len(run_id.encode()) > 256:
            raise ValueError("invalid ULFM recovery persistence run")

    def _load_envelope(self) -> dict[str, Any] | None:
        try:
            descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        try:
            size = os.fstat(descriptor).st_size
            if not 1 <= size <= _MAX_STORE_BYTES:
                raise ValueError("ULFM atomic snapshot size is invalid")
            chunks: list[bytes] = []
            remaining = size
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("ULFM atomic snapshot is truncated")
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            os.close(descriptor)
        try:
            raw = json.loads(
                b"".join(chunks),
                object_pairs_hook=_json_object,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("ULFM atomic snapshot is not canonical JSON") from error
        if not isinstance(raw, dict) or set(raw) != {
            "schema",
            "run_id",
            "policy_sha256",
            "state_ordinal",
            "snapshot",
            "receipts",
            "envelope_sha256",
        }:
            raise ValueError("ULFM atomic snapshot shape changed")
        supplied = raw.pop("envelope_sha256")
        if not isinstance(supplied, str) or content_digest(raw) != supplied:
            raise ValueError("ULFM atomic snapshot identity changed")
        raw["envelope_sha256"] = supplied
        if raw["schema"] != ULFM_ATOMIC_STORE_SCHEMA:
            raise ValueError("ULFM atomic snapshot schema changed")
        ordinal = raw["state_ordinal"]
        if type(ordinal) is not int or not 0 <= ordinal <= _MAX_STATE_ORDINAL:
            raise ValueError("ULFM atomic snapshot ordinal is invalid")
        snapshot = verify_recovery_snapshot(raw["snapshot"])
        if (
            raw["run_id"] != snapshot["run_id"]
            or raw["policy_sha256"] != snapshot["policy_sha256"]
        ):
            raise ValueError("ULFM atomic snapshot scope changed")
        if not isinstance(raw["receipts"], list):
            raise ValueError("ULFM atomic receipt inventory is invalid")
        receipts: list[dict[str, Any]] = []
        generations: list[int] = []
        for item in raw["receipts"]:
            receipt = verify_recovery_receipt(item)
            if (
                receipt["run_id"] != raw["run_id"]
                or receipt["policy_sha256"] != raw["policy_sha256"]
            ):
                raise ValueError("ULFM atomic receipt scope changed")
            receipts.append(receipt)
            generations.append(int(receipt["target_generation"]))
        if generations != sorted(set(generations)):
            raise ValueError("ULFM atomic receipts are not ordered and unique")
        raw["snapshot"] = snapshot
        raw["receipts"] = receipts
        return raw

    def _write_envelope(self, envelope: Mapping[str, Any]) -> None:
        body = dict(envelope)
        body["envelope_sha256"] = content_digest(body)
        payload = canonical_json(body)
        if len(payload) > _MAX_STORE_BYTES:
            raise ValueError("ULFM atomic snapshot exceeds its size budget")
        temporary = self.root / (
            f".recovery-state.{os.getpid()}.{time.monotonic_ns()}.tmp"
        )
        descriptor = -1
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                self.file_mode,
            )
            os.fchmod(descriptor, self.file_mode)
            offset = 0
            while offset < len(payload):
                offset += os.write(descriptor, payload[offset:])
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            durable_replace(str(temporary), str(self.path))
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def load_ulfm_recovery_snapshot(
        self,
        run_id: str,
        policy: UlfmRecoveryPolicy,
    ) -> dict[str, Any] | None:
        state = self.load_ulfm_recovery_state(run_id, policy)
        return None if state is None else state[0]

    def load_ulfm_recovery_state(
        self,
        run_id: str,
        policy: UlfmRecoveryPolicy,
    ) -> tuple[dict[str, Any], int] | None:
        self._validate_scope(run_id, policy)
        envelope = self._load_envelope()
        if envelope is None:
            return None
        snapshot = envelope["snapshot"]
        if (
            envelope["run_id"] != run_id
            or envelope["policy_sha256"] != policy.sha256
            or snapshot["policy"] != policy.as_dict()
        ):
            raise ValueError("stored ULFM atomic snapshot scope changed")
        return snapshot, int(envelope["state_ordinal"])

    def commit_ulfm_recovery_snapshot(
        self,
        run_id: str,
        policy: UlfmRecoveryPolicy,
        snapshot: Mapping[str, Any],
        *,
        receipt: Mapping[str, Any] | None = None,
        state_ordinal: int | None = None,
    ) -> str:
        self._validate_scope(run_id, policy)
        try:
            verified = verify_recovery_snapshot(snapshot)
        except UlfmRecoveryError as error:
            raise ValueError("invalid ULFM recovery checkpoint") from error
        if (
            verified["run_id"] != run_id
            or verified["policy_sha256"] != policy.sha256
            or verified["policy"] != policy.as_dict()
        ):
            raise ValueError("ULFM recovery checkpoint scope changed")
        normalized_receipt: dict[str, Any] | None = None
        if receipt is not None:
            try:
                normalized_receipt = verify_recovery_receipt(
                    receipt, post_snapshot=verified
                )
            except UlfmRecoveryError as error:
                raise ValueError("invalid ULFM recovery receipt") from error
            if (
                normalized_receipt["run_id"] != run_id
                or normalized_receipt["policy_sha256"] != policy.sha256
            ):
                raise ValueError("ULFM recovery receipt scope changed")
        explicit_ordinal = state_ordinal is not None
        if state_ordinal is not None and (
            type(state_ordinal) is not int
            or not 0 <= state_ordinal <= _MAX_STATE_ORDINAL
        ):
            raise ValueError("invalid ULFM recovery state ordinal")

        with bounded_advisory_lock(
            str(self.lock_path),
            timeout=self.timeout,
            description="ULFM atomic snapshot commit",
        ):
            existing = self._load_envelope()
            receipts: list[dict[str, Any]] = []
            identity = str(verified["snapshot_sha256"])
            generation = int(verified["generation"])
            recovery_count = int(verified["recovery_count"])
            pending = verified["pending_recovery"] is not None
            if existing is None:
                next_ordinal = 0 if state_ordinal is None else int(state_ordinal)
                if (
                    generation != 0
                    or recovery_count != 0
                    or pending
                    or receipt is not None
                    or next_ordinal != 0
                ):
                    raise ValueError(
                        "ULFM recovery persistence must start at "
                        "generation/ordinal zero"
                    )
            else:
                if (
                    existing["run_id"] != run_id
                    or existing["policy_sha256"] != policy.sha256
                ):
                    raise ValueError("ULFM atomic snapshot store scope changed")
                old_snapshot = existing["snapshot"]
                old_identity = str(old_snapshot["snapshot_sha256"])
                old_generation = int(old_snapshot["generation"])
                old_count = int(old_snapshot["recovery_count"])
                old_ordinal = int(existing["state_ordinal"])
                old_pending = old_snapshot["pending_recovery"] is not None
                receipts = list(existing["receipts"])
                if not explicit_ordinal and identity == old_identity:
                    return "idempotent"
                next_ordinal = (
                    int(state_ordinal) if explicit_ordinal else old_ordinal + 1
                )
                if next_ordinal < old_ordinal:
                    return "stale"
                if next_ordinal == old_ordinal:
                    if identity != old_identity:
                        raise ValueError(
                            "ULFM recovery checkpoint forked at one state ordinal"
                        )
                    return "idempotent"
                if next_ordinal != old_ordinal + 1:
                    raise ValueError(
                        "ULFM recovery state ordinal is not consecutive"
                    )
                if identity == old_identity:
                    raise ValueError(
                        "ULFM recovery state ordinal advanced without a state change"
                    )
                if generation < old_generation or recovery_count < old_count:
                    return "stale"
                if generation == old_generation:
                    if recovery_count != old_count:
                        raise ValueError(
                            "ULFM recovery count changed without a generation change"
                        )
                    if old_pending or normalized_receipt is not None:
                        raise ValueError(
                            "ULFM recovery checkpoint forked within one generation"
                        )
                    if not explicit_ordinal and not pending:
                        raise ValueError(
                            "ULFM runtime checkpoint requires an explicit "
                            "state ordinal"
                        )
                else:
                    if (
                        generation != old_generation + 1
                        or recovery_count != old_count + 1
                    ):
                        raise ValueError(
                            "ULFM recovery generation is not consecutive"
                        )
                    if not old_pending or pending or normalized_receipt is None:
                        raise ValueError(
                            "ULFM recovery generation advanced without "
                            "prepare/receipt"
                        )
                    if (
                        normalized_receipt["base_generation"] != old_generation
                        or normalized_receipt["target_generation"] != generation
                    ):
                        raise ValueError(
                            "ULFM recovery receipt generation changed"
                        )
                    old_plan = old_snapshot["pending_recovery"]
                    if (
                        old_plan is None
                        or normalized_receipt["plan_sha256"]
                        != old_plan["plan_sha256"]
                    ):
                        raise ValueError(
                            "ULFM recovery receipt plan changed"
                        )

            if normalized_receipt is not None:
                target = int(normalized_receipt["target_generation"])
                prior = {
                    int(item["target_generation"]): item for item in receipts
                }.get(target)
                if prior is not None and (
                    prior["receipt_sha256"]
                    != normalized_receipt["receipt_sha256"]
                ):
                    raise ValueError(
                        "ULFM recovery receipt forked at one generation"
                    )
                if prior is None:
                    receipts.append(normalized_receipt)
                    receipts.sort(key=lambda item: int(item["target_generation"]))

            self._write_envelope(
                {
                    "schema": ULFM_ATOMIC_STORE_SCHEMA,
                    "run_id": run_id,
                    "policy_sha256": policy.sha256,
                    "state_ordinal": next_ordinal,
                    "snapshot": verified,
                    "receipts": receipts,
                }
            )
            return "advanced"
