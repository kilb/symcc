#!/usr/bin/env python3
"""Bounded schedule exploration for MPI concolic executions.

The native preload runtime records synchronization scheduling points as a
compact text trace.  This module turns those traces into replay prefixes and
keeps a small persistent queue of unexplored alternatives.  It is intentionally
bounded: the goal is useful concurrency diversity for fuzzing, not exhaustive
model checking of every interleaving.
"""

from __future__ import annotations

from collections import deque
import ctypes
import ctypes.util
from dataclasses import dataclass
import hashlib
import heapq
import itertools
import json
import os
import re
import tempfile
from typing import Any, Iterable, Mapping


CONTROLLED_OPS = frozenset({
    "lock",
    "trylock",
    "rdlock",
    "wrlock",
    "wait",
    "join",
    "detach",
    "cancel",
})
ACQUIRE_OPS = frozenset({"lock", "trylock", "rdlock", "wrlock"})
RELEASE_OPS = frozenset({"unlock", "rwunlock", "signal", "broadcast"})
MEMORY_OPS = frozenset({"read", "write", "rmw"})
FENCE_OPS = frozenset({"fence"})
MEMORY_MODELS = frozenset({"SC", "TSO", "RA"})
SCHEDULE_CONSTRAINT_SCHEMA = "symcc-schedule-constraint-v1"
SCHEDULE_SMT_SCHEMA_V1 = "symcc-schedule-smt-v1"
SCHEDULE_SMT_SCHEMA_V2 = "symcc-schedule-smt-v2"
SCHEDULE_SMT_SCHEMA_V3 = "symcc-schedule-smt-v3"
SCHEDULE_SMT_SCHEMA_V4 = "symcc-schedule-smt-v4"
SCHEDULE_SMT_SCHEMA_V5 = "symcc-schedule-smt-v5"
SCHEDULE_SMT_SCHEMA_V6 = "symcc-schedule-smt-v6"
SCHEDULE_SMT_SCHEMA = "symcc-schedule-smt-v7"
SCHEDULE_SMT_SCHEMAS = frozenset({
    SCHEDULE_SMT_SCHEMA_V1,
    SCHEDULE_SMT_SCHEMA_V2,
    SCHEDULE_SMT_SCHEMA_V3,
    SCHEDULE_SMT_SCHEMA_V4,
    SCHEDULE_SMT_SCHEMA_V5,
    SCHEDULE_SMT_SCHEMA_V6,
    SCHEDULE_SMT_SCHEMA,
})
SCHEDULE_SMT_ORDER_ENCODINGS = frozenset({"partial", "permutation"})
SCHEDULE_ORDER_IR_SCHEMA = "symcc-lifecycle-order-ir-v1"
SCHEDULE_LINEAR_EXTENSION_SCHEMA = (
    "symcc-lifecycle-linear-extension-v1"
)
SOURCE_DPOR_SCHEMA = "symcc-bounded-source-dpor-v1"
WAKEUP_TREE_SCHEMA = "symcc-bounded-wakeup-tree-v1"
CONDPOR_GRAPH_SCHEMA = "symcc-bounded-condpor-graph-v1"
NATIVE_CONDPOR_MEMORY_GRAPH_SCHEMA = (
    "symcc-native-condpor-memory-graph-v1"
)
OPERATIONAL_ENABLEDNESS_SCHEMA = "symcc-operational-enabledness-v1"
JOINT_PATH_SCHEDULE_SCHEMA = "symcc-joint-path-schedule-rf-v1"
SYNC_ATTEMPT_MODES = {
    "lock": "mutex_write",
    "trylock": "mutex_write",
    "rdlock": "rw_read",
    "wrlock": "rw_write",
}
SYNC_OUTCOME_OPS = frozenset({
    "acquire",
    "lock_fail",
    "trylock_fail",
    "rdlock_fail",
    "wrlock_fail",
})
SYNC_UNLOCK_OPS = frozenset({"unlock", "rwunlock"})
COND_LIFECYCLE_OPS = frozenset({
    "wait",
    "wait_mutex_release",
    "wait_mutex_acquire",
    "wake",
    "wait_timeout",
    "wait_fail",
    "signal",
    "broadcast",
})
THREAD_LIFECYCLE_OPS = frozenset({
    "create",
    "create_success",
    "create_fail",
    "thread_start",
    "thread_exit",
    "thread_retire",
    "join",
    "join_success",
    "join_fail",
    "join_cancelled",
    "detach",
    "detach_success",
    "detach_fail",
    "cancel",
    "cancel_success",
    "cancel_fail",
})
SYNC_LIFECYCLE_OPS = frozenset(SYNC_ATTEMPT_MODES) | (
    SYNC_OUTCOME_OPS | SYNC_UNLOCK_OPS | COND_LIFECYCLE_OPS
    | THREAD_LIFECYCLE_OPS
)


@dataclass(frozen=True)
class ScheduleEvent:
    seq: int
    tid: int
    op: str
    obj: str
    tags: tuple[str, ...] = ()

    @property
    def controlled(self) -> bool:
        return self.op in CONTROLLED_OPS

    @property
    def memory(self) -> bool:
        return self.op in MEMORY_OPS

    @property
    def write(self) -> bool:
        return self.op in {"write", "rmw"}

    @property
    def schedulable(self) -> bool:
        return self.controlled or self.memory or self.op in FENCE_OPS


@dataclass(frozen=True)
class DporReplayJob:
    path: str
    input_id: str
    prefix: tuple[int, ...]


@dataclass(frozen=True)
class SchedulePoint:
    event: ScheduleEvent
    clock: tuple[tuple[int, int], ...]
    lockset: tuple[str, ...]


@dataclass(frozen=True)
class ScheduleConflict:
    left_seq: int
    right_seq: int
    left_tid: int
    right_tid: int
    obj: str
    kind: str
    hb_ordered: bool
    shared_locks: tuple[str, ...] = ()

    def to_mapping(self) -> dict[str, Any]:
        return {
            "left_seq": self.left_seq,
            "right_seq": self.right_seq,
            "left_tid": self.left_tid,
            "right_tid": self.right_tid,
            "object": self.obj,
            "kind": self.kind,
            "hb_ordered": self.hb_ordered,
            "shared_locks": list(self.shared_locks),
        }


def normalize_schedule_prefix(raw: Any, *, max_len: int = 256) -> tuple[int, ...]:
    """Return a bounded tuple of logical thread ids from JSON/MPI payloads."""
    if raw is None or raw == "":
        return ()
    if isinstance(raw, str):
        tokens = raw.replace(",", " ").replace(";", " ").split()
    elif isinstance(raw, Iterable):
        tokens = list(raw)
    else:
        return ()
    result: list[int] = []
    for item in tokens:
        try:
            tid = int(item)
        except (TypeError, ValueError):
            continue
        if tid < 0:
            continue
        result.append(tid)
        if len(result) >= max_len:
            break
    return tuple(result)


def parse_schedule_trace(raw: str | Iterable[str]) -> list[ScheduleEvent]:
    """Parse ``seq tid op object [tags...]`` rows from symcc_schedule_rt."""
    lines = raw.splitlines() if isinstance(raw, str) else raw
    events: list[ScheduleEvent] = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) < 4:
            continue
        try:
            seq = int(fields[0], 0)
            tid = int(fields[1], 0)
        except ValueError:
            continue
        if tid < 0:
            continue
        op = fields[2].strip().lower()
        obj = fields[3].strip().lower()
        tags = tuple(
            field.strip().lower()[:128]
            for field in fields[4:12]
            if field.strip()
        )
        events.append(ScheduleEvent(seq, tid, op, obj, tags))
    events.sort(key=lambda event: event.seq)
    atomic_results = {
        _tag_value(event.tags, "group"): _tag_value(
            event.tags, "success"
        )
        for event in events
        if event.op == "atomic_result"
        and _tag_value(event.tags, "group")
    }
    atomic_commits: dict[str, tuple[int, str, str, str, str, str] | None] = {}
    for event in events:
        if event.op != "atomic_commit":
            continue
        group = _tag_value(event.tags, "group")
        mode = _tag_value(event.tags, "mode")
        advanced = _tag_value(event.tags, "advanced")
        mismatch = _tag_value(event.tags, "mismatch")
        prefix_index = _tag_value(event.tags, "prefix-index")
        try:
            group_number = int(group, 10)
            prefix_number = int(prefix_index, 10)
        except ValueError:
            continue
        if (
            group_number <= 0
            or prefix_number < 0
            or mode not in {"0", "1"}
            or advanced not in {"0", "1"}
            or mismatch not in {"0", "1"}
        ):
            continue
        record = (
            event.tid,
            event.obj,
            mode,
            advanced,
            mismatch,
            str(prefix_number),
        )
        atomic_commits[group] = (
            record if group not in atomic_commits else None
        )
    atomic_values: dict[
        str, dict[str, tuple[int, str, str, str] | None]
    ] = {}
    for event in events:
        if event.op != "atomic_value":
            continue
        group = _tag_value(event.tags, "group")
        role = _tag_value(event.tags, "role")
        bits = _tag_value(event.tags, "bits")
        value = _tag_value(event.tags, "value")
        try:
            group_number = int(group, 10)
            bit_width = int(bits, 10)
            concrete_value = int(value, 0)
        except ValueError:
            continue
        if (
            group_number <= 0
            or role not in {"read", "write", "operand", "expected", "desired"}
            or bit_width < 1
            or bit_width > 64
            or concrete_value < 0
            or concrete_value >= (1 << bit_width)
        ):
            continue
        roles = atomic_values.setdefault(group, {})
        record = (event.tid, event.obj, str(bit_width), hex(concrete_value))
        roles[role] = record if role not in roles else None
    normalized: list[ScheduleEvent] = []
    for event in events:
        kind = _tag_value(event.tags, "kind")
        group = _tag_value(event.tags, "group")
        if not kind or not group:
            normalized.append(event)
            continue
        success = atomic_results.get(group, "unknown") if kind == "cmpxchg" else ""
        op = (
            "read" if kind == "cmpxchg" and success == "0"
            else "rmw" if kind == "cmpxchg"
            else event.op
        )
        tags = list(event.tags)
        if kind == "cmpxchg":
            tags.append(f"success={success}")
        commit = atomic_commits.get(group)
        if (
            commit is not None
            and commit[0] == event.tid
            and commit[1] == event.obj
        ):
            tags.extend((
                f"commit-mode={commit[2]}",
                f"commit-advanced={commit[3]}",
                f"commit-mismatch={commit[4]}",
                f"commit-prefix-index={commit[5]}",
            ))
        values = atomic_values.get(group, {})
        for role in ("read", "write", "operand", "expected", "desired"):
            record = values.get(role)
            if (
                record is None
                or record[0] != event.tid
                or record[1] != event.obj
            ):
                continue
            _, _, bits, value = record
            tags.extend((f"{role}-bits={bits}", f"{role}-value={value}"))
        generic_role = ""
        if op == "read" and values.get("read") is not None:
            generic_role = "read"
        elif op == "write" and values.get("write") is not None:
            generic_role = "write"
        if generic_role:
            record = values[generic_role]
            if (
                record is not None
                and record[0] == event.tid
                and record[1] == event.obj
            ):
                _, _, bits, value = record
                tags.extend((f"value-bits={bits}", f"value={value}"))
        normalized.append(ScheduleEvent(
            event.seq,
            event.tid,
            op,
            event.obj,
            tuple(tags),
        ))
    return normalized


def write_schedule_prefix(path: str, prefix: Iterable[int]) -> bool:
    """Atomically write a replay prefix file consumed by the native runtime."""
    values = normalize_schedule_prefix(tuple(prefix))
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="ascii") as stream:
            for tid in values:
                stream.write(f"{tid}\n")
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def prepend_ld_preload(preload: str, existing: str | None) -> str:
    """Prepend a preload library while preserving existing LD_PRELOAD entries."""
    preload = str(preload).strip()
    entries = [
        item for item in str(existing or "").split(":")
        if item and item != preload
    ]
    return ":".join([preload] + entries) if preload else ":".join(entries)


def _clock_mapping(clock: tuple[tuple[int, int], ...]) -> dict[int, int]:
    return {int(tid): int(value) for tid, value in clock}


def _freeze_clock(clock: dict[int, int]) -> tuple[tuple[int, int], ...]:
    return tuple(sorted((tid, value) for tid, value in clock.items() if value))


def _merge_clock(target: dict[int, int], source: dict[int, int]) -> None:
    for tid, value in source.items():
        if value > target.get(tid, 0):
            target[tid] = value


def happens_before(left: SchedulePoint, right: SchedulePoint) -> bool:
    """Return true when vector clocks prove left is before right."""
    left_clock = _clock_mapping(left.clock)
    right_clock = _clock_mapping(right.clock)
    if not left_clock:
        return False
    for tid, value in left_clock.items():
        other = right_clock.get(tid, 0)
        if value > other:
            return False
    return left_clock != right_clock


def annotate_schedule(events: list[ScheduleEvent]) -> list[SchedulePoint]:
    """Attach SC vector-clock and lockset summaries to a schedule trace."""
    clocks: dict[int, dict[int, int]] = {}
    release_clocks: dict[str, dict[int, int]] = {}
    create_clocks: dict[str, dict[int, int]] = {}
    exit_clocks: dict[str, dict[int, int]] = {}
    held: dict[int, set[str]] = {}
    annotated: list[SchedulePoint] = []
    for event in sorted(events, key=lambda item: item.seq):
        clock = clocks.setdefault(event.tid, {})
        lockset = held.setdefault(event.tid, set())
        clock[event.tid] = clock.get(event.tid, 0) + 1
        if event.op in ACQUIRE_OPS or event.op == "wait_mutex_acquire":
            _merge_clock(clock, release_clocks.get(event.obj, {}))
        elif event.op == "thread_start":
            _merge_clock(clock, create_clocks.get(event.obj, {}))
        elif (event.op == "join_success"
              and "mapped=0" not in event.tags):
            _merge_clock(clock, exit_clocks.get(event.obj, {}))

        annotated.append(SchedulePoint(
            event=event,
            clock=_freeze_clock(clock),
            lockset=tuple(sorted(lockset)),
        ))

        if event.op in ACQUIRE_OPS or event.op == "wait_mutex_acquire":
            lockset.add(event.obj)
        elif event.op in RELEASE_OPS or event.op == "wait_mutex_release":
            release_clocks[event.obj] = dict(clock)
            lockset.discard(event.obj)
        if event.op == "create":
            create_clocks[event.obj] = dict(clock)
        elif event.op == "thread_exit":
            exit_clocks[event.obj] = dict(clock)
    return annotated


def classify_schedule_conflicts(
    events: list[ScheduleEvent],
    *,
    max_window: int = 32,
) -> tuple[ScheduleConflict, ...]:
    """Classify bounded SC conflicts for source-style replay generation."""
    points = [point for point in annotate_schedule(events)
              if point.event.schedulable]
    conflicts: list[ScheduleConflict] = []
    for i, left in enumerate(points):
        end = min(len(points), i + 1 + max(1, int(max_window)))
        for right in points[i + 1:end]:
            if left.event.tid == right.event.tid:
                continue
            shared_locks = tuple(sorted(
                set(left.lockset).intersection(right.lockset)))
            hb_ordered = (
                happens_before(left, right)
                or happens_before(right, left))
            kind = ""
            if left.event.memory or right.event.memory:
                if not (left.event.memory and right.event.memory):
                    continue
                if not _memory_events_overlap(
                    left.event, right.event
                ):
                    continue
                if not (left.event.write or right.event.write):
                    continue
                if shared_locks or hb_ordered:
                    continue
                kind = "memory"
            elif left.event.controlled and right.event.controlled:
                if left.event.obj != right.event.obj:
                    continue
                if hb_ordered:
                    continue
                kind = "sync"
            if not kind:
                continue
            conflicts.append(ScheduleConflict(
                left_seq=left.event.seq,
                right_seq=right.event.seq,
                left_tid=left.event.tid,
                right_tid=right.event.tid,
                obj=left.event.obj,
                kind=kind,
                hb_ordered=hb_ordered,
                shared_locks=shared_locks,
            ))
    return tuple(conflicts)


def schedule_trace_digest(events: Iterable[ScheduleEvent]) -> str:
    """Return a stable digest for the normalized schedule trace."""
    digest = hashlib.sha256()
    for event in sorted(events, key=lambda item: item.seq):
        row = " ".join((
            str(event.seq),
            str(event.tid),
            event.op,
            event.obj,
            *event.tags,
        ))
        digest.update((row + "\n").encode("utf-8", errors="replace"))
    return digest.hexdigest()


def _event_mapping(event: ScheduleEvent) -> dict[str, Any]:
    mapping = {
        "seq": event.seq,
        "tid": event.tid,
        "op": event.op,
        "object": event.obj,
        "controlled": event.controlled,
        "memory": event.memory,
    }
    if event.tags:
        mapping["tags"] = list(event.tags)
    return mapping


def _provenance_counts(events: Iterable[ScheduleEvent]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        if not event.memory:
            continue
        provenance = ""
        for tag in event.tags:
            if tag.startswith("prov="):
                provenance = tag.split("=", 1)[1][:64]
                break
        if not provenance:
            continue
        counts[provenance] = counts.get(provenance, 0) + 1
    return dict(sorted(counts.items()))


def _tag_value(tags: Iterable[str], name: str) -> str:
    prefix = str(name) + "="
    for tag in tags:
        if str(tag).startswith(prefix):
            return str(tag)[len(prefix):]
    return ""


def _memory_interval(event: ScheduleEvent) -> tuple[int, int] | None:
    if not event.memory:
        return None
    try:
        start = int(event.obj, 0)
        size = int(_tag_value(event.tags, "bytes") or "1", 0)
    except ValueError:
        return None
    if start < 0 or size <= 0:
        return None
    return start, start + size


def _memory_events_overlap(
    left: ScheduleEvent,
    right: ScheduleEvent,
) -> bool:
    left_interval = _memory_interval(left)
    right_interval = _memory_interval(right)
    if left_interval is None or right_interval is None:
        return left.obj == right.obj
    return (
        left_interval[0] < right_interval[1]
        and right_interval[0] < left_interval[1]
    )


def _memory_access_contains(
    source: ScheduleEvent,
    target: ScheduleEvent,
) -> bool:
    source_interval = _memory_interval(source)
    target_interval = _memory_interval(target)
    if source_interval is None or target_interval is None:
        return source.obj == target.obj
    return (
        source_interval[0] <= target_interval[0]
        and source_interval[1] >= target_interval[1]
    )


def _constraint_depends_on_read(
    tags: Iterable[str],
    read_id: str,
    read_object: str,
) -> bool:
    """Match explicit node/object dependencies and byte-address intervals."""
    normalized = tuple(str(tag) for tag in tags)
    explicit = _tag_value(normalized, "read-dep")
    if explicit in {read_id, read_object}:
        return True
    last_read = _tag_value(normalized, "last-read")
    if last_read in {read_id, read_object}:
        return True
    try:
        base = int(last_read, 0)
        address = int(read_object, 0)
        byte_count = int(
            _tag_value(normalized, "last-read-bytes") or "1", 0
        )
    except ValueError:
        return False
    return byte_count > 0 and base <= address < base + byte_count


def runtime_ready_evidence(
    events: Iterable[ScheduleEvent],
) -> dict[int, dict[str, Any]]:
    """Return decision-indexed cooperative runtime-ready snapshots."""
    evidence: dict[int, dict[str, Any]] = {}
    for event in sorted(events, key=lambda item: item.seq):
        if event.op != "ready":
            continue
        try:
            decision = int(_tag_value(event.tags, "decision"))
            chosen = int(_tag_value(event.tags, "chosen"))
        except ValueError:
            continue
        if decision < 0 or chosen < 0 or decision in evidence:
            continue
        tids: list[int] = []
        for raw in _tag_value(event.tags, "tids").split(","):
            if not raw:
                continue
            try:
                tid = int(raw)
            except ValueError:
                continue
            if tid >= 0 and tid not in tids:
                tids.append(tid)
        tids.sort()
        offers: list[dict[str, Any]] = []
        offered_tids: set[int] = set()
        for raw in _tag_value(event.tags, "offers").split(","):
            if not raw:
                continue
            parts = raw.split(":")
            if len(parts) not in {3, 4}:
                continue
            try:
                offer_tid = int(parts[0])
            except ValueError:
                continue
            if offer_tid < 0 or offer_tid in offered_tids:
                continue
            offered_tids.add(offer_tid)
            offers.append({
                "tid": offer_tid,
                "op": parts[1],
                "object": parts[2],
                "auxiliary": parts[3] if len(parts) == 4 else "0x0",
            })
        offers.sort(key=lambda row: int(row["tid"]))
        evidence[decision] = {
            "decision": decision,
            "chosen": chosen,
            "threads": tids,
            "complete": _tag_value(event.tags, "complete") == "1",
            "prefix_controlled": (
                _tag_value(event.tags, "prefix") == "1"
            ),
            "fallback": _tag_value(event.tags, "fallback") == "1",
            "event_seq": event.seq,
            "offers": offers,
        }
    return evidence


def _operational_enabledness_digest(
    certificate: Mapping[str, Any],
) -> str:
    return _canonical_json_digest({
        key: value
        for key, value in certificate.items()
        if key != "certificate_sha256"
    })


def _thread_object_id(obj: str) -> int | None:
    try:
        return int(str(obj), 0)
    except ValueError:
        return None


def operational_enabledness_certificate(
    events: list[ScheduleEvent],
) -> dict[str, Any]:
    """Reconstruct bounded completion-enabled sets from lifecycle evidence."""
    ordered = sorted(events, key=lambda event: event.seq)
    ready = runtime_ready_evidence(ordered)
    controlled_by_decision: dict[int, ScheduleEvent] = {}
    for event in ordered:
        if not event.controlled:
            continue
        raw_decision = _tag_value(event.tags, "decision")
        try:
            decision = int(raw_decision)
        except ValueError:
            continue
        controlled_by_decision.setdefault(decision, event)

    mutex_owner: dict[str, int] = {}
    rw_writer: dict[str, int] = {}
    rw_readers: dict[str, set[int]] = {}
    pending: dict[int, tuple[str, str]] = {}
    exited: set[int] = set()
    anomalies: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []

    def offer_status(offer: Mapping[str, Any]) -> dict[str, Any]:
        tid = int(offer["tid"])
        op = str(offer["op"])
        obj = str(offer["object"])
        auxiliary = str(offer.get("auxiliary", "0x0"))
        status = "unknown"
        reason = "operation_not_modeled"
        if op == "lock":
            owner = mutex_owner.get(obj)
            if owner is None:
                status, reason = "enabled", "mutex_observed_free"
            else:
                status, reason = "blocked", f"mutex_owned_by_t{owner}"
        elif op == "trylock":
            status, reason = "enabled", "nonblocking_try_operation"
        elif op == "rdlock":
            writer = rw_writer.get(obj)
            if writer is None:
                status, reason = "enabled", "rwlock_has_no_writer"
            else:
                status, reason = "blocked", f"rwlock_written_by_t{writer}"
        elif op == "wrlock":
            writer = rw_writer.get(obj)
            readers = sorted(rw_readers.get(obj, set()))
            if writer is None and not readers:
                status, reason = "enabled", "rwlock_observed_free"
            elif writer is not None:
                status, reason = "blocked", f"rwlock_written_by_t{writer}"
            else:
                reason = "rwlock_read_by_" + ",".join(
                    f"t{reader}" for reader in readers
                )
                status = "blocked"
        elif op == "join":
            target = _thread_object_id(obj)
            if target is None:
                status, reason = "unknown", "join_target_not_logical_tid"
            elif target in exited:
                status, reason = "enabled", "target_exit_observed"
            else:
                status, reason = "blocked", "target_exit_not_observed"
        elif op in {"detach", "cancel"}:
            status, reason = "enabled", "nonblocking_lifecycle_operation"
        elif op == "wait":
            owner = mutex_owner.get(auxiliary)
            if auxiliary in {"", "0", "0x0"}:
                status, reason = "unknown", "wait_mutex_not_in_ready_offer"
            elif owner == tid:
                status, reason = "enabled", "waiter_owns_associated_mutex"
            elif owner is None:
                status, reason = "blocked", "associated_mutex_not_owned"
            else:
                status, reason = "blocked", f"mutex_owned_by_t{owner}"
        return {
            "tid": tid,
            "op": op,
            "object": obj,
            "auxiliary": auxiliary,
            "status": status,
            "reason": reason,
        }

    ready_by_seq = {
        int(row["event_seq"]): (decision, row)
        for decision, row in ready.items()
    }
    for event in ordered:
        snapshot = ready_by_seq.get(event.seq)
        if snapshot is not None:
            decision, evidence = snapshot
            offers = [
                dict(offer) for offer in evidence.get("offers", ())
                if isinstance(offer, Mapping)
            ]
            chosen_event = controlled_by_decision.get(decision)
            if not offers and chosen_event is not None:
                offers = [{
                    "tid": chosen_event.tid,
                    "op": chosen_event.op,
                    "object": chosen_event.obj,
                    "auxiliary": (
                        _tag_value(chosen_event.tags, "mutex") or "0x0"
                    ),
                }]
            evaluated = [offer_status(offer) for offer in offers]
            enabled = sorted(
                int(row["tid"]) for row in evaluated
                if row["status"] == "enabled"
            )
            blocked = sorted(
                int(row["tid"]) for row in evaluated
                if row["status"] == "blocked"
            )
            unknown = sorted(
                int(row["tid"]) for row in evaluated
                if row["status"] == "unknown"
            )
            offered_threads = sorted(
                int(row["tid"]) for row in evaluated
            )
            chosen = int(evidence["chosen"])
            chosen_row = next(
                (row for row in evaluated if int(row["tid"]) == chosen),
                None,
            )
            decisions.append({
                "decision": decision,
                "event_seq": event.seq,
                "chosen": chosen,
                "ready_complete": bool(evidence["complete"]),
                "offers_complete": (
                    bool(evidence["complete"])
                    and offered_threads == evidence["threads"]
                ),
                "offers": evaluated,
                "enabled_threads": enabled,
                "blocked_threads": blocked,
                "unknown_threads": unknown,
                "chosen_status": (
                    str(chosen_row["status"])
                    if chosen_row is not None else "missing"
                ),
            })

        if event.controlled:
            pending[event.tid] = (event.op, event.obj)
        elif event.op == "acquire":
            attempt = pending.pop(event.tid, None)
            if attempt is None or attempt[1] != event.obj:
                anomalies.append({
                    "seq": event.seq,
                    "kind": "acquire_without_matching_attempt",
                    "tid": event.tid,
                    "object": event.obj,
                })
            elif attempt[0] in {"lock", "trylock"}:
                mutex_owner[event.obj] = event.tid
            elif attempt[0] == "rdlock":
                rw_readers.setdefault(event.obj, set()).add(event.tid)
            elif attempt[0] == "wrlock":
                rw_writer[event.obj] = event.tid
        elif event.op in {
            "lock_fail", "trylock_fail", "rdlock_fail", "wrlock_fail",
        }:
            pending.pop(event.tid, None)
        elif event.op == "unlock":
            owner = mutex_owner.get(event.obj)
            if owner == event.tid:
                mutex_owner.pop(event.obj, None)
            elif owner is not None:
                anomalies.append({
                    "seq": event.seq,
                    "kind": "mutex_unlock_owner_mismatch",
                    "tid": event.tid,
                    "object": event.obj,
                    "observed_owner": owner,
                })
        elif event.op == "rwunlock":
            if rw_writer.get(event.obj) == event.tid:
                rw_writer.pop(event.obj, None)
            else:
                readers = rw_readers.get(event.obj)
                if readers is not None:
                    readers.discard(event.tid)
                    if not readers:
                        rw_readers.pop(event.obj, None)
        elif event.op == "wait_mutex_release":
            if mutex_owner.get(event.obj) == event.tid:
                mutex_owner.pop(event.obj, None)
        elif event.op == "wait_mutex_acquire":
            mutex_owner[event.obj] = event.tid
        elif event.op in {
            "wake", "wait_timeout", "wait_fail",
            "join_success", "join_fail", "join_cancelled",
            "detach_success", "detach_fail",
            "cancel_success", "cancel_fail",
        }:
            pending.pop(event.tid, None)
        elif event.op == "thread_exit":
            exited.add(event.tid)

    unresolved = [
        {"tid": tid, "op": value[0], "object": value[1]}
        for tid, value in sorted(pending.items())
    ]
    runtime_stop = any(event.op == "runtime_stop" for event in ordered)
    offers_complete = bool(decisions) and all(
        row["offers_complete"] for row in decisions
    )
    no_unknown = bool(decisions) and all(
        not row["unknown_threads"] for row in decisions
    )
    certificate: dict[str, Any] = {
        "schema": OPERATIONAL_ENABLEDNESS_SCHEMA,
        "semantics": (
            "bounded-observed-pthread-completion-enabledness-v1"
        ),
        "trace_digest": schedule_trace_digest(ordered),
        "trace_events": [_event_mapping(event) for event in ordered],
        "decisions": decisions,
        "decision_count": len(decisions),
        "offers_complete": offers_complete,
        "all_offers_classified": no_unknown,
        "all_chosen_completion_enabled": bool(decisions) and all(
            row["chosen_status"] == "enabled" for row in decisions
        ),
        "runtime_stop_witnessed": runtime_stop,
        "unresolved_attempts": unresolved,
        "lifecycle_anomalies": anomalies,
        "bounded_terminal_execution_witnessed": (
            runtime_stop and not unresolved
        ),
        "sound_complete_claimed": False,
        "proved_scope": [
            "observed_mutex_rwlock_completion_enabledness",
            "observed_join_exit_completion_enabledness",
            "nonblocking_try_detach_cancel_invocation",
            "runtime_stop_and_pending_attempt_terminal_witness",
        ],
        "not_proved": [
            "condition_signal_wakeup_choice_and_spurious_wakeup_cause",
            "mutex_type_recursive_or_process_shared_semantics",
            "unobserved_or_noninterposed_synchronization",
            "scheduler_enforcement_of_completion_enabled_choices",
            "unbounded_operational_soundness_or_completeness",
        ],
    }
    certificate["certificate_sha256"] = (
        _operational_enabledness_digest(certificate)
    )
    return certificate


def verify_operational_enabledness_certificate(
    certificate: Mapping[str, Any],
) -> bool:
    """Recompute the bounded operational certificate from its bound trace."""
    try:
        if certificate.get("schema") != OPERATIONAL_ENABLEDNESS_SCHEMA:
            return False
        if certificate.get("certificate_sha256") != (
            _operational_enabledness_digest(certificate)
        ):
            return False
        raw_events = certificate.get("trace_events")
        if not isinstance(raw_events, list):
            return False
        events = [
            ScheduleEvent(
                seq=int(row["seq"]),
                tid=int(row["tid"]),
                op=str(row["op"]),
                obj=str(row["object"]),
                tags=tuple(str(tag) for tag in row.get("tags", ())),
            )
            for row in raw_events
            if isinstance(row, Mapping)
        ]
        return (
            len(events) == len(raw_events)
            and dict(certificate)
            == operational_enabledness_certificate(events)
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def _prefix_key(prefix: Iterable[int]) -> str:
    values = normalize_schedule_prefix(tuple(prefix))
    return ",".join(str(value) for value in values)


def _nonnegative_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return max(0, int(default))


def _schedule_events_dependent(
    left: ScheduleEvent,
    right: ScheduleEvent,
) -> bool:
    """Return the bounded SC dependence relation used by trace reduction."""
    if left.tid == right.tid:
        return False
    if left.memory or right.memory:
        return (
            left.memory
            and right.memory
            and _memory_events_overlap(left, right)
            and (left.write or right.write)
        )
    return (
        left.controlled
        and right.controlled
        and left.obj == right.obj
    )


def _canonical_json_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _bounded_dependency_graph(
    events: list[ScheduleEvent],
    *,
    max_events: int,
) -> dict[str, Any]:
    """Build a bounded Mazurkiewicz dependency graph for one SC trace."""
    event_cap = max(0, int(max_events))
    schedulable = [
        event
        for event in sorted(events, key=lambda item: item.seq)
        if event.schedulable
    ]
    modeled = schedulable[:event_cap]
    occurrences: dict[int, int] = {}
    nodes: list[dict[str, Any]] = []
    node_ids: list[str] = []
    by_thread: dict[int, list[int]] = {}
    for position, event in enumerate(modeled):
        occurrence = occurrences.get(event.tid, 0)
        occurrences[event.tid] = occurrence + 1
        node_id = f"t{event.tid}:{occurrence}"
        node_ids.append(node_id)
        by_thread.setdefault(event.tid, []).append(position)
        nodes.append({
            "id": node_id,
            "tid": event.tid,
            "thread_index": occurrence,
            "op": event.op,
            "object": event.obj,
            "tags": list(event.tags),
            "observed_position": position,
            "seq": event.seq,
        })

    edges: list[dict[str, str]] = []
    for positions in by_thread.values():
        for left, right in zip(positions, positions[1:]):
            edges.append({
                "source": node_ids[left],
                "target": node_ids[right],
                "kind": "program_order",
            })
    for left_index, left in enumerate(modeled):
        for right_index in range(left_index + 1, len(modeled)):
            right = modeled[right_index]
            if _schedule_events_dependent(left, right):
                edges.append({
                    "source": node_ids[left_index],
                    "target": node_ids[right_index],
                    "kind": (
                        "memory_dependency"
                        if left.memory else "sync_dependency"
                    ),
                })
    edges.sort(key=lambda row: (
        row["source"], row["target"], row["kind"]))

    signature_nodes = [
        {
            "id": node["id"],
            "tid": node["tid"],
            "thread_index": node["thread_index"],
            "op": node["op"],
            "object": node["object"],
            "tags": node["tags"],
        }
        for node in sorted(nodes, key=lambda row: row["id"])
    ]
    signature_payload = {
        "nodes": signature_nodes,
        "edges": edges,
    }
    equivalence_sha256 = _canonical_json_digest(signature_payload)

    successors: dict[str, list[str]] = {
        node_id: [] for node_id in node_ids
    }
    indegree = {node_id: 0 for node_id in node_ids}
    for edge in edges:
        successors[edge["source"]].append(edge["target"])
        indegree[edge["target"]] += 1
    ready = [node_id for node_id, degree in indegree.items() if degree == 0]
    heapq.heapify(ready)
    canonical_linearization: list[str] = []
    while ready:
        node_id = heapq.heappop(ready)
        canonical_linearization.append(node_id)
        for successor in sorted(successors[node_id]):
            indegree[successor] -= 1
            if indegree[successor] == 0:
                heapq.heappush(ready, successor)
    if len(canonical_linearization) != len(nodes):
        raise ValueError("bounded schedule dependency graph is cyclic")

    return {
        "event_count": len(schedulable),
        "modeled_event_count": len(modeled),
        "nodes": nodes,
        "edges": edges,
        "canonical_linearization": canonical_linearization,
        "equivalence_sha256": equivalence_sha256,
        "truncated": len(schedulable) > len(modeled),
    }


def _consume_thread_sequence(
    per_thread: Mapping[int, tuple[ScheduleEvent, ...]],
    cursors: Mapping[int, int],
    sequence: Iterable[int],
) -> tuple[list[ScheduleEvent], dict[int, int]] | None:
    updated = {int(tid): int(index) for tid, index in cursors.items()}
    consumed: list[ScheduleEvent] = []
    for raw_tid in sequence:
        tid = int(raw_tid)
        index = updated.get(tid, 0)
        thread_events = per_thread.get(tid, ())
        if index >= len(thread_events):
            return None
        consumed.append(thread_events[index])
        updated[tid] = index + 1
    return consumed, updated


def _weak_initial_process_sequence(
    events: list[ScheduleEvent],
    base_prefix: Iterable[int],
    candidate: Iterable[int],
    reference: Iterable[int],
    *,
    max_steps: int = 64,
) -> bool:
    """Check the POPL'14 weak-initial recursion on a bounded trace model.

    Per-thread next events are deterministic in this abstraction. Dependence is
    the same object/read-write relation used by the Source-DPOR certificate.
    """
    schedulable = tuple(
        event
        for event in sorted(events, key=lambda item: item.seq)
        if event.schedulable
    )
    per_thread: dict[int, tuple[ScheduleEvent, ...]] = {}
    tids = sorted({event.tid for event in schedulable})
    for tid in tids:
        per_thread[tid] = tuple(
            event for event in schedulable if event.tid == tid
        )
    base_result = _consume_thread_sequence(
        per_thread, {}, base_prefix
    )
    if base_result is None:
        return False
    _, base_cursors = base_result
    left = tuple(int(tid) for tid in candidate)
    right = tuple(int(tid) for tid in reference)
    if len(left) > max(0, int(max_steps)):
        return False

    def recurse(
        cursors: dict[int, int],
        pending: tuple[int, ...],
        target: tuple[int, ...],
    ) -> bool:
        if not pending:
            return True
        if len(pending) > max_steps:
            return False
        p = pending[0]
        p_result = _consume_thread_sequence(
            per_thread, cursors, (p,)
        )
        target_result = _consume_thread_sequence(
            per_thread, cursors, target
        )
        if p_result is None or target_result is None:
            return False
        p_event = p_result[0][0]
        target_events = target_result[0]

        first_p = -1
        for index, tid in enumerate(target):
            if tid == p:
                first_p = index
                break
        is_initial = (
            first_p >= 0
            and all(
                not _schedule_events_dependent(event, p_event)
                for event in target_events[:first_p]
            )
        )
        independent = all(
            not _schedule_events_dependent(p_event, event)
            for event in target_events
        )
        next_cursors = p_result[1]
        if is_initial:
            reduced = (
                target[:first_p] + target[first_p + 1:]
            )
            if recurse(next_cursors, pending[1:], reduced):
                return True
        if independent:
            return recurse(next_cursors, pending[1:], target)
        return False

    return recurse(base_cursors, left, right)


class BoundedWakeupTree:
    """Ordered wakeup-tree leaves under a bounded deterministic trace model."""

    def __init__(
        self,
        events: list[ScheduleEvent],
        base_prefix: Iterable[int],
        *,
        sleep: Iterable[int] = (),
        ready: Iterable[int] = (),
        ready_complete: bool = False,
        leaves: Iterable[Iterable[int]] = (),
        max_depth: int = 64,
        max_leaves: int = 256,
    ) -> None:
        self.events = list(events)
        self.base_prefix = tuple(int(tid) for tid in base_prefix)
        self.sleep = frozenset(int(tid) for tid in sleep)
        self.ready = frozenset(int(tid) for tid in ready)
        self.ready_complete = bool(ready_complete)
        self.max_depth = max(1, int(max_depth))
        self.max_leaves = max(1, int(max_leaves))
        self.leaves: list[tuple[int, ...]] = []
        for leaf in leaves:
            normalized = tuple(int(tid) for tid in leaf)
            if (
                normalized
                and len(normalized) <= self.max_depth
                and normalized not in self.leaves
            ):
                self.leaves.append(normalized)

    def weak_initial(
        self,
        candidate: Iterable[int],
        reference: Iterable[int],
        *,
        suffix: Iterable[int] = (),
    ) -> bool:
        return _weak_initial_process_sequence(
            self.events,
            self.base_prefix + tuple(int(tid) for tid in suffix),
            candidate,
            reference,
            max_steps=self.max_depth,
        )

    def insert(self, sequence: Iterable[int]) -> dict[str, Any]:
        candidate = tuple(int(tid) for tid in sequence)
        result: dict[str, Any] = {
            "sequence": list(candidate),
            "inserted": False,
            "reason": "",
        }
        if not candidate or len(candidate) > self.max_depth:
            result["reason"] = "invalid_or_bounded"
            return result
        if len(self.leaves) >= self.max_leaves:
            result["reason"] = "leaf_limit"
            return result
        if self.ready_complete and candidate[0] not in self.ready:
            result["reason"] = "root_not_ready"
            return result
        for sleeping in sorted(self.sleep):
            if self.weak_initial((sleeping,), candidate):
                result["reason"] = "sleep_weak_initial"
                result["witness_thread"] = sleeping
                return result
        for leaf_index, leaf in enumerate(self.leaves):
            if self.weak_initial(leaf, candidate):
                result["reason"] = "existing_leaf_weak_initial"
                result["witness_leaf"] = leaf_index
                return result
            common = 0
            while (
                common < len(leaf)
                and common < len(candidate)
                and leaf[common] == candidate[common]
            ):
                common += 1
            if common < len(leaf) and common < len(candidate):
                if self.weak_initial(
                    (leaf[common],),
                    candidate[common:],
                    suffix=candidate[:common],
                ):
                    result["reason"] = "ordered_sibling_weak_initial"
                    result["witness_leaf"] = leaf_index
                    return result
        self.leaves.append(candidate)
        result["inserted"] = True
        result["reason"] = "inserted"
        result["leaf_index"] = len(self.leaves) - 1
        return result

    def remove(self, sequence: Iterable[int]) -> bool:
        candidate = tuple(int(tid) for tid in sequence)
        try:
            self.leaves.remove(candidate)
            return True
        except ValueError:
            return False

    def mapping(self) -> dict[str, Any]:
        nodes: set[tuple[int, ...]] = {()}
        for leaf in self.leaves:
            for length in range(1, len(leaf) + 1):
                nodes.add(leaf[:length])
        return {
            "base_prefix": list(self.base_prefix),
            "sleep": sorted(self.sleep),
            "ready": sorted(self.ready),
            "ready_complete": self.ready_complete,
            "leaves": [list(leaf) for leaf in self.leaves],
            "nodes": [
                list(node)
                for node in sorted(nodes, key=lambda value: (
                    len(value), value
                ))
            ],
        }


def _wakeup_tree_certificate_digest(
    certificate: Mapping[str, Any],
) -> str:
    return _canonical_json_digest({
        key: value
        for key, value in certificate.items()
        if key != "certificate_sha256"
    })


def wakeup_tree_certificate(
    events: list[ScheduleEvent],
    *,
    current_prefix: Iterable[int] = (),
    sleep_sets: Mapping[str, Iterable[int]] | None = None,
    max_depth: int = 64,
    max_window: int = 32,
    max_prefixes: int = 256,
    max_events: int = 512,
) -> dict[str, Any]:
    """Build checked bounded wakeup trees from Source-DPOR candidates."""
    ordered = sorted(events, key=lambda event: event.seq)
    reduction = source_dpor_certificate(
        ordered,
        current_prefix=current_prefix,
        max_depth=max_depth,
        max_window=max_window,
        max_prefixes=max_prefixes,
        max_events=max_events,
    )
    ready_sets = runtime_ready_evidence(ordered)
    operational = operational_enabledness_certificate(ordered)
    configured_sleep = sleep_sets or {}
    by_base: dict[str, BoundedWakeupTree] = {}
    attempts: list[dict[str, Any]] = []
    for row in reduction["candidate_rows"]:
        base = tuple(int(tid) for tid in row["base_prefix"])
        base_key = _prefix_key(base)
        ready = ready_sets.get(len(base), {})
        tree = by_base.setdefault(base_key, BoundedWakeupTree(
            ordered,
            base,
            sleep=configured_sleep.get(base_key, ()),
            ready=ready.get("threads", ()),
            ready_complete=bool(ready.get("complete", False)),
            max_depth=max_depth,
            max_leaves=max_prefixes,
        ))
        result = tree.insert(row["wakeup_sequence"])
        attempts.append({
            "base_prefix": list(base),
            "conflict": row["conflict"],
            **result,
        })
    trees = [
        tree.mapping()
        for _, tree in sorted(by_base.items())
    ]
    ready_complete = bool(trees) and all(
        tree["ready_complete"] for tree in trees
    )
    certificate: dict[str, Any] = {
        "schema": WAKEUP_TREE_SCHEMA,
        "semantics": (
            "bounded-sc-deterministic-next-event-wakeup-tree-v1"
        ),
        "trace_digest": schedule_trace_digest(ordered),
        "trace_events": [_event_mapping(event) for event in ordered],
        "current_prefix": list(normalize_schedule_prefix(
            current_prefix, max_len=max_depth
        )),
        "bounds": {
            "max_depth": max(1, int(max_depth)),
            "max_window": max(1, int(max_window)),
            "max_prefixes": max(1, int(max_prefixes)),
            "max_events": max(0, int(max_events)),
        },
        "source_certificate_sha256": reduction["certificate_sha256"],
        "runtime_ready_evidence": [
            ready_sets[index] for index in sorted(ready_sets)
        ],
        "operational_enabledness_sha256": (
            operational["certificate_sha256"]
        ),
        "bounded_terminal_execution_witnessed": (
            operational["bounded_terminal_execution_witnessed"]
        ),
        "trees": trees,
        "insert_attempts": attempts,
        "abstract_wakeup_invariants_verified": True,
        "ready_evidence_complete": ready_complete,
        "optimality_claimed": False,
        "not_proved": [
            "scheduler_enforcement_of_completion_enabled_choices",
            "unbounded_optimal_dpor",
            "whole_program_operational_enabledness",
            "state_dependent_relation_beyond_observed_next_events",
        ],
    }
    certificate["certificate_sha256"] = (
        _wakeup_tree_certificate_digest(certificate)
    )
    return certificate


def verify_wakeup_tree_certificate(
    certificate: Mapping[str, Any],
) -> bool:
    try:
        if certificate.get("schema") != WAKEUP_TREE_SCHEMA:
            return False
        if certificate.get("certificate_sha256") != (
            _wakeup_tree_certificate_digest(certificate)
        ):
            return False
        raw_events = certificate.get("trace_events")
        bounds = certificate.get("bounds")
        if not isinstance(raw_events, list) or not isinstance(bounds, Mapping):
            return False
        events = [
            ScheduleEvent(
                seq=int(row["seq"]),
                tid=int(row["tid"]),
                op=str(row["op"]),
                obj=str(row["object"]),
                tags=tuple(str(tag) for tag in row.get("tags", ())),
            )
            for row in raw_events
            if isinstance(row, Mapping)
        ]
        if len(events) != len(raw_events):
            return False
        sleep_sets = {
            _prefix_key(tree.get("base_prefix", ())): tree.get("sleep", ())
            for tree in certificate.get("trees", ())
            if isinstance(tree, Mapping)
        }
        expected = wakeup_tree_certificate(
            events,
            current_prefix=certificate.get("current_prefix", ()),
            sleep_sets=sleep_sets,
            max_depth=int(bounds["max_depth"]),
            max_window=int(bounds["max_window"]),
            max_prefixes=int(bounds["max_prefixes"]),
            max_events=int(bounds["max_events"]),
        )
        return dict(certificate) == expected
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def _condpor_event_kind(event: ScheduleEvent) -> str:
    if event.op == "read":
        return "R"
    if event.op == "write":
        return "W"
    if event.op == "constraint":
        return "C"
    return "A"


def _transitive_successors(
    node_ids: Iterable[str],
    edges: Iterable[tuple[str, str]],
) -> dict[str, set[str]]:
    successors = {str(node_id): set() for node_id in node_ids}
    for source, target in edges:
        if source in successors and target in successors:
            successors[source].add(target)
    closure: dict[str, set[str]] = {}
    for node_id in successors:
        reached: set[str] = set()
        pending = list(successors[node_id])
        while pending:
            target = pending.pop()
            if target in reached:
                continue
            reached.add(target)
            pending.extend(successors[target] - reached)
        closure[node_id] = reached
    return closure


def _acyclic_relations(
    node_ids: Iterable[str],
    edges: Iterable[tuple[str, str]],
) -> bool:
    nodes = tuple(str(node_id) for node_id in node_ids)
    successors = {node_id: set() for node_id in nodes}
    indegree = {node_id: 0 for node_id in nodes}
    for source, target in edges:
        if source not in successors or target not in successors:
            return False
        if source == target:
            return False
        if target not in successors[source]:
            successors[source].add(target)
            indegree[target] += 1
    pending = [node_id for node_id, degree in indegree.items() if degree == 0]
    heapq.heapify(pending)
    visited = 0
    while pending:
        node_id = heapq.heappop(pending)
        visited += 1
        for target in sorted(successors[node_id]):
            indegree[target] -= 1
            if indegree[target] == 0:
                heapq.heappush(pending, target)
    return visited == len(nodes)


def _condpor_observed_read_from(
    read: Mapping[str, Any],
    writes: Iterable[Mapping[str, Any]],
    init_id: str,
) -> tuple[str, str]:
    tags = tuple(str(tag) for tag in read.get("tags", ()))
    explicit = _tag_value(tags, "rf")
    candidates = [
        write for write in writes
        if (
            str(write["object"]) == str(read["object"])
            and int(write["observed_position"])
            < int(read["observed_position"])
        )
    ]
    by_id = {str(write["id"]): write for write in candidates}
    if explicit in {"init", "-1", init_id}:
        return init_id, "explicit"
    if explicit in by_id:
        return explicit, "explicit"
    explicit_seq = _tag_uint(tags, "rf-seq")
    if explicit_seq is not None:
        for write in reversed(candidates):
            if int(write["seq"]) == explicit_seq:
                return str(write["id"]), "explicit-seq"
    read_value = _tag_value(tags, "value")
    if read_value:
        matching = [
            write for write in candidates
            if _tag_value(write.get("tags", ()), "value") == read_value
        ]
        if matching:
            return str(matching[-1]["id"]), "latest-matching-value"
    if candidates:
        return str(candidates[-1]["id"]), "latest-observed-write"
    return init_id, "initial"


def _condpor_replay_rows(
    events: list[ScheduleEvent],
    *,
    current_prefix: Iterable[int],
    max_depth: int,
    max_window: int,
    max_prefixes: int,
) -> dict[tuple[int, int], dict[str, Any]]:
    analysis = _source_replay_analysis(
        events,
        current_prefix,
        max_depth=max_depth,
        max_window=max_window,
        max_prefixes=max_prefixes,
    )
    rows: dict[tuple[int, int], dict[str, Any]] = {}
    for row in analysis["candidate_rows"]:
        conflict = row["conflict"]
        rows[(int(conflict["left_seq"]), int(conflict["right_seq"]))] = row
    return rows


def _condpor_maximal_extension(
    nodes: list[dict[str, Any]],
    po_edges: list[tuple[str, str]],
    rf_edges: list[tuple[str, str]],
    co_orders: Mapping[str, list[str]],
    *,
    read_id: str,
    write_id: str,
    deleted: set[str],
) -> dict[str, Any]:
    """Restore an observed bounded suffix with deterministic maximal choices."""
    by_id = {str(node["id"]): node for node in nodes}
    original_causal = _transitive_successors(
        by_id, list(po_edges) + list(rf_edges)
    )
    read_object = str(by_id[read_id]["object"])
    regeneration_frontier: list[str] = []
    withheld: set[str] = set()
    for node_id in deleted:
        node = by_id[node_id]
        if str(node["kind"]) != "C":
            continue
        tags = tuple(str(tag) for tag in node.get("tags", ()))
        if not _constraint_depends_on_read(
            tags, read_id, read_object
        ):
            continue
        regeneration_frontier.append(node_id)
        withheld.add(node_id)
        withheld.update(original_causal.get(node_id, set()))
    withheld.intersection_update(deleted)
    restorable = deleted - withheld
    retained = {
        node_id for node_id in by_id
        if node_id not in deleted
    }
    retained_rf = [
        (source, target)
        for source, target in rf_edges
        if (
            source in retained
            and target in retained
            and target != read_id
        )
    ]
    retained_rf.append((write_id, read_id))
    current_co: dict[str, list[str]] = {}
    for obj, order in co_orders.items():
        current_co[obj] = [
            node_id for node_id in order if node_id in retained
        ]

    extension_order = sorted(
        restorable,
        key=lambda node_id: (
            int(by_id[node_id].get("observed_position", -1)),
            node_id,
        ),
    )
    choices: list[dict[str, Any]] = []
    extension_rf: list[tuple[str, str]] = []
    for node_id in extension_order:
        node = by_id[node_id]
        kind = str(node["kind"])
        obj = str(node["object"])
        if kind == "R":
            sources = current_co.get(obj, ())
            source = sources[-1] if sources else f"init:{obj}"
            extension_rf.append((source, node_id))
            choices.append({
                "event": node_id,
                "kind": "read_from",
                "source": source,
                "rule": "co_maximal_current_write",
            })
        elif kind == "W":
            current_co.setdefault(obj, [f"init:{obj}"]).append(node_id)
            choices.append({
                "event": node_id,
                "kind": "coherence",
                "after": list(current_co[obj][:-1]),
                "rule": "co_after_all_current_writes",
            })
        elif kind == "C":
            tags = tuple(str(tag) for tag in node.get("tags", ()))
            model_outcome = _tag_value(tags, "model-outcome")
            observed_outcome = _tag_value(tags, "outcome")
            choices.append({
                "event": node_id,
                "kind": "constraint",
                "outcome": model_outcome or observed_outcome,
                "rule": (
                    "deterministic_model_tiebreak"
                    if model_outcome
                    else "observed_outcome_fallback"
                ),
                "model_witnessed": bool(model_outcome),
            })
        else:
            choices.append({
                "event": node_id,
                "kind": "action",
                "rule": "observed_thread_successor",
            })

    final_rf = retained_rf + extension_rf
    active_nodes = set(by_id) - withheld
    final_po = [
        (source, target)
        for source, target in po_edges
        if source in active_nodes and target in active_nodes
    ]
    final_rf = [
        (source, target)
        for source, target in final_rf
        if source in active_nodes and target in active_nodes
    ]
    final_co = [
        (left, right)
        for order in current_co.values()
        for left, right in zip(order, order[1:])
    ]
    causal_edges = final_po + final_rf
    return {
        "deleted_events": sorted(
            deleted,
            key=lambda node_id: (
                int(by_id[node_id].get("observed_position", -1)),
                node_id,
            ),
        ),
        "extension_order": extension_order,
        "regeneration_required": bool(withheld),
        "regeneration_frontier": sorted(
            regeneration_frontier,
            key=lambda node_id: (
                int(by_id[node_id].get("observed_position", -1)),
                node_id,
            ),
        ),
        "withheld_path_dependent_events": sorted(
            withheld,
            key=lambda node_id: (
                int(by_id[node_id].get("observed_position", -1)),
                node_id,
            ),
        ),
        "choices": choices,
        "read_from_edges": [
            {"source": source, "target": target}
            for source, target in final_rf
        ],
        "coherence_edges": [
            {"source": source, "target": target}
            for source, target in final_co
        ],
        "causal_acyclic": _acyclic_relations(
            active_nodes, causal_edges
        ),
        "model_witnessed_constraint_count": sum(
            1 for choice in choices
            if (
                choice["kind"] == "constraint"
                and choice["model_witnessed"]
            )
        ),
        "observed_constraint_fallback_count": sum(
            1 for choice in choices
            if (
                choice["kind"] == "constraint"
                and not choice["model_witnessed"]
            )
        ),
    }


def _condpor_graph_digest(certificate: Mapping[str, Any]) -> str:
    return _canonical_json_digest({
        key: value
        for key, value in certificate.items()
        if key != "certificate_sha256"
    })


def condpor_execution_graph_certificate(
    events: list[ScheduleEvent],
    *,
    current_prefix: Iterable[int] = (),
    max_depth: int = 64,
    max_window: int = 32,
    max_prefixes: int = 256,
    max_events: int = 512,
) -> dict[str, Any]:
    """Build a checked bounded ConDPOR-style graph and backward revisits."""
    depth = max(1, int(max_depth))
    window = max(1, int(max_window))
    prefix_cap = max(1, int(max_prefixes))
    event_cap = max(0, int(max_events))
    ordered = sorted(events, key=lambda event: event.seq)
    operational = operational_enabledness_certificate(ordered)
    graph_events = [
        event for event in ordered
        if event.op != "ready"
    ]
    modeled = graph_events[:event_cap]
    occurrences: dict[int, int] = {}
    nodes: list[dict[str, Any]] = []
    by_thread: dict[int, list[str]] = {}
    for position, event in enumerate(modeled):
        occurrence = occurrences.get(event.tid, 0)
        occurrences[event.tid] = occurrence + 1
        node_id = f"t{event.tid}:{occurrence}"
        by_thread.setdefault(event.tid, []).append(node_id)
        nodes.append({
            "id": node_id,
            "kind": _condpor_event_kind(event),
            "tid": event.tid,
            "thread_index": occurrence,
            "op": event.op,
            "object": event.obj,
            "tags": list(event.tags),
            "seq": event.seq,
            "observed_position": position,
            "schedulable": event.schedulable,
        })

    memory_objects = sorted({
        str(node["object"]) for node in nodes
        if node["kind"] in {"R", "W"}
    })
    init_nodes = [{
        "id": f"init:{obj}",
        "kind": "I",
        "tid": -1,
        "thread_index": -1,
        "op": "init",
        "object": obj,
        "tags": [],
        "seq": -1,
        "observed_position": -1,
        "schedulable": False,
    } for obj in memory_objects]
    all_nodes = init_nodes + nodes
    by_id = {str(node["id"]): node for node in all_nodes}

    po_edges = [
        (left, right)
        for thread_nodes in by_thread.values()
        for left, right in zip(thread_nodes, thread_nodes[1:])
    ]
    writes = [node for node in nodes if node["kind"] == "W"]
    reads = [node for node in nodes if node["kind"] == "R"]
    rf_edges: list[tuple[str, str]] = []
    rf_rows: list[dict[str, Any]] = []
    for read in reads:
        init_id = f"init:{read['object']}"
        source, inference = _condpor_observed_read_from(
            read, writes, init_id
        )
        rf_edges.append((source, str(read["id"])))
        rf_rows.append({
            "read": read["id"],
            "source": source,
            "inference": inference,
        })

    co_orders: dict[str, list[str]] = {}
    for obj in memory_objects:
        co_orders[obj] = [f"init:{obj}"] + [
            str(write["id"]) for write in writes
            if str(write["object"]) == obj
        ]
    co_edges = [
        (left, right)
        for order in co_orders.values()
        for left, right in zip(order, order[1:])
    ]
    causal_edges = po_edges + rf_edges
    causal_acyclic = _acyclic_relations(by_id, causal_edges)
    causal_successors = _transitive_successors(by_id, causal_edges)

    replay_rows = _condpor_replay_rows(
        ordered,
        current_prefix=current_prefix,
        max_depth=depth,
        max_window=window,
        max_prefixes=prefix_cap,
    )
    eligible_pairs = set(replay_rows)
    revisits: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen_revisits: set[str] = set()
    for write in writes:
        for read in reads:
            if (
                str(read["object"]) != str(write["object"])
                or int(read["observed_position"])
                >= int(write["observed_position"])
            ):
                continue
            pair = (int(read["seq"]), int(write["seq"]))
            rejection = ""
            if pair not in eligible_pairs:
                rejection = "not_unordered_unprotected_memory_conflict"
            elif str(write["id"]) in causal_successors[str(read["id"])]:
                rejection = "read_causally_before_write"
            if rejection:
                rejected.append({
                    "read": read["id"],
                    "write": write["id"],
                    "reason": rejection,
                })
                continue
            deleted = set(causal_successors[str(read["id"])])
            extension = _condpor_maximal_extension(
                all_nodes,
                po_edges,
                rf_edges,
                co_orders,
                read_id=str(read["id"]),
                write_id=str(write["id"]),
                deleted=deleted,
            )
            if not extension["causal_acyclic"]:
                rejected.append({
                    "read": read["id"],
                    "write": write["id"],
                    "reason": "revisit_causal_cycle",
                })
                continue
            source_row = replay_rows[pair]
            identity_payload = {
                "nodes": [{
                    "id": node["id"],
                    "kind": node["kind"],
                    "op": node["op"],
                    "object": node["object"],
                    "tags": node["tags"],
                } for node in sorted(
                    all_nodes, key=lambda row: str(row["id"])
                )],
                "program_order": sorted(po_edges),
                "read_from": sorted(
                    (str(row["source"]), str(row["target"]))
                    for row in extension["read_from_edges"]
                ),
                "coherence": sorted(
                    (str(row["source"]), str(row["target"]))
                    for row in extension["coherence_edges"]
                ),
                "extension_choices": extension["choices"],
            }
            revisit_id = _canonical_json_digest(identity_payload)
            if revisit_id in seen_revisits:
                rejected.append({
                    "read": read["id"],
                    "write": write["id"],
                    "reason": "duplicate_revisit_identity",
                    "revisit_sha256": revisit_id,
                })
                continue
            seen_revisits.add(revisit_id)
            revisits.append({
                "read": read["id"],
                "write": write["id"],
                "object": read["object"],
                "old_read_from": next(
                    row["source"] for row in rf_rows
                    if row["read"] == read["id"]
                ),
                "new_read_from": write["id"],
                "base_prefix": list(source_row["base_prefix"]),
                "wakeup_sequence": list(source_row["wakeup_sequence"]),
                "replay_prefix": list(source_row["prefix"]),
                "extension": extension,
                "revisit_sha256": revisit_id,
            })
            if len(revisits) >= prefix_cap:
                break
        if len(revisits) >= prefix_cap:
            break

    graph_identity = {
        "nodes": [{
            "id": node["id"],
            "kind": node["kind"],
            "op": node["op"],
            "object": node["object"],
            "tags": node["tags"],
        } for node in sorted(all_nodes, key=lambda row: str(row["id"]))],
        "program_order": sorted(po_edges),
        "read_from": sorted(rf_edges),
        "coherence": sorted(co_edges),
    }
    certificate: dict[str, Any] = {
        "schema": CONDPOR_GRAPH_SCHEMA,
        "semantics": "bounded-observed-control-flow-condpor-graph-v1",
        "trace_digest": schedule_trace_digest(ordered),
        "trace_events": [_event_mapping(event) for event in ordered],
        "current_prefix": list(normalize_schedule_prefix(
            current_prefix, max_len=depth
        )),
        "bounds": {
            "max_depth": depth,
            "max_window": window,
            "max_prefixes": prefix_cap,
            "max_events": event_cap,
        },
        "nodes": all_nodes,
        "relations": {
            "program_order": [
                {"source": source, "target": target}
                for source, target in po_edges
            ],
            "read_from": [
                {"source": source, "target": target}
                for source, target in rf_edges
            ],
            "coherence": [
                {"source": source, "target": target}
                for source, target in co_edges
            ],
            "add_order": [
                str(node["id"]) for node in nodes
            ],
        },
        "read_from_inference": rf_rows,
        "causal_acyclic": causal_acyclic,
        "execution_graph_sha256": _canonical_json_digest(
            graph_identity
        ),
        "operational_enabledness_sha256": (
            operational["certificate_sha256"]
        ),
        "bounded_terminal_execution_witnessed": (
            operational["bounded_terminal_execution_witnessed"]
        ),
        "revisits": revisits,
        "rejected_revisits": rejected,
        "revisit_count": len(revisits),
        "replay_prefixes": [
            row["replay_prefix"] for row in revisits
        ],
        "truncated": len(graph_events) > len(modeled),
        "sound_complete_optimal_claimed": False,
        "proved_scope": [
            "bounded_po_rf_causality",
            "backward_revisit_cycle_rejection",
            "deterministic_bounded_maximal_extension",
            "path_dependent_suffix_withholding_before_replay",
            "revisit_identity_deduplication",
        ],
        "not_proved": [
            "unobserved_path_dependent_event_existence_after_replay_frontier",
            "scheduler_enforcement_of_completion_enabled_choices",
            "complete_memory_model_consistency_oracle",
            "unbounded_condpor_soundness_completeness_optimality",
            "unique_constraint_extension_without_model_outcome_tags",
        ],
    }
    certificate["certificate_sha256"] = _condpor_graph_digest(certificate)
    return certificate


def verify_condpor_execution_graph_certificate(
    certificate: Mapping[str, Any],
) -> bool:
    """Recompute all bounded ConDPOR graph relations and revisit choices."""
    try:
        if certificate.get("schema") != CONDPOR_GRAPH_SCHEMA:
            return False
        if certificate.get("certificate_sha256") != (
            _condpor_graph_digest(certificate)
        ):
            return False
        raw_events = certificate.get("trace_events")
        bounds = certificate.get("bounds")
        if not isinstance(raw_events, list) or not isinstance(bounds, Mapping):
            return False
        events = [
            ScheduleEvent(
                seq=int(row["seq"]),
                tid=int(row["tid"]),
                op=str(row["op"]),
                obj=str(row["object"]),
                tags=tuple(str(tag) for tag in row.get("tags", ())),
            )
            for row in raw_events
            if isinstance(row, Mapping)
        ]
        if len(events) != len(raw_events):
            return False
        expected = condpor_execution_graph_certificate(
            events,
            current_prefix=certificate.get("current_prefix", ()),
            max_depth=int(bounds["max_depth"]),
            max_window=int(bounds["max_window"]),
            max_prefixes=int(bounds["max_prefixes"]),
            max_events=int(bounds["max_events"]),
        )
        return dict(certificate) == expected
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def _source_replay_analysis(
    events: list[ScheduleEvent],
    current_prefix: Iterable[int] = (),
    *,
    max_depth: int,
    max_window: int,
    max_prefixes: int,
) -> dict[str, Any]:
    """Compute bounded source sets and causal wakeup sequences."""
    depth = max(1, int(max_depth))
    window = max(1, int(max_window))
    cap = max(1, int(max_prefixes))
    ordered_events = sorted(events, key=lambda event: event.seq)
    schedulable = [event for event in ordered_events if event.schedulable]
    tids = [event.tid for event in schedulable]
    positions = {event.seq: index for index, event in enumerate(schedulable)}
    points = {
        point.event.seq: point
        for point in annotate_schedule(ordered_events)
    }
    current = normalize_schedule_prefix(current_prefix, max_len=depth)
    candidates: list[tuple[tuple[int, ...], ScheduleConflict]] = []
    candidate_rows: list[dict[str, Any]] = []
    source_sets: dict[str, dict[str, Any]] = {}
    seen_prefixes: set[str] = set()
    dropped_depth = 0

    for conflict in classify_schedule_conflicts(
        ordered_events,
        max_window=window,
    ):
        left_index = positions.get(conflict.left_seq)
        right_index = positions.get(conflict.right_seq)
        if left_index is None or right_index is None:
            continue
        if left_index >= depth:
            dropped_depth += 1
            continue
        right_point = points.get(conflict.right_seq)
        causal_events: list[ScheduleEvent] = []
        if right_point is not None:
            for event in schedulable[left_index + 1:right_index]:
                point = points.get(event.seq)
                if point is not None and happens_before(point, right_point):
                    causal_events.append(event)
        wakeup = tuple(
            [event.tid for event in causal_events]
            + [conflict.right_tid]
        )
        base = tuple(tids[:left_index])
        if len(base) + len(wakeup) > depth:
            dropped_depth += 1
            continue
        prefix = base + wakeup
        if (
            not prefix
            or prefix == tuple(tids[:len(prefix)])
            or prefix == current
        ):
            continue

        base_key = _prefix_key(base)
        source_set = source_sets.setdefault(base_key, {
            "base_prefix": list(base),
            "threads": [],
            "wakeup_sequences": [],
        })
        if wakeup and wakeup[0] not in source_set["threads"]:
            source_set["threads"].append(wakeup[0])
        wakeup_row = {
            "sequence": list(wakeup),
            "target_seq": conflict.right_seq,
            "conflict": conflict.to_mapping(),
        }
        if wakeup_row not in source_set["wakeup_sequences"]:
            source_set["wakeup_sequences"].append(wakeup_row)

        prefix_key = _prefix_key(prefix)
        if prefix_key in seen_prefixes:
            continue
        seen_prefixes.add(prefix_key)
        candidates.append((prefix, conflict))
        candidate_rows.append({
            "base_prefix": list(base),
            "wakeup_sequence": list(wakeup),
            "prefix": list(prefix),
            "conflict": conflict.to_mapping(),
        })
        if len(candidates) >= cap:
            break

    normalized_sets = []
    for source_set in source_sets.values():
        source_set["threads"].sort()
        source_set["wakeup_sequences"].sort(key=lambda row: (
            row["sequence"],
            row["target_seq"],
        ))
        normalized_sets.append(source_set)
    normalized_sets.sort(key=lambda row: row["base_prefix"])
    return {
        "candidates": tuple(candidates),
        "candidate_rows": candidate_rows,
        "source_sets": normalized_sets,
        "dropped_depth_count": dropped_depth,
    }


def _source_replay_candidates(
    events: list[ScheduleEvent],
    current_prefix: Iterable[int] = (),
    *,
    max_depth: int = 64,
    max_window: int = 32,
    max_prefixes: int = 256,
) -> tuple[tuple[tuple[int, ...], ScheduleConflict], ...]:
    """Generate replay prefixes together with their source conflicts."""
    analysis = _source_replay_analysis(
        events,
        current_prefix,
        max_depth=max_depth,
        max_window=max_window,
        max_prefixes=max_prefixes,
    )
    return analysis["candidates"]


def propose_source_replay_prefixes(
    events: list[ScheduleEvent],
    current_prefix: Iterable[int] = (),
    *,
    max_depth: int = 64,
    max_window: int = 32,
    max_prefixes: int = 256,
) -> tuple[tuple[int, ...], ...]:
    """Generate bounded source-style replay prefixes from SC conflicts."""
    return tuple(
        prefix
        for prefix, _ in _source_replay_candidates(
            events,
            current_prefix,
            max_depth=max_depth,
            max_window=max_window,
            max_prefixes=max_prefixes,
        )
    )


def _source_dpor_certificate_digest(
    certificate: Mapping[str, Any],
) -> str:
    return _canonical_json_digest({
        key: value
        for key, value in certificate.items()
        if key != "certificate_sha256"
    })


def source_dpor_certificate(
    events: list[ScheduleEvent],
    *,
    current_prefix: Iterable[int] = (),
    max_depth: int = 64,
    max_window: int = 32,
    max_prefixes: int = 256,
    max_events: int = 512,
) -> dict[str, Any]:
    """Construct a reproducible bounded Source-DPOR reduction certificate."""
    depth = max(1, int(max_depth))
    window = max(1, int(max_window))
    prefix_cap = max(1, int(max_prefixes))
    event_cap = max(0, int(max_events))
    ordered = sorted(events, key=lambda event: event.seq)
    analysis = _source_replay_analysis(
        ordered,
        current_prefix,
        max_depth=depth,
        max_window=window,
        max_prefixes=prefix_cap,
    )
    graph = _bounded_dependency_graph(ordered, max_events=event_cap)
    certificate: dict[str, Any] = {
        "schema": SOURCE_DPOR_SCHEMA,
        "semantics": "bounded-sc-observed-trace-source-sets-v1",
        "scope": "observed_schedulable_events",
        "optimality_claimed": False,
        "trace_digest": schedule_trace_digest(ordered),
        "current_prefix": list(normalize_schedule_prefix(
            current_prefix,
            max_len=depth,
        )),
        "bounds": {
            "max_depth": depth,
            "max_window": window,
            "max_prefixes": prefix_cap,
            "max_events": event_cap,
        },
        "assumptions": [
            "sequential_consistency",
            "deterministic_next_transition_per_thread_and_input",
            "runtime_trace_contains_all_modeled_scheduling_points",
        ],
        "not_proved": [
            "runtime_enabledness",
            "unbounded_source_dpor_completeness",
            "optimal_dpor_wakeup_tree_property",
            "weak_memory_consistency",
        ],
        "event_count": len(ordered),
        "trace_events": [_event_mapping(event) for event in ordered],
        "dependency_graph": graph,
        "source_sets": analysis["source_sets"],
        "candidate_rows": analysis["candidate_rows"],
        "replay_prefixes": [
            list(prefix) for prefix, _ in analysis["candidates"]
        ],
        "dropped_depth_count": analysis["dropped_depth_count"],
    }
    certificate["certificate_sha256"] = (
        _source_dpor_certificate_digest(certificate)
    )
    return certificate


def verify_source_dpor_certificate(
    certificate: Mapping[str, Any],
) -> bool:
    """Recompute and validate a bounded Source-DPOR certificate."""
    try:
        if certificate.get("schema") != SOURCE_DPOR_SCHEMA:
            return False
        if certificate.get("certificate_sha256") != (
            _source_dpor_certificate_digest(certificate)
        ):
            return False
        raw_events = certificate.get("trace_events")
        bounds = certificate.get("bounds")
        if not isinstance(raw_events, list) or not isinstance(bounds, Mapping):
            return False
        events: list[ScheduleEvent] = []
        for row in raw_events:
            if not isinstance(row, Mapping):
                return False
            events.append(ScheduleEvent(
                seq=int(row["seq"]),
                tid=int(row["tid"]),
                op=str(row["op"]),
                obj=str(row["object"]),
                tags=tuple(str(tag) for tag in row.get("tags", ())),
            ))
        expected = source_dpor_certificate(
            events,
            current_prefix=certificate.get("current_prefix", ()),
            max_depth=int(bounds["max_depth"]),
            max_window=int(bounds["max_window"]),
            max_prefixes=int(bounds["max_prefixes"]),
            max_events=int(bounds["max_events"]),
        )
        return dict(certificate) == expected
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def _smt_any(terms: Iterable[str]) -> str:
    values = tuple(terms)
    if not values:
        return "false"
    if len(values) == 1:
        return values[0]
    return f"(or {' '.join(values)})"


def _smt_all(terms: Iterable[str]) -> str:
    values = tuple(terms)
    if not values:
        return "true"
    if len(values) == 1:
        return values[0]
    return f"(and {' '.join(values)})"


def _smt_integer(value: int) -> str:
    normalized = int(value)
    if normalized < 0:
        return f"(- {abs(normalized)})"
    return str(normalized)


def _normalize_schedule_order_encoding(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    if value in SCHEDULE_SMT_ORDER_ENCODINGS:
        return value
    if value in {"permutation", "total", "legacy", "all-different"}:
        return "permutation"
    return "partial"


def _schedule_sync_state_context(
    events: list[ScheduleEvent],
    modeled_events: list[ScheduleEvent],
    *,
    enabled: bool,
    max_events: int,
    order_encoding: str,
) -> tuple[str, str, dict[str, Any]]:
    """Encode lock, condition, and thread lifecycle operational state."""
    lifecycle_all = [
        event for event in events if event.op in SYNC_LIFECYCLE_OPS
    ]
    event_cap = min(512, max(2, int(max_events)))
    lifecycle = lifecycle_all[:event_cap]
    order_encoding = _normalize_schedule_order_encoding(order_encoding)
    lifecycle_event_rows = []
    for index, event in enumerate(lifecycle):
        row = _event_mapping(event)
        row["index"] = index
        lifecycle_event_rows.append(row)
    metadata: dict[str, Any] = {
        "enabled": bool(enabled),
        "semantics": "lock-condition-thread-operational-state-v5",
        "lifecycle_event_count": len(lifecycle_all),
        "modeled_lifecycle_event_count": len(lifecycle),
        "order_encoding": order_encoding,
        "linear_extension_semantics": order_encoding == "partial",
        "order_bound_constraint_count": 0,
        "order_distinct_constraint_count": 0,
        "order_link_encoding": (
            "scaled-anchor" if order_encoding == "partial"
            else "pairwise"
        ),
        "order_link_constraint_count": 0,
        "order_link_semantic_pair_count": 0,
        "controlled_anchor_count": 0,
        "anchor_stride": len(lifecycle) + 1,
        "complete_section_count": 0,
        "open_section_count": 0,
        "unmatched_outcome_count": 0,
        "unmatched_unlock_count": 0,
        "pending_attempt_count": 0,
        "exclusion_constraint_count": 0,
        "lazy_refinement_count": 0,
        "lazy_refinements": [],
        "trylock_failure_count": 0,
        "trylock_assumptions": [],
        "trylock_failures": [],
        "condition_wait_count": 0,
        "complete_condition_wait_count": 0,
        "successful_condition_wait_count": 0,
        "timed_out_condition_wait_count": 0,
        "condition_wake_assumptions": [],
        "condition_waits": [],
        "wake_witness_uniqueness_count": 0,
        "unmatched_condition_event_count": 0,
        "thread_lifecycle_count": 0,
        "complete_thread_lifecycle_count": 0,
        "successful_create_count": 0,
        "failed_create_count": 0,
        "thread_spawn_constraint_count": 0,
        "thread_lifecycles": [],
        "join_count": 0,
        "successful_join_count": 0,
        "failed_join_count": 0,
        "cancelled_join_count": 0,
        "join_completion_constraint_count": 0,
        "joins": [],
        "detach_count": 0,
        "successful_detach_count": 0,
        "failed_detach_count": 0,
        "detaches": [],
        "cancel_count": 0,
        "successful_cancel_count": 0,
        "failed_cancel_count": 0,
        "cancellations": [],
        "thread_retire_count": 0,
        "thread_retirement_constraint_count": 0,
        "thread_retirements": [],
        "unmatched_thread_event_count": 0,
        "sections": [],
        "order_ir": {
            "schema": SCHEDULE_ORDER_IR_SCHEMA,
            "event_count": len(lifecycle),
            "events": lifecycle_event_rows,
            "fixed_edges": [],
            "choice_constraints": [],
            "controlled_anchors": [],
            "link_encoding": (
                "scaled-anchor" if order_encoding == "partial"
                else "pairwise"
            ),
            "anchor_stride": len(lifecycle) + 1,
        },
        "truncated": len(lifecycle_all) > event_cap,
        "max_events": event_cap,
    }
    if not enabled or not lifecycle:
        return "", "", metadata

    pending: dict[tuple[int, str], list[dict[str, Any]]] = {}
    active: dict[tuple[int, str], list[int]] = {}
    sections: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    condition_waits: list[dict[str, Any]] = []
    waits_by_thread: dict[int, list[int]] = {}
    thread_lifecycles: list[dict[str, Any]] = []
    threads_by_object: dict[str, list[int]] = {}
    joins: list[dict[str, Any]] = []
    joins_by_thread_object: dict[tuple[int, str], list[int]] = {}
    detaches: list[dict[str, Any]] = []
    detaches_by_thread_object: dict[tuple[int, str], list[int]] = {}
    cancellations: list[dict[str, Any]] = []
    cancellations_by_thread_object: dict[
        tuple[int, str], list[int]
    ] = {}
    retirements: list[dict[str, Any]] = []
    unmatched_outcomes = 0
    unmatched_unlocks = 0
    unmatched_condition_events = 0
    unmatched_thread_events = 0
    fixed_order_edges: list[dict[str, Any]] = []
    order_choices: list[dict[str, Any]] = []

    def record_edge(
        before: int,
        after: int,
        kind: str,
        name: str,
    ) -> None:
        fixed_order_edges.append({
            "before": int(before),
            "after": int(after),
            "kind": kind,
            "name": name,
        })

    def open_section(
        event: ScheduleEvent,
        index: int,
        mode: str,
        *,
        attempt_seq: int | None = None,
        attempt_index: int | None = None,
        origin: str = "lock",
    ) -> int:
        section_index = len(sections)
        sections.append({
            "id": section_index,
            "tid": event.tid,
            "object": event.obj,
            "mode": mode,
            "origin": origin,
            "attempt_seq": (
                event.seq if attempt_seq is None else attempt_seq
            ),
            "attempt_index": (
                index if attempt_index is None else attempt_index
            ),
            "acquire_seq": event.seq,
            "acquire_index": index,
            "release_seq": None,
            "release_index": None,
        })
        active.setdefault((event.tid, event.obj), []).append(section_index)
        return section_index

    def close_section(
        event: ScheduleEvent,
        index: int,
        allowed_modes: set[str],
    ) -> int | None:
        nonlocal unmatched_unlocks
        active_sections = active.get((event.tid, event.obj), [])
        match = None
        for position in range(len(active_sections) - 1, -1, -1):
            if sections[active_sections[position]]["mode"] in allowed_modes:
                match = position
                break
        if match is None:
            unmatched_unlocks += 1
            return None
        section_index = active_sections.pop(match)
        sections[section_index]["release_seq"] = event.seq
        sections[section_index]["release_index"] = index
        return section_index

    def latest_wait(
        tid: int,
        predicate: Any,
    ) -> dict[str, Any] | None:
        for wait_index in reversed(waits_by_thread.get(tid, [])):
            wait = condition_waits[wait_index]
            if predicate(wait):
                return wait
        return None

    for index, event in enumerate(lifecycle):
        key = (event.tid, event.obj)
        if event.op == "create":
            thread_index = len(thread_lifecycles)
            try:
                child_tid = int(event.obj, 0)
            except ValueError:
                child_tid = -1
            thread_lifecycles.append({
                "id": thread_index,
                "parent_tid": event.tid,
                "child_tid": child_tid,
                "object": event.obj,
                "create_seq": event.seq,
                "create_index": index,
                "detached_at_create": "detached=1" in event.tags,
                "create_mapped": None,
                "create_outcome_seq": None,
                "create_outcome_index": None,
                "create_outcome_op": "",
                "start_seq": None,
                "start_index": None,
                "start_tid": None,
                "exit_seq": None,
                "exit_index": None,
                "exit_tid": None,
            })
            threads_by_object.setdefault(event.obj, []).append(thread_index)
            continue
        if event.op in {"create_success", "create_fail"}:
            match = None
            for thread_index in reversed(
                threads_by_object.get(event.obj, [])
            ):
                thread = thread_lifecycles[thread_index]
                if (thread["parent_tid"] == event.tid
                        and thread["create_outcome_index"] is None):
                    match = thread
                    break
            if match is None:
                unmatched_thread_events += 1
                continue
            match["create_outcome_seq"] = event.seq
            match["create_outcome_index"] = index
            match["create_outcome_op"] = event.op
            if event.op == "create_success":
                match["create_mapped"] = "mapped=0" not in event.tags
            continue
        if event.op in {"thread_start", "thread_exit"}:
            match = None
            for thread_index in reversed(
                threads_by_object.get(event.obj, [])
            ):
                thread = thread_lifecycles[thread_index]
                field = (
                    "start_index"
                    if event.op == "thread_start"
                    else "exit_index"
                )
                if (thread[field] is None
                        and thread["create_outcome_op"] != "create_fail"):
                    match = thread
                    break
            if match is None:
                unmatched_thread_events += 1
                continue
            prefix = "start" if event.op == "thread_start" else "exit"
            match[f"{prefix}_seq"] = event.seq
            match[f"{prefix}_index"] = index
            match[f"{prefix}_tid"] = event.tid
            continue
        if event.op == "thread_retire":
            cause = ""
            for tag in event.tags:
                if tag.startswith("cause="):
                    cause = tag.split("=", 1)[1]
                    break
            retirements.append({
                "id": len(retirements),
                "object": event.obj,
                "retire_seq": event.seq,
                "retire_index": index,
                "retire_tid": event.tid,
                "cause": cause,
                "target_thread_id": None,
                "target_exit_index": None,
                "trigger_index": None,
            })
            continue
        if event.op == "join":
            join_index = len(joins)
            mapped = "mapped=0" not in event.tags
            joins.append({
                "id": join_index,
                "joiner_tid": event.tid,
                "target_object": event.obj,
                "mapped": mapped,
                "join_seq": event.seq,
                "join_index": index,
                "outcome_seq": None,
                "outcome_index": None,
                "outcome_op": "",
                "target_thread_id": None,
                "target_exit_index": None,
            })
            joins_by_thread_object.setdefault(key, []).append(join_index)
            continue
        if event.op in {
            "join_success", "join_fail", "join_cancelled"
        }:
            match = None
            for join_index in reversed(
                joins_by_thread_object.get(key, [])
            ):
                join = joins[join_index]
                if join["outcome_index"] is None:
                    match = join
                    break
            if match is None:
                unmatched_thread_events += 1
                continue
            match["outcome_seq"] = event.seq
            match["outcome_index"] = index
            match["outcome_op"] = event.op
            continue
        if event.op == "detach":
            detach_index = len(detaches)
            detaches.append({
                "id": detach_index,
                "caller_tid": event.tid,
                "target_object": event.obj,
                "mapped": "mapped=0" not in event.tags,
                "detach_seq": event.seq,
                "detach_index": index,
                "outcome_seq": None,
                "outcome_index": None,
                "outcome_op": "",
                "target_thread_id": None,
            })
            detaches_by_thread_object.setdefault(
                key, []
            ).append(detach_index)
            continue
        if event.op in {"detach_success", "detach_fail"}:
            match = None
            for detach_index in reversed(
                detaches_by_thread_object.get(key, [])
            ):
                detach = detaches[detach_index]
                if detach["outcome_index"] is None:
                    match = detach
                    break
            if match is None:
                unmatched_thread_events += 1
                continue
            match["outcome_seq"] = event.seq
            match["outcome_index"] = index
            match["outcome_op"] = event.op
            continue
        if event.op == "cancel":
            cancel_index = len(cancellations)
            cancellations.append({
                "id": cancel_index,
                "caller_tid": event.tid,
                "target_object": event.obj,
                "mapped": "mapped=0" not in event.tags,
                "cancel_seq": event.seq,
                "cancel_index": index,
                "outcome_seq": None,
                "outcome_index": None,
                "outcome_op": "",
                "target_thread_id": None,
            })
            cancellations_by_thread_object.setdefault(
                key, []
            ).append(cancel_index)
            continue
        if event.op in {"cancel_success", "cancel_fail"}:
            match = None
            for cancel_index in reversed(
                cancellations_by_thread_object.get(key, [])
            ):
                cancellation = cancellations[cancel_index]
                if cancellation["outcome_index"] is None:
                    match = cancellation
                    break
            if match is None:
                unmatched_thread_events += 1
                continue
            match["outcome_seq"] = event.seq
            match["outcome_index"] = index
            match["outcome_op"] = event.op
            continue
        if event.op in SYNC_ATTEMPT_MODES:
            pending.setdefault(key, []).append({
                "index": index,
                "seq": event.seq,
                "mode": SYNC_ATTEMPT_MODES[event.op],
                "op": event.op,
            })
            continue
        if event.op in SYNC_OUTCOME_OPS:
            attempts = pending.get(key, [])
            if not attempts:
                unmatched_outcomes += 1
                continue
            attempt = attempts.pop()
            if event.op != "acquire":
                failures.append({
                    "tid": event.tid,
                    "object": event.obj,
                    "mode": attempt["mode"],
                    "attempt_index": attempt["index"],
                    "attempt_seq": attempt["seq"],
                    "outcome_index": index,
                    "outcome_seq": event.seq,
                    "outcome_op": event.op,
                })
                continue
            open_section(
                event,
                index,
                attempt["mode"],
                attempt_seq=attempt["seq"],
                attempt_index=attempt["index"],
            )
            continue
        if event.op in SYNC_UNLOCK_OPS:
            modes = (
                {"mutex_write"}
                if event.op == "unlock"
                else {"rw_read", "rw_write"}
            )
            close_section(event, index, modes)
            continue
        if event.op == "wait":
            wait_index = len(condition_waits)
            condition_waits.append({
                "id": wait_index,
                "tid": event.tid,
                "condition": event.obj,
                "wait_seq": event.seq,
                "wait_index": index,
                "mutex": "",
                "release_seq": None,
                "release_index": None,
                "released_section_id": None,
                "reacquire_seq": None,
                "reacquire_index": None,
                "reacquired_section_id": None,
                "outcome_seq": None,
                "outcome_index": None,
                "outcome_op": "",
            })
            waits_by_thread.setdefault(event.tid, []).append(wait_index)
            continue
        if event.op == "wait_mutex_release":
            wait = latest_wait(
                event.tid,
                lambda row: row["release_index"] is None,
            )
            if wait is None:
                unmatched_condition_events += 1
                continue
            wait["mutex"] = event.obj
            wait["release_seq"] = event.seq
            wait["release_index"] = index
            wait["released_section_id"] = close_section(
                event,
                index,
                {"mutex_write"},
            )
            continue
        if event.op == "wait_mutex_acquire":
            wait = latest_wait(
                event.tid,
                lambda row: (
                    row["release_index"] is not None
                    and row["reacquire_index"] is None
                    and row["mutex"] == event.obj
                ),
            )
            if wait is None:
                unmatched_condition_events += 1
                continue
            wait["reacquire_seq"] = event.seq
            wait["reacquire_index"] = index
            wait["reacquired_section_id"] = open_section(
                event,
                index,
                "mutex_write",
                origin="condition_reacquire",
            )
            continue
        if event.op in {"wake", "wait_timeout", "wait_fail"}:
            wait = latest_wait(
                event.tid,
                lambda row: (
                    row["condition"] == event.obj
                    and row["outcome_index"] is None
                ),
            )
            if wait is None:
                unmatched_condition_events += 1
                continue
            wait["outcome_seq"] = event.seq
            wait["outcome_index"] = index
            wait["outcome_op"] = event.op

    lines = [
        "; Lock, condition, and thread lifecycle state for schedule-SMT v7.",
        f"; Lifecycle order encoding: {order_encoding}.",
    ]
    exclusion_lines: list[str] = []
    for index, event in enumerate(lifecycle):
        lines.append(
            f"; sync event {index}: seq={event.seq} tid={event.tid} "
            f"op={event.op} object={event.obj}"
        )
        lines.append(f"(declare-fun sync_ord_{index} () Int)")
        if order_encoding == "permutation":
            lines.append(
                f"(assert (! (and (<= 0 sync_ord_{index}) "
                f"(< sync_ord_{index} {len(lifecycle)})) "
                f":named sync_bound_{index}))"
            )
    if order_encoding == "permutation" and len(lifecycle) > 1:
        variables = " ".join(
            f"sync_ord_{index}" for index in range(len(lifecycle))
        )
        lines.append(
            f"(assert (! (distinct {variables}) "
            ":named sync_permutation))"
        )

    lifecycle_by_thread: dict[int, list[int]] = {}
    for index, event in enumerate(lifecycle):
        lifecycle_by_thread.setdefault(event.tid, []).append(index)
    for indices in lifecycle_by_thread.values():
        for left, right in zip(indices, indices[1:]):
            name = f"sync_po_{left}_{right}"
            lines.append(
                f"(assert (! (< sync_ord_{left} sync_ord_{right}) "
                f":named {name}))"
            )
            record_edge(left, right, "program_order", name)

    modeled_positions = {
        event.seq: index for index, event in enumerate(modeled_events)
    }
    linked_controlled = [
        (modeled_positions[event.seq], index)
        for index, event in enumerate(lifecycle)
        if event.controlled and event.seq in modeled_positions
    ]
    controlled_anchors = [
        {
            "position_index": position_index,
            "lifecycle_index": lifecycle_index,
        }
        for position_index, lifecycle_index in linked_controlled
    ]
    anchor_stride = len(lifecycle) + 1
    semantic_pair_count = (
        len(linked_controlled) * (len(linked_controlled) - 1) // 2
    )
    link_count = 0
    if order_encoding == "partial":
        for position_index, lifecycle_index in linked_controlled:
            name = f"sync_anchor_{lifecycle_index}"
            lines.append(
                f"(assert (! (= sync_ord_{lifecycle_index} "
                f"(* {anchor_stride} pos_{position_index})) "
                f":named {name}))"
            )
            link_count += 1
    else:
        for left in range(len(linked_controlled)):
            for right in range(left + 1, len(linked_controlled)):
                left_pos, left_sync = linked_controlled[left]
                right_pos, right_sync = linked_controlled[right]
                lines.append(
                    f"(assert (! (or (and "
                    f"(< pos_{left_pos} pos_{right_pos}) "
                    f"(< sync_ord_{left_sync} sync_ord_{right_sync})) "
                    f"(and (< pos_{right_pos} pos_{left_pos}) "
                    f"(< sync_ord_{right_sync} sync_ord_{left_sync}))) "
                    f":named sync_link_{left_sync}_{right_sync}))"
                )
                link_count += 1

    complete_sections = [
        section for section in sections
        if section["release_index"] is not None
    ]
    exclusion_count = 0
    for left_index, left in enumerate(complete_sections):
        for right in complete_sections[left_index + 1:]:
            if (left["tid"] == right["tid"]
                    or left["object"] != right["object"]
                    or (left["mode"] == "rw_read"
                        and right["mode"] == "rw_read")):
                continue
            name = f"sync_exclusion_{left['id']}_{right['id']}"
            left_before = [
                int(left["release_index"]),
                int(right["acquire_index"]),
            ]
            right_before = [
                int(right["release_index"]),
                int(left["acquire_index"]),
            ]
            assertion_smt2 = (
                "(assert (! (or "
                f"(< sync_ord_{left['release_index']} "
                f"sync_ord_{right['acquire_index']}) "
                f"(< sync_ord_{right['release_index']} "
                f"sync_ord_{left['acquire_index']})) "
                f":named {name}))"
            )
            exclusion_lines.append(assertion_smt2)
            order_choices.append({
                "name": name,
                "kind": "critical_section_nonoverlap",
                "alternatives": [
                    {"edges": [left_before]},
                    {"edges": [right_before]},
                ],
            })
            exclusion_count += 1

    trylock_assumptions: list[str] = []
    trylock_rows: list[dict[str, Any]] = []
    for failure_index, failure in enumerate(failures):
        if failure["outcome_op"] != "trylock_fail":
            continue
        covering = []
        for section in complete_sections:
            if (section["tid"] == failure["tid"]
                    or section["object"] != failure["object"]
                    or (section["mode"] == "rw_read"
                        and failure["mode"] == "rw_read")):
                continue
            covering.append(
                f"(and (< sync_ord_{section['acquire_index']} "
                f"sync_ord_{failure['attempt_index']}) "
                f"(< sync_ord_{failure['attempt_index']} "
                f"sync_ord_{section['release_index']}))"
            )
        name = f"sync_trylock_busy_{failure_index}"
        lines.append(f"(declare-fun {name} () Bool)")
        lines.append(f"(assert (=> {name} {_smt_any(covering)}))")
        trylock_assumptions.append(name)
        trylock_rows.append({
            "tid": failure["tid"],
            "object": failure["object"],
            "attempt_seq": failure["attempt_seq"],
            "outcome_seq": failure["outcome_seq"],
            "attempt_var": f"sync_ord_{failure['attempt_index']}",
            "assumption": name,
            "covering_section_count": len(covering),
        })

    signal_events = [
        (index, event)
        for index, event in enumerate(lifecycle)
        if event.op in {"signal", "broadcast"}
    ]
    condition_assumptions: list[str] = []
    wake_rows: list[dict[str, Any]] = []
    for wait in condition_waits:
        complete = all(
            wait[field] is not None
            for field in ("release_index", "reacquire_index", "outcome_index")
        )
        if not complete or wait["outcome_op"] != "wake":
            continue
        candidates = [
            (index, event)
            for index, event in signal_events
            if event.obj == wait["condition"]
        ]
        assumption = f"cond_wake_signal_{wait['id']}"
        witness = f"cond_witness_{wait['id']}"
        lines.append(f"(declare-fun {assumption} () Bool)")
        lines.append(f"(declare-fun {witness} () Int)")
        alternatives = [
            (
                f"(and (= {witness} {candidate_index}) "
                f"(< sync_ord_{wait['release_index']} "
                f"sync_ord_{candidate_index}) "
                f"(< sync_ord_{candidate_index} "
                f"sync_ord_{wait['reacquire_index']}))"
            )
            for candidate_index, _ in candidates
        ]
        lines.append(
            f"(assert (=> {assumption} {_smt_any(alternatives)}))"
        )
        condition_assumptions.append(assumption)
        wake_rows.append({
            "wait_id": wait["id"],
            "assumption": assumption,
            "witness_var": witness,
            "candidates": [
                {
                    "value": candidate_index,
                    "seq": event.seq,
                    "op": event.op,
                }
                for candidate_index, event in candidates
            ],
        })

    wake_by_id = {
        row["wait_id"]: row for row in wake_rows
    }
    uniqueness_count = 0
    successful_waits = [
        wait for wait in condition_waits if wait["id"] in wake_by_id
    ]
    for left_offset, left in enumerate(successful_waits):
        left_row = wake_by_id[left["id"]]
        left_signals = {
            candidate["value"]
            for candidate in left_row["candidates"]
            if candidate["op"] == "signal"
        }
        for right in successful_waits[left_offset + 1:]:
            right_row = wake_by_id[right["id"]]
            shared_signals = left_signals.intersection(
                candidate["value"]
                for candidate in right_row["candidates"]
                if candidate["op"] == "signal"
            )
            for signal_index in sorted(shared_signals):
                lines.append(
                    f"(assert (! (=> (and {left_row['assumption']} "
                    f"{right_row['assumption']}) "
                    f"(not (and (= {left_row['witness_var']} "
                    f"{signal_index}) (= {right_row['witness_var']} "
                    f"{signal_index})))) "
                    f":named cond_signal_unique_{left['id']}_"
                    f"{right['id']}_{signal_index}))"
                )
                uniqueness_count += 1

    spawn_constraint_count = 0
    for thread in thread_lifecycles:
        identity_consistent = (
            thread["child_tid"] >= 0
            and (
                thread["start_tid"] is None
                or thread["start_tid"] == thread["child_tid"]
            )
            and (
                thread["exit_tid"] is None
                or thread["exit_tid"] == thread["child_tid"]
            )
        )
        thread["identity_consistent"] = identity_consistent
        if (thread["start_index"] is None
                or thread["create_outcome_op"] == "create_fail"
                or not identity_consistent):
            continue
        name = f"thread_spawn_{thread['id']}"
        lines.append(
            f"(assert (! (< sync_ord_{thread['create_index']} "
            f"sync_ord_{thread['start_index']}) "
            f":named {name}))"
        )
        record_edge(
            thread["create_index"],
            thread["start_index"],
            "thread_create_before_start",
            name,
        )
        spawn_constraint_count += 1

    def latest_target_thread(
        target_object: str,
        *,
        require_exit: bool = False,
    ) -> dict[str, Any] | None:
        for thread_index in reversed(
            threads_by_object.get(target_object, [])
        ):
            thread = thread_lifecycles[thread_index]
            if (
                thread.get("identity_consistent", False)
                and thread["create_outcome_op"] != "create_fail"
                and (
                    not require_exit
                    or thread["exit_index"] is not None
                )
            ):
                return thread
        return None

    join_completion_count = 0
    for join in joins:
        if not join["mapped"] or join["outcome_op"] != "join_success":
            continue
        target = latest_target_thread(
            join["target_object"], require_exit=True
        )
        if target is None:
            continue
        join["target_thread_id"] = target["id"]
        join["target_exit_index"] = target["exit_index"]
        name = f"thread_join_complete_{join['id']}"
        lines.append(
            f"(assert (! (< sync_ord_{target['exit_index']} "
            f"sync_ord_{join['outcome_index']}) "
            f":named {name}))"
        )
        record_edge(
            target["exit_index"],
            join["outcome_index"],
            "thread_exit_before_join_success",
            name,
        )
        join_completion_count += 1

    for detach in detaches:
        if not detach["mapped"]:
            continue
        target = latest_target_thread(detach["target_object"])
        if target is not None:
            detach["target_thread_id"] = target["id"]
            if detach["outcome_op"] == "detach_success":
                target["detach_id"] = detach["id"]

    for cancellation in cancellations:
        if not cancellation["mapped"]:
            continue
        target = latest_target_thread(cancellation["target_object"])
        if target is not None:
            cancellation["target_thread_id"] = target["id"]

    retirement_constraint_count = 0
    for retirement in retirements:
        target = latest_target_thread(
            retirement["object"], require_exit=True
        )
        if target is None:
            continue
        retirement["target_thread_id"] = target["id"]
        retirement["target_exit_index"] = target["exit_index"]
        target["retirement_id"] = retirement["id"]
        exit_name = f"thread_retire_exit_{retirement['id']}"
        lines.append(
            f"(assert (! (< sync_ord_{target['exit_index']} "
            f"sync_ord_{retirement['retire_index']}) "
            f":named {exit_name}))"
        )
        record_edge(
            target["exit_index"],
            retirement["retire_index"],
            "thread_exit_before_identity_retire",
            exit_name,
        )
        retirement_constraint_count += 1

        trigger_index = None
        trigger_kind = ""
        if retirement["cause"] == "join":
            for join in reversed(joins):
                if (
                    join["target_object"] == retirement["object"]
                    and join["outcome_op"] == "join_success"
                    and join["outcome_index"] is not None
                    and join["outcome_index"] < retirement["retire_index"]
                ):
                    trigger_index = join["outcome_index"]
                    trigger_kind = "join_success_before_identity_retire"
                    break
        elif retirement["cause"] == "detach":
            for detach in reversed(detaches):
                if (
                    detach["target_object"] == retirement["object"]
                    and detach["outcome_op"] == "detach_success"
                    and detach["outcome_index"] is not None
                    and detach["outcome_index"]
                    < retirement["retire_index"]
                ):
                    trigger_index = detach["outcome_index"]
                    trigger_kind = (
                        "detach_success_before_identity_retire"
                    )
                    break
            if (
                trigger_index is None
                and target["detached_at_create"]
                and target["create_outcome_index"] is not None
                and target["create_outcome_index"]
                < retirement["retire_index"]
            ):
                trigger_index = target["create_outcome_index"]
                trigger_kind = (
                    "detached_create_before_identity_retire"
                )
        if trigger_index is not None:
            retirement["trigger_index"] = trigger_index
            name = f"thread_retire_trigger_{retirement['id']}"
            lines.append(
                f"(assert (! (< sync_ord_{trigger_index} "
                f"sync_ord_{retirement['retire_index']}) "
                f":named {name}))"
            )
            record_edge(
                trigger_index,
                retirement["retire_index"],
                trigger_kind,
                name,
            )
            retirement_constraint_count += 1

    section_rows = []
    for section in sections:
        row = dict(section)
        row["attempt_var"] = f"sync_ord_{section['attempt_index']}"
        row["acquire_var"] = f"sync_ord_{section['acquire_index']}"
        row["release_var"] = (
            f"sync_ord_{section['release_index']}"
            if section["release_index"] is not None
            else ""
        )
        section_rows.append(row)
    condition_rows = []
    for wait in condition_waits:
        row = dict(wait)
        row["wait_var"] = f"sync_ord_{wait['wait_index']}"
        row["release_var"] = (
            f"sync_ord_{wait['release_index']}"
            if wait["release_index"] is not None
            else ""
        )
        row["reacquire_var"] = (
            f"sync_ord_{wait['reacquire_index']}"
            if wait["reacquire_index"] is not None
            else ""
        )
        row["outcome_var"] = (
            f"sync_ord_{wait['outcome_index']}"
            if wait["outcome_index"] is not None
            else ""
        )
        row["complete"] = all(
            wait[field] is not None
            for field in ("release_index", "reacquire_index", "outcome_index")
        )
        wake_row = wake_by_id.get(wait["id"])
        row["wake_assumption"] = (
            wake_row["assumption"] if wake_row else ""
        )
        row["wake_witness_var"] = (
            wake_row["witness_var"] if wake_row else ""
        )
        row["wake_candidates"] = (
            wake_row["candidates"] if wake_row else []
        )
        condition_rows.append(row)
    thread_rows = []
    for thread in thread_lifecycles:
        row = dict(thread)
        row["create_var"] = f"sync_ord_{thread['create_index']}"
        row["create_outcome_var"] = (
            f"sync_ord_{thread['create_outcome_index']}"
            if thread["create_outcome_index"] is not None
            else ""
        )
        row["start_var"] = (
            f"sync_ord_{thread['start_index']}"
            if thread["start_index"] is not None
            else ""
        )
        row["exit_var"] = (
            f"sync_ord_{thread['exit_index']}"
            if thread["exit_index"] is not None
            else ""
        )
        row["complete"] = (
            thread["create_outcome_op"] == "create_success"
            and thread["start_index"] is not None
            and thread["exit_index"] is not None
            and thread["identity_consistent"]
        )
        row["detached"] = bool(
            thread["detached_at_create"]
            or thread.get("detach_id") is not None
        )
        row["retire_var"] = (
            f"sync_ord_{retirements[thread['retirement_id']]['retire_index']}"
            if thread.get("retirement_id") is not None
            else ""
        )
        thread_rows.append(row)
    join_rows = []
    for join in joins:
        row = dict(join)
        row["join_var"] = f"sync_ord_{join['join_index']}"
        row["outcome_var"] = (
            f"sync_ord_{join['outcome_index']}"
            if join["outcome_index"] is not None
            else ""
        )
        row["target_exit_var"] = (
            f"sync_ord_{join['target_exit_index']}"
            if join["target_exit_index"] is not None
            else ""
        )
        row["complete"] = (
            join["outcome_op"] == "join_success"
            and join["target_exit_index"] is not None
        )
        join_rows.append(row)
    detach_rows = []
    for detach in detaches:
        row = dict(detach)
        row["detach_var"] = f"sync_ord_{detach['detach_index']}"
        row["outcome_var"] = (
            f"sync_ord_{detach['outcome_index']}"
            if detach["outcome_index"] is not None
            else ""
        )
        row["complete"] = detach["outcome_index"] is not None
        detach_rows.append(row)
    cancellation_rows = []
    for cancellation in cancellations:
        row = dict(cancellation)
        row["cancel_var"] = (
            f"sync_ord_{cancellation['cancel_index']}"
        )
        row["outcome_var"] = (
            f"sync_ord_{cancellation['outcome_index']}"
            if cancellation["outcome_index"] is not None
            else ""
        )
        row["complete"] = cancellation["outcome_index"] is not None
        cancellation_rows.append(row)
    retirement_rows = []
    for retirement in retirements:
        row = dict(retirement)
        row["retire_var"] = (
            f"sync_ord_{retirement['retire_index']}"
        )
        row["target_exit_var"] = (
            f"sync_ord_{retirement['target_exit_index']}"
            if retirement["target_exit_index"] is not None
            else ""
        )
        row["trigger_var"] = (
            f"sync_ord_{retirement['trigger_index']}"
            if retirement["trigger_index"] is not None
            else ""
        )
        row["complete"] = (
            retirement["target_exit_index"] is not None
            and retirement["trigger_index"] is not None
        )
        retirement_rows.append(row)
    metadata.update({
        "complete_section_count": len(complete_sections),
        "open_section_count": len(sections) - len(complete_sections),
        "unmatched_outcome_count": unmatched_outcomes,
        "unmatched_unlock_count": unmatched_unlocks,
        "pending_attempt_count": sum(
            len(attempts) for attempts in pending.values()
        ),
        "exclusion_constraint_count": exclusion_count,
        "lazy_refinement_count": len(exclusion_lines),
        "lazy_refinements": [
            {
                "name": choice["name"],
                "kind": choice["kind"],
                "choice_index": index,
            }
            for index, choice in enumerate(order_choices)
        ],
        "order_link_constraint_count": link_count,
        "order_link_semantic_pair_count": semantic_pair_count,
        "controlled_anchor_count": len(controlled_anchors),
        "anchor_stride": anchor_stride,
        "trylock_failure_count": sum(
            1 for failure in failures
            if failure["outcome_op"] == "trylock_fail"
        ),
        "trylock_assumptions": trylock_assumptions,
        "trylock_failures": trylock_rows,
        "condition_wait_count": len(condition_waits),
        "complete_condition_wait_count": sum(
            1 for wait in condition_rows if wait["complete"]
        ),
        "successful_condition_wait_count": sum(
            1 for wait in condition_rows if wait["outcome_op"] == "wake"
        ),
        "timed_out_condition_wait_count": sum(
            1
            for wait in condition_rows
            if wait["outcome_op"] == "wait_timeout"
        ),
        "condition_wake_assumptions": condition_assumptions,
        "condition_waits": condition_rows,
        "wake_witness_uniqueness_count": uniqueness_count,
        "unmatched_condition_event_count": unmatched_condition_events,
        "thread_lifecycle_count": len(thread_rows),
        "complete_thread_lifecycle_count": sum(
            1 for thread in thread_rows if thread["complete"]
        ),
        "successful_create_count": sum(
            1
            for thread in thread_rows
            if thread["create_outcome_op"] == "create_success"
        ),
        "failed_create_count": sum(
            1
            for thread in thread_rows
            if thread["create_outcome_op"] == "create_fail"
        ),
        "thread_spawn_constraint_count": spawn_constraint_count,
        "thread_lifecycles": thread_rows,
        "join_count": len(join_rows),
        "successful_join_count": sum(
            1
            for join in join_rows
            if join["outcome_op"] == "join_success"
        ),
        "failed_join_count": sum(
            1
            for join in join_rows
            if join["outcome_op"] == "join_fail"
        ),
        "cancelled_join_count": sum(
            1
            for join in join_rows
            if join["outcome_op"] == "join_cancelled"
        ),
        "join_completion_constraint_count": join_completion_count,
        "joins": join_rows,
        "detach_count": len(detach_rows),
        "successful_detach_count": sum(
            1
            for detach in detach_rows
            if detach["outcome_op"] == "detach_success"
        ),
        "failed_detach_count": sum(
            1
            for detach in detach_rows
            if detach["outcome_op"] == "detach_fail"
        ),
        "detaches": detach_rows,
        "cancel_count": len(cancellation_rows),
        "successful_cancel_count": sum(
            1
            for cancellation in cancellation_rows
            if cancellation["outcome_op"] == "cancel_success"
        ),
        "failed_cancel_count": sum(
            1
            for cancellation in cancellation_rows
            if cancellation["outcome_op"] == "cancel_fail"
        ),
        "cancellations": cancellation_rows,
        "thread_retire_count": len(retirement_rows),
        "thread_retirement_constraint_count": (
            retirement_constraint_count
        ),
        "thread_retirements": retirement_rows,
        "unmatched_thread_event_count": unmatched_thread_events,
        "order_bound_constraint_count": (
            len(lifecycle) if order_encoding == "permutation" else 0
        ),
        "order_distinct_constraint_count": (
            1
            if order_encoding == "permutation" and len(lifecycle) > 1
            else 0
        ),
        "sections": section_rows,
        "order_ir": {
            "schema": SCHEDULE_ORDER_IR_SCHEMA,
            "event_count": len(lifecycle),
            "events": lifecycle_event_rows,
            "fixed_edges": fixed_order_edges,
            "choice_constraints": order_choices,
            "controlled_anchors": controlled_anchors,
            "link_encoding": (
                "scaled-anchor" if order_encoding == "partial"
                else "pairwise"
            ),
            "anchor_stride": anchor_stride,
        },
    })
    relaxed_smt2 = "\n".join(lines) + "\n"
    exact_smt2 = "\n".join(lines + exclusion_lines) + "\n"
    return exact_smt2, relaxed_smt2, metadata


def _normalize_memory_model(value: Any) -> str:
    model = str(value or "SC").strip().upper().replace("-", "_")
    aliases = {
        "SEQUENTIAL_CONSISTENCY": "SC",
        "RELEASE_ACQUIRE": "RA",
        "C11_RA": "RA",
        "X86_TSO": "TSO",
    }
    model = aliases.get(model, model)
    if model not in MEMORY_MODELS:
        raise ValueError(
            f"memory model must be one of {sorted(MEMORY_MODELS)}"
        )
    return model


def _program_order_pairs(
    events: list[ScheduleEvent],
    memory_model: str,
) -> set[tuple[int, int]]:
    model = _normalize_memory_model(memory_model)
    by_thread: dict[int, list[int]] = {}
    for index, event in enumerate(events):
        by_thread.setdefault(event.tid, []).append(index)
    pairs: set[tuple[int, int]] = set()
    for indices in by_thread.values():
        for left, right in zip(indices, indices[1:]):
            if (
                model == "TSO"
                and events[left].write
                and events[right].op == "read"
            ):
                continue
            pairs.add((left, right))
    return pairs


def _event_memory_order(event: ScheduleEvent) -> str:
    for tag in event.tags:
        if tag.startswith("mo="):
            value = tag.split("=", 1)[1]
            if value in {
                "relaxed", "consume", "acquire", "release",
                "acq_rel", "seq_cst",
            }:
                return value
    return "relaxed"


def _event_is_atomic(event: ScheduleEvent) -> bool:
    return (
        event.op in FENCE_OPS
        or _tag_value(event.tags, "atomic") == "1"
        or any(str(tag).startswith("mo=") for tag in event.tags)
    )


def _memory_events_with_indices(
    events: list[ScheduleEvent],
    max_memory_events: int,
) -> tuple[list[tuple[int, ScheduleEvent]], int]:
    all_memory = [
        (index, event)
        for index, event in enumerate(events)
        if event.memory or event.op in FENCE_OPS
    ]
    cap = max(0, int(max_memory_events))
    return all_memory[:cap], len(all_memory)


def _sc_or_tso_read_from(
    memory_events: list[tuple[int, ScheduleEvent]],
    *,
    model: str,
) -> tuple[list[str], list[dict[str, Any]]]:
    lines: list[str] = []
    rows: list[dict[str, Any]] = []
    for read_index, read in memory_events:
        if read.op not in {"read", "rmw"}:
            continue
        writes = [
            (index, event)
            for index, event in memory_events
            if (
                index != read_index
                and event.write
                and _memory_access_contains(event, read)
            )
        ]
        variable = f"rf_{read_index}"
        sources = [-1] + [index for index, _ in writes]
        lines.append(f"(declare-fun {variable} () Int)")
        lines.append(
            f"(assert (! {_smt_any(f'(= {variable} {_smt_integer(source)})' for source in sources)} "
            f":named rf_domain_{read_index}))"
        )

        global_conditions: dict[int, str] = {}
        no_prior = [
            f"(< pos_{read_index} pos_{write_index})"
            for write_index, _ in writes
        ]
        global_conditions[-1] = _smt_all(no_prior)
        for write_index, _ in writes:
            terms = [f"(< pos_{write_index} pos_{read_index})"]
            for other_index, _ in writes:
                if other_index == write_index:
                    continue
                terms.append(
                    f"(or (< pos_{other_index} pos_{write_index}) "
                    f"(< pos_{read_index} pos_{other_index}))"
                )
            global_conditions[write_index] = _smt_all(terms)

        latest_local: tuple[int, ScheduleEvent] | None = None
        if model == "TSO":
            local_prior = [
                (index, event)
                for index, event in writes
                if event.tid == read.tid and index < read_index
            ]
            if local_prior:
                latest_local = local_prior[-1]
        for source in sources:
            condition = global_conditions[source]
            if latest_local is not None:
                local_index = latest_local[0]
                buffered = f"(< pos_{read_index} pos_{local_index})"
                if source == local_index:
                    condition = f"(or {buffered} {condition})"
                else:
                    condition = f"(and (not {buffered}) {condition})"
            lines.append(
                f"(assert (! (=> (= {variable} {_smt_integer(source)}) "
                f"{condition}) "
                f":named {model.lower()}_rf_{read_index}_"
                f"{str(source).replace('-', 'i')}))"
            )
        rows.append({
            "read_index": read_index,
            "read_seq": read.seq,
            "object": read.obj,
            "variable": variable,
            "sources": sources,
            "rmw": read.op == "rmw",
        })
    return lines, rows


def _ra_memory_context(
    events: list[ScheduleEvent],
    memory_events: list[tuple[int, ScheduleEvent]],
) -> tuple[list[str], list[dict[str, Any]], dict[str, Any]]:
    lines = [
        "; bounded C11/C++ atomic axiomatic consistency",
    ]
    rows: list[dict[str, Any]] = []
    local_of_global = {
        global_index: local_index
        for local_index, (global_index, _) in enumerate(memory_events)
    }
    count = len(memory_events)

    memory_only = [
        (index, event)
        for index, event in memory_events if event.memory
    ]
    parent = {index: index for index, _ in memory_only}

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        low, high = sorted((left_root, right_root))
        parent[high] = low

    for position, (left_index, left) in enumerate(memory_only):
        for right_index, right in memory_only[position + 1:]:
            if _memory_events_overlap(left, right):
                union(left_index, right_index)
    location_of = {
        index: find(index) for index, _ in memory_only
    }
    writes_by_location: dict[
        int, list[tuple[int, ScheduleEvent]]
    ] = {}
    reads_by_location: dict[
        int, list[tuple[int, ScheduleEvent]]
    ] = {}
    reads: list[tuple[int, ScheduleEvent]] = []
    for global_index, event in memory_only:
        location = location_of[global_index]
        if event.write:
            writes_by_location.setdefault(location, []).append(
                (global_index, event)
            )
        if event.op in {"read", "rmw"}:
            reads.append((global_index, event))
            reads_by_location.setdefault(location, []).append(
                (global_index, event)
            )

    for writes in writes_by_location.values():
        for global_index, _ in writes:
            lines.append(f"(declare-fun mo_{global_index} () Int)")
            lines.append(
                f"(assert (and (<= 0 mo_{global_index}) "
                f"(< mo_{global_index} {len(writes)})))"
            )
        if len(writes) > 1:
            lines.append(
                "(assert (distinct "
                + " ".join(f"mo_{index}" for index, _ in writes)
                + "))"
            )

    for local_index in range(count):
        lines.append(f"(declare-fun causal_{local_index} () Int)")
        for other_index in range(count):
            lines.append(
                f"(declare-fun hb_{local_index}_{other_index} () Bool)"
            )
        lines.append(f"(assert (not hb_{local_index}_{local_index}))")
    for local_index in range(count):
        for other_index in range(count):
            if local_index == other_index:
                continue
            lines.append(
                f"(assert (=> hb_{local_index}_{other_index} "
                f"(< causal_{local_index} causal_{other_index})))"
            )

    by_thread: dict[int, list[int]] = {}
    for local_index, (_, event) in enumerate(memory_events):
        by_thread.setdefault(event.tid, []).append(local_index)
    po_edge_count = 0
    for indices in by_thread.values():
        for left_pos, left in enumerate(indices):
            for right in indices[left_pos + 1:]:
                po_edge_count += 1
                lines.append(f"(assert hb_{left}_{right})")

    for left in range(count):
        for middle in range(count):
            if left == middle:
                continue
            for right in range(count):
                if right == left or right == middle:
                    continue
                lines.append(
                    f"(assert (=> (and hb_{left}_{middle} "
                    f"hb_{middle}_{right}) hb_{left}_{right}))"
                )

    for read_index, read in reads:
        location = location_of[read_index]
        writes = [
            (index, write)
            for index, write in writes_by_location.get(location, ())
            if (
                index != read_index
                and _memory_access_contains(write, read)
            )
        ]
        read_local = local_of_global[read_index]
        variable = f"rf_{read_index}"
        sources = [-1] + [index for index, _ in writes]
        lines.append(f"(declare-fun {variable} () Int)")
        lines.append(
            f"(assert {_smt_any(f'(= {variable} {_smt_integer(source)})' for source in sources)})"
        )
        for write_index, _ in writes:
            write_local = local_of_global[write_index]
            lines.append(
                f"(assert (=> (= {variable} {write_index}) "
                f"(< causal_{write_local} causal_{read_local})))"
            )
        if read.op == "rmw":
            lines.append(
                f"(assert (=> (= {variable} (- 1)) "
                f"(= mo_{read_index} 0)))"
            )
            for write_index, _ in writes:
                lines.append(
                    f"(assert (=> (= {variable} {write_index}) "
                    f"(= mo_{read_index} (+ mo_{write_index} 1))))"
                )
        rows.append({
            "read_index": read_index,
            "read_seq": read.seq,
            "object": read.obj,
            "variable": variable,
            "sources": sources,
            "rmw": read.op == "rmw",
        })

    release_orders = {"release", "acq_rel", "seq_cst"}
    acquire_orders = {"acquire", "acq_rel", "seq_cst"}
    release_sequence_count = 0
    release_heads_by_location: dict[
        int, list[tuple[int, ScheduleEvent]]
    ] = {}
    for location, writes in writes_by_location.items():
        heads = [
            (index, event) for index, event in writes
            if _event_memory_order(event) in release_orders
        ]
        release_heads_by_location[location] = heads
        for head_index, head in heads:
            for source_index, source in writes:
                variable = f"rs_{head_index}_{source_index}"
                lines.append(f"(declare-fun {variable} () Bool)")
                if source_index == head_index:
                    lines.append(f"(assert {variable})")
                    continue
                terms: list[str] = []
                for predecessor_index, _ in writes:
                    if predecessor_index == source_index:
                        continue
                    continuation = "false"
                    if source.op == "rmw":
                        continuation = (
                            f"(= rf_{source_index} {predecessor_index})"
                        )
                    elif source.tid == head.tid:
                        continuation = "true"
                    if continuation == "false":
                        continue
                    terms.append(
                        f"(and rs_{head_index}_{predecessor_index} "
                        f"(= mo_{source_index} "
                        f"(+ mo_{predecessor_index} 1)) {continuation})"
                    )
                lines.append(
                    f"(assert (= {variable} {_smt_any(terms)}))"
                )
                release_sequence_count += 1

    sw_count = 0
    release_fences = [
        (index, event) for index, event in memory_events
        if (
            event.op == "fence"
            and _event_memory_order(event) in release_orders
        )
    ]
    acquire_fences = [
        (index, event) for index, event in memory_events
        if (
            event.op == "fence"
            and _event_memory_order(event) in acquire_orders
        )
    ]
    for read_index, read in reads:
        location = location_of[read_index]
        read_local = local_of_global[read_index]
        writes = [
            (index, write)
            for index, write in writes_by_location.get(location, ())
            if index != read_index and _memory_access_contains(write, read)
        ]
        heads = release_heads_by_location.get(location, ())
        for write_index, write in writes:
            for head_index, _ in heads:
                release_sequence = f"rs_{head_index}_{write_index}"
                if _event_memory_order(read) in acquire_orders:
                    sw_count += 1
                    lines.append(
                        f"(assert (=> (and (= rf_{read_index} {write_index}) "
                        f"{release_sequence}) "
                        f"hb_{local_of_global[head_index]}_{read_local}))"
                    )
                for fence_index, fence in acquire_fences:
                    if fence.tid == read.tid and fence_index > read_index:
                        sw_count += 1
                        lines.append(
                            f"(assert (=> (and (= rf_{read_index} "
                            f"{write_index}) {release_sequence}) "
                            f"hb_{local_of_global[head_index]}_"
                            f"{local_of_global[fence_index]}))"
                        )
            for fence_index, fence in release_fences:
                if fence.tid != write.tid or fence_index >= write_index:
                    continue
                if _event_memory_order(read) in acquire_orders:
                    sw_count += 1
                    lines.append(
                        f"(assert (=> (= rf_{read_index} {write_index}) "
                        f"hb_{local_of_global[fence_index]}_{read_local}))"
                    )
                for acquire_index, acquire_fence in acquire_fences:
                    if (
                        acquire_fence.tid == read.tid
                        and acquire_index > read_index
                    ):
                        sw_count += 1
                        lines.append(
                            f"(assert (=> (= rf_{read_index} {write_index}) "
                            f"hb_{local_of_global[fence_index]}_"
                            f"{local_of_global[acquire_index]}))"
                        )

    coherence_count = 0
    for writes in writes_by_location.values():
        for left_index, _ in writes:
            left_local = local_of_global[left_index]
            for right_index, _ in writes:
                if left_index == right_index:
                    continue
                right_local = local_of_global[right_index]
                coherence_count += 1
                lines.append(
                    f"(assert (=> hb_{left_local}_{right_local} "
                    f"(< mo_{left_index} mo_{right_index})))"
                )

    for location, object_reads in reads_by_location.items():
        writes = writes_by_location.get(location, [])
        for read_index, read in object_reads:
            read_local = local_of_global[read_index]
            sources = [-1] + [
                index for index, write in writes
                if (
                    index != read_index
                    and _memory_access_contains(write, read)
                )
            ]
            for write_index, _ in writes:
                if write_index == read_index:
                    continue
                write_local = local_of_global[write_index]
                for source_index in sources:
                    if source_index == -1:
                        wr_order = "false"
                    elif source_index == write_index:
                        wr_order = "true"
                    else:
                        wr_order = (
                            f"(< mo_{write_index} mo_{source_index})"
                        )
                    lines.append(
                        f"(assert (=> (and hb_{write_local}_{read_local} "
                        f"(= rf_{read_index} "
                        f"{_smt_integer(source_index)})) {wr_order}))"
                    )
                    if source_index == -1:
                        rw_order = "true"
                    elif source_index == write_index:
                        rw_order = "false"
                    else:
                        rw_order = (
                            f"(< mo_{source_index} mo_{write_index})"
                        )
                    lines.append(
                        f"(assert (=> (and hb_{read_local}_{write_local} "
                        f"(= rf_{read_index} "
                        f"{_smt_integer(source_index)})) {rw_order}))"
                    )
                    coherence_count += 2

    seq_cst = [
        (index, event) for index, event in memory_events
        if _event_memory_order(event) == "seq_cst"
    ]
    for rank, (global_index, _) in enumerate(seq_cst):
        lines.append(f"(declare-fun sc_{global_index} () Int)")
        lines.append(
            f"(assert (and (<= 0 sc_{global_index}) "
            f"(< sc_{global_index} {len(seq_cst)})))"
        )
    if len(seq_cst) > 1:
        lines.append(
            "(assert (distinct "
            + " ".join(f"sc_{index}" for index, _ in seq_cst)
            + "))"
        )
    sc_constraint_count = 0
    for left_index, _ in seq_cst:
        for right_index, _ in seq_cst:
            if left_index == right_index:
                continue
            sc_constraint_count += 1
            lines.append(
                f"(assert (=> hb_{local_of_global[left_index]}_"
                f"{local_of_global[right_index]} "
                f"(< sc_{left_index} sc_{right_index})))"
            )
    for writes in writes_by_location.values():
        sc_writes = [
            (index, event) for index, event in writes
            if _event_memory_order(event) == "seq_cst"
        ]
        for left_index, _ in sc_writes:
            for right_index, _ in sc_writes:
                if left_index == right_index:
                    continue
                sc_constraint_count += 1
                lines.append(
                    f"(assert (=> (< mo_{left_index} mo_{right_index}) "
                    f"(< sc_{left_index} sc_{right_index})))"
                )
    for read_index, read in reads:
        if _event_memory_order(read) != "seq_cst":
            continue
        location = location_of[read_index]
        sc_writes = [
            (index, write)
            for index, write in writes_by_location.get(location, ())
            if (
                index != read_index
                and _event_memory_order(write) == "seq_cst"
                and _memory_access_contains(write, read)
            )
        ]
        for write_index, _ in sc_writes:
            sc_constraint_count += 1
            lines.append(
                f"(assert (=> (= rf_{read_index} (- 1)) "
                f"(< sc_{read_index} sc_{write_index})))"
            )
            lines.append(
                f"(assert (=> (= rf_{read_index} {write_index}) "
                f"(< sc_{write_index} sc_{read_index})))"
            )
            sc_constraint_count += 1
            for other_index, _ in sc_writes:
                if other_index == write_index:
                    continue
                sc_constraint_count += 1
                lines.append(
                    f"(assert (=> (= rf_{read_index} {write_index}) "
                    f"(or (< mo_{other_index} mo_{write_index}) "
                    f"(< sc_{read_index} sc_{other_index}))))"
                )

    event_by_seq = {event.seq: event for event in events}
    data_races: list[dict[str, Any]] = []
    atomic_classification_available = any(
        _tag_value(event.tags, "atomic") in {"0", "1"}
        for event in events if event.memory
    )
    if atomic_classification_available:
        for conflict in classify_schedule_conflicts(events):
            if conflict.kind != "memory":
                continue
            left = event_by_seq.get(conflict.left_seq)
            right = event_by_seq.get(conflict.right_seq)
            if left is None or right is None:
                continue
            if _event_is_atomic(left) and _event_is_atomic(right):
                continue
            data_races.append(conflict.to_mapping())
            lines.append("(assert false)")

    mixed_size_overlaps = 0
    for position, (_, left) in enumerate(memory_only):
        left_interval = _memory_interval(left)
        for _, right in memory_only[position + 1:]:
            right_interval = _memory_interval(right)
            if (
                left_interval is not None
                and right_interval is not None
                and _memory_events_overlap(left, right)
                and (
                    left_interval[1] - left_interval[0]
                    != right_interval[1] - right_interval[0]
                )
            ):
                mixed_size_overlaps += 1

    return lines, rows, {
        "hb_variable_count": count * count,
        "po_hb_edge_count": po_edge_count,
        "synchronizes_with_choice_count": sw_count,
        "coherence_constraint_count": coherence_count,
        "release_sequence_constraint_count": release_sequence_count,
        "seq_cst_event_count": len(seq_cst),
        "seq_cst_constraint_count": sc_constraint_count,
        "non_atomic_race_candidate_count": len(data_races),
        "non_atomic_race_candidates": data_races,
        "atomic_classification_available": (
            atomic_classification_available
        ),
        "mixed_size_overlap_count": mixed_size_overlaps,
        "atomic_rmw_count": sum(
            1 for _, event in reads if event.op == "rmw"
        ),
        "fence_count": len(release_fences) + sum(
            1 for _, event in acquire_fences
            if _event_memory_order(event) == "acquire"
        ),
    }


def _memory_model_context(
    events: list[ScheduleEvent],
    *,
    memory_model: str,
    max_memory_events: int,
) -> tuple[str, dict[str, Any]]:
    model = _normalize_memory_model(memory_model)
    memory_events, total_memory = _memory_events_with_indices(
        events, max_memory_events
    )
    if model in {"SC", "TSO"}:
        lines, reads = _sc_or_tso_read_from(
            memory_events,
            model=model,
        )
        extra: dict[str, Any] = {}
        semantics = (
            "last-global-write-before-read"
            if model == "SC"
            else "fifo-store-buffer-forwarding-or-last-global-write"
        )
    else:
        lines, reads, extra = _ra_memory_context(events, memory_events)
        semantics = (
            "bounded-c11-rf-mo-hb-release-sequence-fence-sc-rmw"
        )
    smt2 = "\n".join([
        f"; memory model {model}: {semantics}",
        *lines,
    ]) + "\n"
    metadata = {
        "model": model,
        "semantics": semantics,
        "memory_event_count": total_memory,
        "modeled_memory_event_count": len(memory_events),
        "truncated": total_memory > len(memory_events),
        "read_count": len(reads),
        "choice_count": sum(len(row["sources"]) for row in reads),
        "reads": reads,
        "smt2_sha256": hashlib.sha256(
            smt2.encode("utf-8")
        ).hexdigest(),
        **extra,
    }
    return smt2, metadata


def _schedule_smt_base(
    events: list[ScheduleEvent],
    observed_points: dict[int, SchedulePoint],
    sync_state_smt2: str = "",
    *,
    memory_model: str = "SC",
    memory_model_smt2: str = "",
) -> tuple[str, int, tuple[str, ...]]:
    """Encode declarations and schedule-invariant partial-order constraints."""
    lines = [
        f"; {SCHEDULE_SMT_SCHEMA}",
        f"; Shared {_normalize_memory_model(memory_model)} context. "
        "General runtime enabledness is not encoded.",
        "(set-logic QF_LIA)",
        "(set-option :produce-models true)",
    ]
    for index, event in enumerate(events):
        tags = " ".join(event.tags)
        suffix = f" {tags}" if tags else ""
        lines.append(
            f"; event {index}: seq={event.seq} tid={event.tid} "
            f"op={event.op} object={event.obj}{suffix}"
        )
        lines.append(f"(declare-fun pos_{index} () Int)")
    for index in range(len(events)):
        lines.append(
            f"(assert (! (and (<= 0 pos_{index}) "
            f"(< pos_{index} {len(events)})) :named bound_{index}))"
        )
    if len(events) > 1:
        positions = " ".join(f"pos_{index}" for index in range(len(events)))
        lines.append(f"(assert (! (distinct {positions}) :named permutation))")

    program_order = _program_order_pairs(events, memory_model)
    for left, right in sorted(program_order):
        lines.append(
            f"(assert (! (< pos_{left} pos_{right}) "
            f":named po_{left}_{right}))"
        )

    # Observed HB is schedule-dependent, so expose it as optional assumptions
    # rather than baking it into alternative-schedule feasibility.
    hb_edges: list[tuple[int, int]] = []
    for left in range(len(events)):
        for right in range(left + 1, len(events)):
            left_point = observed_points.get(events[left].seq)
            right_point = observed_points.get(events[right].seq)
            if (left_point is None or right_point is None
                    or not happens_before(left_point, right_point)
                    or (left, right) in program_order):
                continue
            hb_edges.append((left, right))
            lines.append(f"(declare-fun observed_hb_{left}_{right} () Bool)")
            lines.append(
                f"(assert (=> observed_hb_{left}_{right} "
                f"(< pos_{left} pos_{right})))"
            )
    if sync_state_smt2:
        lines.append(sync_state_smt2.rstrip())
    if memory_model_smt2:
        lines.append(memory_model_smt2.rstrip())
    return (
        "\n".join(lines) + "\n",
        len(program_order),
        tuple(f"observed_hb_{left}_{right}" for left, right in hb_edges),
    )


def _schedule_smt_query_delta(
    events: list[ScheduleEvent],
    prefix: tuple[int, ...],
    conflict: ScheduleConflict,
    query_index: int,
) -> str:
    """Encode prefix and conflict-reversal assertions for one candidate."""
    event_positions = {event.seq: index for index, event in enumerate(events)}
    lines = [
        f"; query {query_index}: prefix={_prefix_key(prefix)}",
    ]
    by_thread: dict[int, list[int]] = {}
    for index, event in enumerate(events):
        by_thread.setdefault(event.tid, []).append(index)
    occurrences: dict[int, int] = {}
    for slot, tid in enumerate(prefix):
        occurrence = occurrences.get(tid, 0)
        thread_events = by_thread.get(tid, [])
        term = (
            f"(= pos_{thread_events[occurrence]} {slot})"
            if occurrence < len(thread_events)
            else "false"
        )
        occurrences[tid] = occurrence + 1
        lines.append(
            f"(assert (! {term} "
            f":named q{query_index}_replay_slot_{slot}))"
        )

    left = event_positions[conflict.left_seq]
    right = event_positions[conflict.right_seq]
    lines.append(
        f"(assert (! (< pos_{right} pos_{left}) "
        f":named q{query_index}_conflict_reversal))"
    )
    return "\n".join(lines) + "\n"


def materialize_schedule_smt_query(
    artifact: dict[str, Any],
    query_index: int,
) -> str:
    """Materialize one standalone SMT-LIB2 query from a shared artifact."""
    if artifact.get("schema") not in SCHEDULE_SMT_SCHEMAS:
        raise ValueError("unsupported schedule-SMT artifact schema")
    try:
        query = artifact["queries"][int(query_index)]
        base = artifact["base_smt2"]
        delta = query["delta_smt2"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ValueError("invalid schedule-SMT query index or artifact") from exc
    if not isinstance(base, str) or not isinstance(delta, str):
        raise ValueError("schedule-SMT base/delta must be strings")
    return base + delta + "(check-sat)\n"


class _SystemZ3Solver:
    """Small ctypes binding for incremental QF_LIA model extraction."""

    _Z3_L_FALSE = -1
    _Z3_L_UNDEF = 0
    _Z3_L_TRUE = 1

    def __init__(self, smt2: str) -> None:
        library_path = ctypes.util.find_library("z3")
        if not library_path:
            raise RuntimeError("system libz3 is unavailable")
        self._z3 = ctypes.CDLL(library_path)
        self._context: int | None = None
        self._solver: int | None = None
        self._retained_asts: list[int] = []
        self._constants: dict[tuple[str, bool], int] = {}
        self._bitvector_constants: dict[tuple[str, int], int] = {}
        self._error_codes: list[int] = []
        self._bind_api()
        self._error_handler = self._ERROR_HANDLER(self._record_error)

        config = self._z3.Z3_mk_config()
        if not config:
            raise RuntimeError("Z3_mk_config failed")
        try:
            self._context = self._z3.Z3_mk_context_rc(config)
        finally:
            self._z3.Z3_del_config(config)
        if not self._context:
            raise RuntimeError("Z3_mk_context_rc failed")
        self._z3.Z3_set_error_handler(
            self._context, self._error_handler
        )
        self._solver = self._z3.Z3_mk_solver(self._context)
        if not self._solver:
            self.close()
            raise RuntimeError("Z3_mk_solver failed")
        self._z3.Z3_solver_inc_ref(self._context, self._solver)
        try:
            self._assert_smt2(smt2)
        except BaseException:
            self.close()
            raise

    def _bind_api(self) -> None:
        z3 = self._z3
        void = ctypes.c_void_p
        void_array = ctypes.POINTER(void)
        self._ERROR_HANDLER = ctypes.CFUNCTYPE(
            None, void, ctypes.c_int
        )
        z3.Z3_mk_config.restype = void
        z3.Z3_del_config.argtypes = [void]
        z3.Z3_mk_context_rc.argtypes = [void]
        z3.Z3_mk_context_rc.restype = void
        z3.Z3_del_context.argtypes = [void]
        z3.Z3_set_error_handler.argtypes = [
            void, self._ERROR_HANDLER,
        ]
        z3.Z3_get_error_msg.argtypes = [void, ctypes.c_int]
        z3.Z3_get_error_msg.restype = ctypes.c_char_p
        z3.Z3_parse_smtlib2_string.argtypes = [
            void,
            ctypes.c_char_p,
            ctypes.c_uint,
            void_array,
            void_array,
            ctypes.c_uint,
            void_array,
            void_array,
        ]
        z3.Z3_parse_smtlib2_string.restype = void
        z3.Z3_ast_vector_inc_ref.argtypes = [void, void]
        z3.Z3_ast_vector_dec_ref.argtypes = [void, void]
        z3.Z3_ast_vector_size.argtypes = [void, void]
        z3.Z3_ast_vector_size.restype = ctypes.c_uint
        z3.Z3_ast_vector_get.argtypes = [void, void, ctypes.c_uint]
        z3.Z3_ast_vector_get.restype = void
        z3.Z3_mk_solver.argtypes = [void]
        z3.Z3_mk_solver.restype = void
        z3.Z3_solver_inc_ref.argtypes = [void, void]
        z3.Z3_solver_dec_ref.argtypes = [void, void]
        z3.Z3_solver_assert.argtypes = [void, void, void]
        z3.Z3_solver_check_assumptions.argtypes = [
            void, void, ctypes.c_uint, void_array,
        ]
        z3.Z3_solver_check_assumptions.restype = ctypes.c_int
        z3.Z3_solver_get_reason_unknown.argtypes = [void, void]
        z3.Z3_solver_get_reason_unknown.restype = ctypes.c_char_p
        z3.Z3_solver_get_model.argtypes = [void, void]
        z3.Z3_solver_get_model.restype = void
        z3.Z3_model_inc_ref.argtypes = [void, void]
        z3.Z3_model_dec_ref.argtypes = [void, void]
        z3.Z3_mk_string_symbol.argtypes = [void, ctypes.c_char_p]
        z3.Z3_mk_string_symbol.restype = void
        z3.Z3_mk_bool_sort.argtypes = [void]
        z3.Z3_mk_bool_sort.restype = void
        z3.Z3_mk_int_sort.argtypes = [void]
        z3.Z3_mk_int_sort.restype = void
        z3.Z3_mk_bv_sort.argtypes = [void, ctypes.c_uint]
        z3.Z3_mk_bv_sort.restype = void
        z3.Z3_mk_const.argtypes = [void, void, void]
        z3.Z3_mk_const.restype = void
        z3.Z3_mk_lt.argtypes = [void, void, void]
        z3.Z3_mk_lt.restype = void
        z3.Z3_mk_and.argtypes = [
            void, ctypes.c_uint, void_array,
        ]
        z3.Z3_mk_and.restype = void
        z3.Z3_mk_or.argtypes = [
            void, ctypes.c_uint, void_array,
        ]
        z3.Z3_mk_or.restype = void
        z3.Z3_inc_ref.argtypes = [void, void]
        z3.Z3_dec_ref.argtypes = [void, void]
        z3.Z3_model_eval.argtypes = [
            void, void, void, ctypes.c_bool, ctypes.POINTER(void),
        ]
        z3.Z3_model_eval.restype = ctypes.c_bool
        z3.Z3_get_numeral_int64.argtypes = [
            void, void, ctypes.POINTER(ctypes.c_longlong),
        ]
        z3.Z3_get_numeral_int64.restype = ctypes.c_bool

    def _record_error(self, _context: int, code: int) -> None:
        self._error_codes.append(int(code))

    def _take_error(self, operation: str) -> None:
        if not self._error_codes:
            return
        code = self._error_codes[-1]
        self._error_codes.clear()
        message = self._z3.Z3_get_error_msg(
            self._context, code
        )
        detail = (
            message.decode("utf-8", errors="replace")
            if message else f"error code {code}"
        )
        raise ValueError(f"{operation}: {detail}")

    def _assert_smt2(self, smt2: str) -> None:
        assertions = self._z3.Z3_parse_smtlib2_string(
            self._context,
            smt2.encode("utf-8"),
            0,
            None,
            None,
            0,
            None,
            None,
        )
        self._take_error("invalid schedule SMT-LIB2")
        if not assertions:
            raise ValueError("schedule SMT-LIB2 parser returned no vector")
        self._z3.Z3_ast_vector_inc_ref(self._context, assertions)
        try:
            count = self._z3.Z3_ast_vector_size(
                self._context, assertions
            )
            self._take_error("cannot inspect parsed assertions")
            for index in range(count):
                assertion = self._z3.Z3_ast_vector_get(
                    self._context, assertions, index
                )
                self._take_error("cannot read parsed assertion")
                self._z3.Z3_solver_assert(
                    self._context, self._solver, assertion
                )
                self._take_error("cannot assert schedule constraint")
        finally:
            self._z3.Z3_ast_vector_dec_ref(
                self._context, assertions
            )

    def _constant(self, name: str, *, boolean: bool) -> int:
        key = (name, boolean)
        cached = self._constants.get(key)
        if cached is not None:
            return cached
        symbol = self._z3.Z3_mk_string_symbol(
            self._context, name.encode("utf-8")
        )
        sort = (
            self._z3.Z3_mk_bool_sort(self._context)
            if boolean else self._z3.Z3_mk_int_sort(self._context)
        )
        value = self._z3.Z3_mk_const(self._context, symbol, sort)
        self._take_error(f"cannot construct model symbol {name}")
        self._z3.Z3_inc_ref(self._context, value)
        self._retained_asts.append(value)
        self._constants[key] = value
        return value

    def _retain_ast(self, value: int) -> int:
        self._z3.Z3_inc_ref(self._context, value)
        self._retained_asts.append(value)
        return value

    def _bitvector_constant(self, name: str, bits: int) -> int:
        if bits < 1:
            raise ValueError("bit-vector model width must be positive")
        key = (name, bits)
        cached = self._bitvector_constants.get(key)
        if cached is not None:
            return cached
        symbol = self._z3.Z3_mk_string_symbol(
            self._context, name.encode("utf-8")
        )
        sort = self._z3.Z3_mk_bv_sort(self._context, bits)
        value = self._z3.Z3_mk_const(self._context, symbol, sort)
        self._take_error(f"cannot construct bit-vector symbol {name}")
        self._z3.Z3_inc_ref(self._context, value)
        self._retained_asts.append(value)
        self._bitvector_constants[key] = value
        return value

    def assert_order_choice(self, choice: Mapping[str, Any]) -> None:
        alternatives: list[int] = []
        for alternative in choice.get("alternatives", ()):
            comparisons: list[int] = []
            for edge in alternative.get("edges", ()):
                try:
                    before = int(edge[0])
                    after = int(edge[1])
                except (IndexError, TypeError, ValueError) as exc:
                    raise ValueError(
                        "invalid lifecycle refinement edge"
                    ) from exc
                comparisons.append(self._retain_ast(
                    self._z3.Z3_mk_lt(
                    self._context,
                    self._constant(
                        f"sync_ord_{before}", boolean=False
                    ),
                    self._constant(
                        f"sync_ord_{after}", boolean=False
                    ),
                    )
                ))
            if not comparisons:
                raise ValueError(
                    "lifecycle refinement alternative has no edge"
                )
            if len(comparisons) == 1:
                alternatives.append(comparisons[0])
            else:
                array = (ctypes.c_void_p * len(comparisons))(
                    *comparisons
                )
                alternatives.append(self._retain_ast(
                    self._z3.Z3_mk_and(
                        self._context, len(comparisons), array
                    )
                ))
            self._take_error("cannot construct refinement alternative")
        if not alternatives:
            raise ValueError("lifecycle refinement has no alternative")
        if len(alternatives) == 1:
            assertion = alternatives[0]
        else:
            array = (ctypes.c_void_p * len(alternatives))(
                *alternatives
            )
            assertion = self._retain_ast(self._z3.Z3_mk_or(
                self._context, len(alternatives), array
            ))
        self._take_error("cannot construct lifecycle refinement")
        self._z3.Z3_solver_assert(
            self._context, self._solver, assertion
        )
        self._take_error("cannot assert lifecycle refinement")

    def check(self, assumptions: Iterable[int]) -> str:
        values = list(assumptions)
        array = (
            (ctypes.c_void_p * len(values))(*values)
            if values else None
        )
        result = self._z3.Z3_solver_check_assumptions(
            self._context,
            self._solver,
            len(values),
            array,
        )
        self._take_error("schedule solver check failed")
        if result == self._Z3_L_TRUE:
            return "sat"
        if result == self._Z3_L_FALSE:
            return "unsat"
        return "unknown"

    def reason_unknown(self) -> str:
        reason = self._z3.Z3_solver_get_reason_unknown(
            self._context, self._solver
        )
        return (
            reason.decode("utf-8", errors="replace") if reason else ""
        )

    def integer_values(self, names: Iterable[str]) -> dict[str, int]:
        model = self._z3.Z3_solver_get_model(
            self._context, self._solver
        )
        self._take_error("cannot obtain schedule model")
        if not model:
            raise ValueError("satisfiable schedule has no model")
        self._z3.Z3_model_inc_ref(self._context, model)
        try:
            result: dict[str, int] = {}
            for name in names:
                constant = self._constant(name, boolean=False)
                interpreted = ctypes.c_void_p()
                if not self._z3.Z3_model_eval(
                    self._context,
                    model,
                    constant,
                    True,
                    ctypes.byref(interpreted),
                ):
                    self._take_error(f"cannot evaluate {name}")
                    raise ValueError(f"model has no value for {name}")
                number = ctypes.c_longlong()
                if not self._z3.Z3_get_numeral_int64(
                    self._context,
                    interpreted,
                    ctypes.byref(number),
                ):
                    self._take_error(f"non-integer model value for {name}")
                    raise ValueError(
                        f"model value for {name} is not an int64"
                    )
                result[name] = int(number.value)
            return result
        finally:
            self._z3.Z3_model_dec_ref(self._context, model)

    def bitvector_values(
        self,
        names: Mapping[str, int],
    ) -> dict[str, int]:
        model = self._z3.Z3_solver_get_model(
            self._context, self._solver
        )
        self._take_error("cannot obtain joint model")
        if not model:
            raise ValueError("satisfiable joint query has no model")
        self._z3.Z3_model_inc_ref(self._context, model)
        try:
            result: dict[str, int] = {}
            for name, bits in names.items():
                constant = self._bitvector_constant(name, int(bits))
                interpreted = ctypes.c_void_p()
                if not self._z3.Z3_model_eval(
                    self._context,
                    model,
                    constant,
                    True,
                    ctypes.byref(interpreted),
                ):
                    self._take_error(f"cannot evaluate {name}")
                    raise ValueError(f"model has no value for {name}")
                number = ctypes.c_longlong()
                if not self._z3.Z3_get_numeral_int64(
                    self._context,
                    interpreted,
                    ctypes.byref(number),
                ):
                    self._take_error(
                        f"non-numeral bit-vector model value for {name}"
                    )
                    raise ValueError(
                        f"model value for {name} is not an int64"
                    )
                result[name] = int(number.value)
            return result
        finally:
            self._z3.Z3_model_dec_ref(self._context, model)

    def close(self) -> None:
        if self._context:
            for value in reversed(self._retained_asts):
                self._z3.Z3_dec_ref(self._context, value)
            self._retained_asts.clear()
            if self._solver:
                self._z3.Z3_solver_dec_ref(
                    self._context, self._solver
                )
                self._solver = None
            self._z3.Z3_del_context(self._context)
            self._context = None

    def __enter__(self) -> "_SystemZ3Solver":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _verified_smt_text(
    container: Mapping[str, Any],
    field: str,
) -> str:
    value = container.get(field)
    if not isinstance(value, str):
        raise ValueError(f"schedule artifact has no {field}")
    expected = container.get(field + "_sha256")
    if expected is not None:
        actual = hashlib.sha256(value.encode("utf-8")).hexdigest()
        if expected != actual:
            raise ValueError(f"schedule artifact {field} digest mismatch")
    return value


def _violated_order_choices(
    choices: Iterable[Mapping[str, Any]],
    lifecycle_ranks: list[int],
) -> list[str]:
    violated: list[str] = []
    for choice in choices:
        satisfied = False
        for alternative in choice.get("alternatives", ()):
            edges = alternative.get("edges", ())
            try:
                satisfied = all(
                    lifecycle_ranks[int(edge[0])]
                    < lifecycle_ranks[int(edge[1])]
                    for edge in edges
                )
            except (IndexError, TypeError, ValueError):
                raise ValueError("invalid lifecycle choice edge")
            if satisfied:
                break
        if not satisfied:
            name = choice.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError("lifecycle choice has no name")
            violated.append(name)
    return violated


def solve_schedule_smt_query(
    artifact: Mapping[str, Any],
    query_index: int | None = 0,
    *,
    lazy_refinement: bool = True,
    max_refinement_rounds: int = 64,
    extra_smt2: str = "",
    extra_integer_model_names: Iterable[str] = (),
    extra_bitvector_model_names: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Solve a schedule query and construct a checked replay certificate."""
    if artifact.get("schema") not in SCHEDULE_SMT_SCHEMAS:
        raise ValueError("unsupported schedule-SMT artifact schema")
    if max_refinement_rounds < 0:
        raise ValueError("max_refinement_rounds must be non-negative")
    query: Mapping[str, Any] | None = None
    delta_smt2 = ""
    normalized_query_index: int | None = None
    if query_index is not None:
        try:
            normalized_query_index = int(query_index)
            query = artifact["queries"][normalized_query_index]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ValueError("invalid schedule-SMT query index") from exc
        if not isinstance(query, Mapping):
            raise ValueError("schedule-SMT query must be an object")
        delta_smt2 = _verified_smt_text(query, "delta_smt2")
    if not isinstance(extra_smt2, str):
        raise ValueError("extra_smt2 must be a string")
    extra_integer_names = [
        str(name) for name in extra_integer_model_names
    ]
    if len(set(extra_integer_names)) != len(extra_integer_names):
        raise ValueError("duplicate extra integer model name")
    extra_bitvector_names = {
        str(name): int(bits)
        for name, bits in (
            extra_bitvector_model_names or {}
        ).items()
    }
    if any(bits < 1 or bits > 64
           for bits in extra_bitvector_names.values()):
        raise ValueError("extra bit-vector model widths must be in [1, 64]")

    sync_state = artifact.get("sync_state", {})
    if not isinstance(sync_state, Mapping):
        raise ValueError("schedule artifact sync_state must be an object")
    order_ir = sync_state.get("order_ir", {})
    if not isinstance(order_ir, Mapping):
        raise ValueError("schedule artifact order_ir must be an object")
    raw_choices = order_ir.get("choice_constraints", [])
    if not isinstance(raw_choices, list):
        raise ValueError("schedule artifact choices must be a list")
    choices = [
        choice for choice in raw_choices if isinstance(choice, Mapping)
    ]
    if len(choices) != len(raw_choices):
        raise ValueError("schedule artifact contains invalid choices")

    refinements = sync_state.get("lazy_refinements", [])
    if not isinstance(refinements, list):
        raise ValueError("schedule lazy refinements must be a list")
    refinement_by_name: dict[str, Mapping[str, Any]] = {}
    for refinement in refinements:
        if not isinstance(refinement, Mapping):
            raise ValueError("invalid lazy refinement")
        name = refinement.get("name")
        if (
            not isinstance(name, str)
            or not name
            or name in refinement_by_name
        ):
            raise ValueError("invalid lazy refinement identity")
        refinement_by_name[name] = refinement
    choice_by_name = {
        str(choice.get("name")): choice for choice in choices
    }

    use_lazy = bool(
        lazy_refinement
        and artifact.get("schema") == SCHEDULE_SMT_SCHEMA
        and choices
    )
    base_field = "relaxed_base_smt2" if use_lazy else "base_smt2"
    base_smt2 = _verified_smt_text(artifact, base_field)
    if use_lazy and set(refinement_by_name) != {
        str(choice.get("name")) for choice in choices
    }:
        raise ValueError("choice/refinement identities do not match")

    event_count = int(artifact.get("modeled_event_count", 0))
    lifecycle_count = int(
        sync_state.get("modeled_lifecycle_event_count", 0)
    )
    if event_count < 0 or lifecycle_count < 0:
        raise ValueError("negative schedule model dimensions")
    position_names = [f"pos_{index}" for index in range(event_count)]
    rank_names = [
        f"sync_ord_{index}" for index in range(lifecycle_count)
    ]
    active_names: list[str] = []
    check_count = 0
    refinement_round_count = 0
    combined_smt2 = base_smt2 + delta_smt2 + extra_smt2

    with _SystemZ3Solver(combined_smt2) as solver:
        while True:
            check_count += 1
            status = solver.check(())
            result: dict[str, Any] = {
                "schema": "symcc-schedule-solve-result-v1",
                "status": status,
                "solver": "system-libz3-c-api",
                "mode": (
                    "violation-driven-lazy-refinement"
                    if use_lazy else "eager-exact"
                ),
                "query_index": normalized_query_index,
                "solver_check_count": check_count,
                "refinement_round_count": refinement_round_count,
                "activated_refinement_count": len(active_names),
                "activated_refinements": list(active_names),
                "candidate_refinement_count": len(choices),
                "exact_semantics": True,
            }
            if status != "sat":
                if status == "unknown":
                    result["reason_unknown"] = solver.reason_unknown()
                return result

            assignments = solver.integer_values(
                position_names + rank_names + extra_integer_names
            )
            event_positions = [
                assignments[name] for name in position_names
            ]
            lifecycle_ranks = [
                assignments[name] for name in rank_names
            ]
            violated = _violated_order_choices(
                choices, lifecycle_ranks
            )
            inactive_violations = [
                name for name in violated if name not in active_names
            ]
            if not violated:
                certificate = schedule_linear_extension_certificate(
                    dict(artifact),
                    event_positions=event_positions,
                    lifecycle_ranks=lifecycle_ranks,
                    query_index=normalized_query_index,
                )
                if not verify_schedule_linear_extension_certificate(
                    dict(artifact), certificate
                ):
                    raise ValueError(
                        "solver produced an invalid schedule certificate"
                    )
                result["model"] = {
                    "event_positions": event_positions,
                    "lifecycle_ranks": lifecycle_ranks,
                    "assignments": assignments,
                }
                if extra_integer_names:
                    result["model"]["extra_integers"] = {
                        name: assignments[name]
                        for name in extra_integer_names
                    }
                if extra_bitvector_names:
                    result["model"]["extra_bitvectors"] = (
                        solver.bitvector_values(extra_bitvector_names)
                    )
                result["certificate"] = certificate
                result["runtime_replayable"] = certificate[
                    "runtime_replayable"
                ]
                result["replay_prefix"] = list(
                    certificate["replay_prefix"]
                )
                return result
            if not use_lazy or not inactive_violations:
                raise ValueError(
                    "satisfiable model violates an active hard choice"
                )
            if refinement_round_count >= max_refinement_rounds:
                result["status"] = "refinement_limit"
                result["exact_semantics"] = False
                result["remaining_violations"] = violated
                return result
            for name in inactive_violations:
                solver.assert_order_choice(choice_by_name[name])
                active_names.append(name)
            refinement_round_count += 1


def _strip_smt2_control_commands(smt2: str) -> str:
    """Remove solver-control commands while preserving SMT declarations."""
    skipped = {
        "check-sat",
        "check-sat-assuming",
        "exit",
        "get-model",
        "get-value",
        "pop",
        "push",
        "reset",
        "reset-assertions",
        "set-logic",
    }
    commands: list[str] = []
    index = 0
    length = len(smt2)
    while index < length:
        while index < length:
            if smt2[index].isspace():
                index += 1
                continue
            if smt2[index] == ";":
                newline = smt2.find("\n", index)
                index = length if newline < 0 else newline + 1
                continue
            break
        if index >= length:
            break
        if smt2[index] != "(":
            newline = smt2.find("\n", index)
            index = length if newline < 0 else newline + 1
            continue
        start = index
        depth = 0
        quoted_symbol = False
        string_literal = False
        while index < length:
            char = smt2[index]
            if string_literal:
                if char == '"':
                    if index + 1 < length and smt2[index + 1] == '"':
                        index += 2
                        continue
                    string_literal = False
            elif quoted_symbol:
                if char == "|":
                    quoted_symbol = False
            elif char == '"':
                string_literal = True
            elif char == "|":
                quoted_symbol = True
            elif char == ";":
                newline = smt2.find("\n", index)
                if newline < 0:
                    index = length
                    break
                index = newline
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    index += 1
                    break
            index += 1
        if depth != 0:
            raise ValueError("unterminated SMT-LIB2 command")
        command = smt2[start:index].strip()
        match = re.match(r"\(\s*([^\s()]+)", command)
        if match is None:
            raise ValueError("invalid SMT-LIB2 command")
        if match.group(1).lower() not in skipped:
            commands.append(command)
    return "\n".join(commands) + ("\n" if commands else "")


def _tag_uint(tags: Iterable[str], name: str) -> int | None:
    prefix = name + "="
    for tag in tags:
        if not tag.startswith(prefix):
            continue
        try:
            value = int(tag[len(prefix):], 0)
        except ValueError:
            return None
        return value if value >= 0 else None
    return None


def _schedule_read_from_context(
    artifact: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Build optional Query IR value bridges for encoded reads-from choices."""
    raw_events = artifact.get("events", ())
    if not isinstance(raw_events, list):
        raise ValueError("schedule artifact events must be a list")
    events: dict[int, dict[str, Any]] = {}
    for index, row in enumerate(raw_events):
        if not isinstance(row, Mapping):
            raise ValueError("invalid modeled schedule event")
        events[index] = {
            "index": index,
            "seq": int(row["seq"]),
            "tid": int(row["tid"]),
            "op": str(row["op"]),
            "object": str(row["object"]),
            "tags": tuple(str(tag) for tag in row.get("tags", ())),
        }
    encoded = artifact.get("memory_consistency")
    if not isinstance(encoded, Mapping):
        raise ValueError("schedule artifact has no memory consistency context")
    rows_raw = encoded.get("reads", ())
    if not isinstance(rows_raw, list):
        raise ValueError("invalid memory consistency read metadata")
    rows = [dict(row) for row in rows_raw if isinstance(row, Mapping)]
    if len(rows) != len(rows_raw):
        raise ValueError("invalid reads-from metadata")

    lines = ["; Query IR byte-value bridges for encoded reads-from choices"]
    bridges: list[dict[str, Any]] = []
    for row in rows:
        read_index = int(row["read_index"])
        read = events[read_index]
        name = str(row["variable"])
        byte_index = _tag_uint(read["tags"], "sym-byte")
        source_values: dict[str, int] = {}
        init_value = _tag_uint(read["tags"], "init")
        if init_value is not None and init_value <= 255:
            source_values["-1"] = init_value
        for source in row.get("sources", ()):
            source_index = int(source)
            if source_index < 0:
                continue
            write = events.get(source_index)
            if write is None:
                raise ValueError("reads-from source is outside modeled events")
            value = _tag_uint(write["tags"], "value")
            if value is not None and value <= 255:
                source_values[str(source_index)] = value
        if byte_index is not None:
            for source, value in sorted(
                source_values.items(), key=lambda item: int(item[0])
            ):
                lines.append(
                    f"(assert (! (=> (= {name} "
                    f"{_smt_integer(int(source))}) "
                    f"(= |{byte_index}| #x{value:02x})) "
                    f":named rf_value_{read_index}_{source.replace('-', 'i')}))"
                )
            if source_values:
                bridges.append({
                    "read_index": read_index,
                    "byte_index": byte_index,
                    "source_values": source_values,
                })
    smt2 = "\n".join(lines) + "\n"
    return smt2, {
        "memory_model": str(encoded.get("model", "SC")),
        "semantics": str(encoded.get("semantics", "")),
        "read_count": len(rows),
        "choice_count": sum(len(row["sources"]) for row in rows),
        "value_bridge_count": len(bridges),
        "reads": rows,
        "value_bridges": bridges,
        "smt2_sha256": hashlib.sha256(
            smt2.encode("utf-8")
        ).hexdigest(),
    }


def _joint_result_digest(result: Mapping[str, Any]) -> str:
    return _canonical_json_digest({
        key: value
        for key, value in result.items()
        if key != "result_sha256"
    })


def solve_joint_path_schedule_query(
    artifact: Mapping[str, Any],
    query_smt2: str,
    *,
    query_id: str = "",
    query_index: int = 0,
    query_byte_offsets: Iterable[int] = (),
) -> dict[str, Any]:
    """Solve Query IR path, SC schedule, and reads-from in one Z3 context."""
    if not isinstance(query_smt2, str) or not query_smt2.strip():
        raise ValueError("joint path SMT-LIB2 must be non-empty")
    path_smt2 = _strip_smt2_control_commands(query_smt2)
    rf_smt2, rf_metadata = _schedule_read_from_context(artifact)
    byte_offsets = sorted({
        int(offset) for offset in query_byte_offsets
        if 0 <= int(offset) <= (1 << 32) - 1
    })
    if not byte_offsets:
        byte_offsets = sorted({
            int(match)
            for match in re.findall(
                r"\(declare-fun\s+\|([0-9]+)\|\s+\(\)\s+"
                r"\(_\s+BitVec\s+8\)\s*\)",
                path_smt2,
            )
        })
    rf_names = [
        str(row["variable"]) for row in rf_metadata["reads"]
    ]
    schedule_result = solve_schedule_smt_query(
        artifact,
        query_index,
        extra_smt2=path_smt2 + rf_smt2,
        extra_integer_model_names=rf_names,
        extra_bitvector_model_names={
            str(offset): 8 for offset in byte_offsets
        },
    )
    status = str(schedule_result["status"])
    result: dict[str, Any] = {
        "schema": JOINT_PATH_SCHEDULE_SCHEMA,
        "status": status,
        "query_id": str(query_id),
        "query_index": int(query_index),
        "schedule_artifact_sha256": str(
            artifact.get("artifact_sha256", "")
        ),
        "schedule_trace_digest": str(
            artifact.get("trace_digest", "")
        ),
        "query_smt2_sha256": hashlib.sha256(
            query_smt2.encode("utf-8")
        ).hexdigest(),
        "normalized_query_smt2_sha256": hashlib.sha256(
            path_smt2.encode("utf-8")
        ).hexdigest(),
        "read_from": rf_metadata,
        "schedule_solve": schedule_result,
        "single_solver_context": True,
        "exact_for_encoded_constraints": bool(
            schedule_result.get("exact_semantics", False)
        ),
        "scope": (
            "query-ir-path + bounded schedule + "
            f"{rf_metadata['memory_model']} reads-from"
        ),
        "not_encoded": [
            "runtime_enabledness",
            "path-dependent_event_existence",
            "memory_values_without_explicit_trace_value_tags",
            "alias_equivalence_beyond_runtime_object_identity",
        ],
    }
    if status == "sat":
        model = schedule_result.get("model", {})
        if not isinstance(model, Mapping):
            raise ValueError("joint SAT result has no model")
        extra_integers = model.get("extra_integers", {})
        extra_bitvectors = model.get("extra_bitvectors", {})
        result["model"] = {
            "input_bytes": dict(extra_bitvectors),
            "read_from": {
                row["variable"]: int(extra_integers[row["variable"]])
                for row in rf_metadata["reads"]
            },
            "event_positions": list(model.get("event_positions", ())),
            "lifecycle_ranks": list(model.get("lifecycle_ranks", ())),
        }
    result["result_sha256"] = _joint_result_digest(result)
    return result


def verify_joint_path_schedule_result(
    artifact: Mapping[str, Any],
    query_smt2: str,
    result: Mapping[str, Any],
    *,
    recheck_solver: bool = True,
) -> bool:
    """Check hashes, schedule certificate, reads-from model, and solver status."""
    try:
        if result.get("schema") != JOINT_PATH_SCHEDULE_SCHEMA:
            return False
        if result.get("result_sha256") != _joint_result_digest(result):
            return False
        if result.get("query_smt2_sha256") != hashlib.sha256(
            query_smt2.encode("utf-8")
        ).hexdigest():
            return False
        schedule_result = result.get("schedule_solve")
        if not isinstance(schedule_result, Mapping):
            return False
        if schedule_result.get("status") != result.get("status"):
            return False
        if result.get("status") == "sat":
            certificate = schedule_result.get("certificate")
            if (
                not isinstance(certificate, Mapping)
                or not verify_schedule_linear_extension_certificate(
                    dict(artifact), certificate
                )
            ):
                return False
            model = result.get("model")
            rf_metadata = result.get("read_from")
            if not isinstance(model, Mapping) or not isinstance(
                rf_metadata, Mapping
            ):
                return False
            positions = [int(value) for value in model["event_positions"]]
            rf_model = model["read_from"]
            if not isinstance(rf_model, Mapping):
                return False
            encoded_model = str(
                rf_metadata.get("memory_model", "SC")
            )
            for row in rf_metadata.get("reads", ()):
                read_index = int(row["read_index"])
                source = int(rf_model[row["variable"]])
                sources = [int(value) for value in row["sources"]]
                if source not in sources:
                    return False
                if encoded_model == "SC":
                    preceding = [
                        candidate for candidate in sources
                        if candidate >= 0
                        and positions[candidate] < positions[read_index]
                    ]
                    expected = (
                        max(preceding, key=lambda item: positions[item])
                        if preceding else -1
                    )
                    if source != expected:
                        return False
            input_bytes = model.get("input_bytes", {})
            for bridge in rf_metadata.get("value_bridges", ()):
                source = str(rf_model[f"rf_{bridge['read_index']}"])
                expected = bridge["source_values"].get(source)
                if expected is not None and int(
                    input_bytes[str(bridge["byte_index"])]
                ) != int(expected):
                    return False
        if recheck_solver:
            repeated = solve_joint_path_schedule_query(
                artifact,
                query_smt2,
                query_id=str(result.get("query_id", "")),
                query_index=int(result.get("query_index", 0)),
                query_byte_offsets=(
                    int(offset)
                    for offset in result.get("model", {}).get(
                        "input_bytes", {}
                    )
                ),
            )
            if repeated.get("status") != result.get("status"):
                return False
        return True
    except (
        IndexError,
        KeyError,
        TypeError,
        ValueError,
        OverflowError,
        RuntimeError,
    ):
        return False


def _indexed_integer_assignment(
    raw: Any,
    *,
    prefix: str,
    count: int,
    default: list[int] | None = None,
) -> list[int]:
    if raw is None:
        if default is None:
            raise ValueError(f"missing {prefix} assignment")
        return list(default)
    values: list[Any]
    if isinstance(raw, Mapping):
        values = []
        for index in range(count):
            value = None
            found = False
            for key in (index, str(index), f"{prefix}_{index}"):
                if key in raw:
                    value = raw[key]
                    found = True
                    break
            if not found:
                raise ValueError(
                    f"missing {prefix}_{index} assignment"
                )
            values.append(value)
    elif isinstance(raw, Iterable) and not isinstance(
        raw, (str, bytes, bytearray)
    ):
        values = list(raw)
        if len(values) != count:
            raise ValueError(
                f"{prefix} assignment has {len(values)} values, "
                f"expected {count}"
            )
    else:
        raise ValueError(f"invalid {prefix} assignment")
    result: list[int] = []
    for value in values:
        if isinstance(value, bool):
            raise ValueError(f"invalid Boolean {prefix} assignment")
        try:
            result.append(int(value))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"invalid integer in {prefix} assignment"
            ) from exc
    return result


def _topological_extension(
    event_count: int,
    edges: Iterable[tuple[int, int]],
    priorities: list[int],
) -> list[int]:
    successors: list[set[int]] = [set() for _ in range(event_count)]
    indegree = [0] * event_count
    for before, after in edges:
        if (before < 0 or after < 0
                or before >= event_count or after >= event_count
                or before == after):
            raise ValueError("invalid lifecycle order edge")
        if after in successors[before]:
            continue
        successors[before].add(after)
        indegree[after] += 1
    ready = [
        (priorities[index], index)
        for index in range(event_count)
        if indegree[index] == 0
    ]
    heapq.heapify(ready)
    order: list[int] = []
    while ready:
        _, current = heapq.heappop(ready)
        order.append(current)
        for successor in sorted(successors[current]):
            indegree[successor] -= 1
            if indegree[successor] == 0:
                heapq.heappush(
                    ready,
                    (priorities[successor], successor),
                )
    if len(order) != event_count:
        raise ValueError("lifecycle order constraints contain a cycle")
    return order


def _embed_extension_with_anchors(
    order: list[int],
    anchors: list[dict[str, Any]],
    event_positions: list[int],
    stride: int,
) -> list[int]:
    if not order:
        return []
    order_offsets = {
        lifecycle_index: offset
        for offset, lifecycle_index in enumerate(order)
    }
    targets = []
    for anchor in anchors:
        lifecycle_index = int(anchor["lifecycle_index"])
        position_index = int(anchor["position_index"])
        targets.append((
            order_offsets[lifecycle_index],
            lifecycle_index,
            event_positions[position_index] * stride,
        ))
    targets.sort()
    for left, right in zip(targets, targets[1:]):
        if left[2] >= right[2]:
            raise ValueError(
                "linear extension reverses controlled event positions"
            )

    ranks: list[int | None] = [None] * len(order)
    if not targets:
        for offset, lifecycle_index in enumerate(order):
            ranks[lifecycle_index] = offset
        return [int(value) for value in ranks]

    first_offset, first_index, first_rank = targets[0]
    ranks[first_index] = first_rank
    for offset in range(first_offset - 1, -1, -1):
        ranks[order[offset]] = first_rank - (first_offset - offset)

    for left, right in zip(targets, targets[1:]):
        left_offset, _, left_rank = left
        right_offset, right_index, right_rank = right
        gap_count = right_offset - left_offset - 1
        if right_rank - left_rank <= gap_count:
            raise ValueError("scaled anchors leave insufficient rank space")
        for delta in range(1, gap_count + 1):
            ranks[order[left_offset + delta]] = left_rank + delta
        ranks[right_index] = right_rank

    last_offset, last_index, last_rank = targets[-1]
    ranks[last_index] = last_rank
    for offset in range(last_offset + 1, len(order)):
        ranks[order[offset]] = last_rank + (offset - last_offset)
    if any(value is None for value in ranks):
        raise ValueError("failed to embed lifecycle linear extension")
    return [int(value) for value in ranks]


def _certificate_digest(certificate: Mapping[str, Any]) -> str:
    payload = {
        str(key): value
        for key, value in certificate.items()
        if key != "certificate_sha256"
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _query_positions_satisfied(
    artifact: Mapping[str, Any],
    query_index: int,
    event_positions: list[int],
) -> bool:
    try:
        query = artifact["queries"][query_index]
        events = artifact["events"]
        by_thread: dict[int, list[int]] = {}
        sequence_positions: dict[int, int] = {}
        for index, event in enumerate(events):
            by_thread.setdefault(int(event["tid"]), []).append(index)
            sequence_positions[int(event["seq"])] = index
        occurrences: dict[int, int] = {}
        for slot, raw_tid in enumerate(query["prefix"]):
            tid = int(raw_tid)
            occurrence = occurrences.get(tid, 0)
            thread_events = by_thread.get(tid, [])
            if occurrence >= len(thread_events):
                return False
            if event_positions[thread_events[occurrence]] != slot:
                return False
            occurrences[tid] = occurrence + 1
        conflict = query["conflict"]
        left = sequence_positions[int(conflict["left_seq"])]
        right = sequence_positions[int(conflict["right_seq"])]
        return event_positions[right] < event_positions[left]
    except (KeyError, IndexError, TypeError, ValueError):
        return False


def _event_program_order_satisfied(
    artifact: Mapping[str, Any],
    event_positions: list[int],
) -> bool:
    try:
        events = [
            ScheduleEvent(
                seq=int(event["seq"]),
                tid=int(event["tid"]),
                op=str(event["op"]),
                obj=str(event["object"]),
                tags=tuple(str(tag) for tag in event.get("tags", ())),
            )
            for event in artifact["events"]
        ]
        return all(
            event_positions[left] < event_positions[right]
            for left, right in _program_order_pairs(
                events,
                str(artifact.get("memory_model", "SC")),
            )
        )
    except (KeyError, IndexError, TypeError, ValueError):
        return False


def _query_runtime_replayable(
    artifact: Mapping[str, Any],
    query_index: int | None,
) -> bool:
    if _normalize_memory_model(
        artifact.get("memory_model", "SC")
    ) != "SC":
        return False
    if query_index is None:
        return True
    try:
        query = artifact["queries"][query_index]
        events = artifact["events"]
        by_thread: dict[int, list[Mapping[str, Any]]] = {}
        by_sequence: dict[int, Mapping[str, Any]] = {}
        for event in events:
            by_thread.setdefault(int(event["tid"]), []).append(event)
            by_sequence[int(event["seq"])] = event
        occurrences: dict[int, int] = {}
        for raw_tid in query["prefix"]:
            tid = int(raw_tid)
            occurrence = occurrences.get(tid, 0)
            thread_events = by_thread.get(tid, [])
            if (
                occurrence >= len(thread_events)
                or not bool(thread_events[occurrence].get("controlled"))
            ):
                return False
            occurrences[tid] = occurrence + 1
        conflict = query["conflict"]
        return all(
            bool(by_sequence[int(conflict[key])].get("controlled"))
            for key in ("left_seq", "right_seq")
        )
    except (KeyError, IndexError, TypeError, ValueError):
        return False


def verify_schedule_linear_extension_certificate(
    artifact: Mapping[str, Any],
    certificate: Mapping[str, Any],
) -> bool:
    """Check a constructive lifecycle total-order and replay certificate."""
    try:
        if certificate.get("schema") != SCHEDULE_LINEAR_EXTENSION_SCHEMA:
            return False
        if certificate.get("trace_digest") != artifact.get("trace_digest"):
            return False
        if certificate.get("base_smt2_sha256") != artifact.get(
            "base_smt2_sha256"
        ):
            return False
        if certificate.get("certificate_sha256") != _certificate_digest(
            certificate
        ):
            return False
        sync_state = artifact["sync_state"]
        order_ir = sync_state["order_ir"]
        if order_ir.get("schema") != SCHEDULE_ORDER_IR_SCHEMA:
            return False
        event_count = int(order_ir["event_count"])
        position_count = int(artifact["modeled_event_count"])
        event_positions = _indexed_integer_assignment(
            certificate.get("source_event_positions"),
            prefix="pos",
            count=position_count,
        )
        if sorted(event_positions) != list(range(position_count)):
            return False
        if not _event_program_order_satisfied(
            artifact,
            event_positions,
        ):
            return False
        query_index_raw = certificate.get("query_index")
        if query_index_raw is not None:
            query_index = int(query_index_raw)
            if query_index < 0 or not _query_positions_satisfied(
                artifact,
                query_index,
                event_positions,
            ):
                return False
        else:
            query_index = None
        if certificate.get("runtime_replayable") is not (
            _query_runtime_replayable(artifact, query_index)
        ):
            return False
        if certificate.get("scope") != "hard_lifecycle_order_and_query":
            return False
        if certificate.get("optional_assumptions_certified") is not False:
            return False
        source_ranks = _indexed_integer_assignment(
            certificate.get("source_lifecycle_ranks"),
            prefix="sync_ord",
            count=event_count,
        )
        extension = _indexed_integer_assignment(
            certificate.get("linear_extension"),
            prefix="extension",
            count=event_count,
        )
        if sorted(extension) != list(range(event_count)):
            return False
        total_ranks = [0] * event_count
        for rank, lifecycle_index in enumerate(extension):
            total_ranks[lifecycle_index] = rank

        fixed_edges = [
            (int(edge["before"]), int(edge["after"]))
            for edge in order_ir.get("fixed_edges", [])
        ]
        for before, after in fixed_edges:
            if not (
                source_ranks[before] < source_ranks[after]
                and total_ranks[before] < total_ranks[after]
            ):
                return False

        selected_raw = certificate.get("selected_choices", [])
        selected = {
            str(row["name"]): int(row["alternative"])
            for row in selected_raw
        }
        choices = order_ir.get("choice_constraints", [])
        if len(selected) != len(choices):
            return False
        selected_edges: list[tuple[int, int]] = []
        for choice in choices:
            name = str(choice["name"])
            alternatives = choice["alternatives"]
            alternative = selected.get(name, -1)
            if alternative < 0 or alternative >= len(alternatives):
                return False
            for before, after in alternatives[alternative]["edges"]:
                before = int(before)
                after = int(after)
                if not (
                    source_ranks[before] < source_ranks[after]
                    and total_ranks[before] < total_ranks[after]
                ):
                    return False
                selected_edges.append((before, after))

        anchors = order_ir.get("controlled_anchors", [])
        anchor_rows = [
            (
                int(anchor["position_index"]),
                int(anchor["lifecycle_index"]),
            )
            for anchor in anchors
        ]
        order_encoding = str(sync_state.get("order_encoding", "partial"))
        if order_encoding == "partial":
            stride = int(order_ir["anchor_stride"])
            if stride <= event_count:
                return False
            for position_index, lifecycle_index in anchor_rows:
                if source_ranks[lifecycle_index] != (
                    event_positions[position_index] * stride
                ):
                    return False
        else:
            if sorted(source_ranks) != list(range(event_count)):
                return False
            for left_offset, left in enumerate(anchor_rows):
                for right in anchor_rows[left_offset + 1:]:
                    left_pos, left_lifecycle = left
                    right_pos, right_lifecycle = right
                    if (
                        event_positions[left_pos]
                        < event_positions[right_pos]
                    ) != (
                        source_ranks[left_lifecycle]
                        < source_ranks[right_lifecycle]
                    ):
                        return False

        controlled_order = [
            lifecycle_index
            for _, lifecycle_index in sorted(
                anchor_rows,
                key=lambda row: event_positions[row[0]],
            )
        ]
        extension_controlled = [
            lifecycle_index
            for lifecycle_index in extension
            if lifecycle_index in set(controlled_order)
        ]
        if extension_controlled != controlled_order:
            return False
        if certificate.get("controlled_lifecycle_order") != controlled_order:
            return False

        events = order_ir["events"]
        replay_prefix = [
            int(events[lifecycle_index]["tid"])
            for lifecycle_index in controlled_order
        ]
        if certificate.get("replay_prefix") != replay_prefix:
            return False
        if certificate.get("total_lifecycle_ranks") != total_ranks:
            return False
        return True
    except (KeyError, TypeError, ValueError, IndexError):
        return False


def schedule_linear_extension_certificate(
    artifact: Mapping[str, Any],
    *,
    event_positions: Any = None,
    lifecycle_ranks: Any = None,
    query_index: int | None = None,
) -> dict[str, Any]:
    """Construct and verify a total lifecycle order from a partial model.

    When assignments are omitted, the observed trace order is embedded into the
    same scaled-anchor representation. Query-model materialization should pass
    complete ``pos_*`` and ``sync_ord_*`` assignments.
    """
    try:
        sync_state = artifact["sync_state"]
        order_ir = sync_state["order_ir"]
        if order_ir.get("schema") != SCHEDULE_ORDER_IR_SCHEMA:
            raise ValueError("unsupported lifecycle order IR")
        event_count = int(order_ir["event_count"])
        position_count = int(artifact["modeled_event_count"])
        positions = _indexed_integer_assignment(
            event_positions,
            prefix="pos",
            count=position_count,
            default=list(range(position_count)),
        )
        if sorted(positions) != list(range(position_count)):
            raise ValueError("event positions are not a bounded permutation")
        if not _event_program_order_satisfied(artifact, positions):
            raise ValueError(
                "event positions violate per-thread program order"
            )
        selected_query = None
        if query_index is not None:
            selected_query = int(query_index)
            if selected_query < 0 or not _query_positions_satisfied(
                artifact,
                selected_query,
                positions,
            ):
                raise ValueError(
                    "event positions do not satisfy schedule query"
                )

        anchors = list(order_ir.get("controlled_anchors", []))
        fixed_edges = [
            (int(edge["before"]), int(edge["after"]))
            for edge in order_ir.get("fixed_edges", [])
        ]
        choices = list(order_ir.get("choice_constraints", []))
        provided_ranks = lifecycle_ranks is not None
        ranks = (
            _indexed_integer_assignment(
                lifecycle_ranks,
                prefix="sync_ord",
                count=event_count,
            )
            if provided_ranks
            else list(range(event_count))
        )
        priority = ranks if provided_ranks else list(range(event_count))

        selected_choices: list[dict[str, Any]] = []
        selected_edges: list[tuple[int, int]] = []
        for choice in choices:
            selected_index = -1
            for alternative_index, alternative in enumerate(
                choice["alternatives"]
            ):
                edges = [
                    (int(before), int(after))
                    for before, after in alternative["edges"]
                ]
                if all(
                    priority[before] < priority[after]
                    for before, after in edges
                ):
                    selected_index = alternative_index
                    selected_edges.extend(edges)
                    break
            if selected_index < 0:
                raise ValueError(
                    f"no satisfied alternative for {choice['name']}"
                )
            selected_choices.append({
                "name": str(choice["name"]),
                "alternative": selected_index,
            })

        controlled_edges: list[tuple[int, int]] = []
        controlled_order = [
            int(anchor["lifecycle_index"])
            for anchor in sorted(
                anchors,
                key=lambda anchor: positions[
                    int(anchor["position_index"])
                ],
            )
        ]
        controlled_edges.extend(zip(
            controlled_order,
            controlled_order[1:],
        ))
        extension = _topological_extension(
            event_count,
            fixed_edges + selected_edges + controlled_edges,
            priority,
        )

        if not provided_ranks:
            if str(sync_state.get("order_encoding")) == "partial":
                ranks = _embed_extension_with_anchors(
                    extension,
                    anchors,
                    positions,
                    int(order_ir["anchor_stride"]),
                )
            else:
                ranks = [0] * event_count
                for rank, lifecycle_index in enumerate(extension):
                    ranks[lifecycle_index] = rank

        total_ranks = [0] * event_count
        for rank, lifecycle_index in enumerate(extension):
            total_ranks[lifecycle_index] = rank
        events = order_ir["events"]
        replay_prefix = [
            int(events[lifecycle_index]["tid"])
            for lifecycle_index in controlled_order
        ]
        certificate: dict[str, Any] = {
            "schema": SCHEDULE_LINEAR_EXTENSION_SCHEMA,
            "trace_digest": artifact.get("trace_digest", ""),
            "base_smt2_sha256": artifact.get("base_smt2_sha256", ""),
            "order_encoding": sync_state.get(
                "order_encoding", "partial"
            ),
            "source": (
                "solver_query_model"
                if provided_ranks and selected_query is not None
                else "solver_base_model"
                if provided_ranks
                else "observed_trace"
            ),
            "scope": "hard_lifecycle_order_and_query",
            "optional_assumptions_certified": False,
            "query_index": selected_query,
            "runtime_replayable": _query_runtime_replayable(
                artifact,
                selected_query,
            ),
            "source_event_positions": positions,
            "source_lifecycle_ranks": ranks,
            "selected_choices": selected_choices,
            "linear_extension": extension,
            "total_lifecycle_ranks": total_ranks,
            "controlled_lifecycle_order": controlled_order,
            "replay_prefix": replay_prefix,
            "event_count": event_count,
            "controlled_event_count": len(controlled_order),
        }
        certificate["certificate_sha256"] = _certificate_digest(
            certificate
        )
        if not verify_schedule_linear_extension_certificate(
            artifact, certificate
        ):
            raise ValueError("generated linear-extension certificate failed")
        return certificate
    except (KeyError, TypeError, IndexError) as exc:
        raise ValueError("invalid schedule-SMT order artifact") from exc


def write_schedule_linear_extension_prefix(
    path: str,
    artifact: Mapping[str, Any],
    certificate: Mapping[str, Any] | None = None,
) -> bool:
    """Write the verified controlled-thread projection of a total order."""
    selected = (
        certificate
        if certificate is not None
        else artifact.get("observed_linear_extension")
    )
    if not isinstance(selected, Mapping):
        return False
    if not verify_schedule_linear_extension_certificate(
        artifact, selected
    ):
        return False
    if selected.get("runtime_replayable") is not True:
        return False
    return write_schedule_prefix(path, selected.get("replay_prefix", ()))


def schedule_smt_artifact(
    events: list[ScheduleEvent],
    *,
    input_id: str = "",
    target_branch: int = 0,
    current_prefix: Iterable[int] = (),
    max_depth: int = 64,
    max_window: int = 32,
    max_prefixes: int = 256,
    max_events: int = 128,
    max_memory_events: int = 32,
    max_queries: int = 64,
    sync_state: bool = True,
    order_encoding: str = "partial",
    memory_model: str = "SC",
) -> dict[str, Any]:
    """Build bounded SMT-LIB2 replay queries for conflict-derived prefixes.

    The hard constraints model an SC, TSO, or bounded C11 release/acquire
    execution, a replay thread prefix, and reversal of the source conflict.
    Observed vector-clock HB edges remain optional because they may change
    under an alternative schedule.
    """
    depth = max(1, int(max_depth))
    window = max(1, int(max_window))
    prefix_cap = max(1, int(max_prefixes))
    event_cap = min(512, max(2, int(max_events)))
    memory_event_cap = min(64, max(0, int(max_memory_events)))
    query_cap = min(512, max(1, int(max_queries)))
    lifecycle_order = _normalize_schedule_order_encoding(order_encoding)
    normalized_memory_model = _normalize_memory_model(memory_model)
    current = normalize_schedule_prefix(current_prefix, max_len=depth)
    ordered_events = sorted(events, key=lambda event: event.seq)
    schedulable = [
        event for event in ordered_events if event.schedulable
    ]
    modeled_events = schedulable[:event_cap]
    modeled_sequences = {event.seq for event in modeled_events}
    observed_points = {
        point.event.seq: point
        for point in annotate_schedule(ordered_events)
    }
    (
        sync_state_smt2,
        relaxed_sync_state_smt2,
        sync_state_metadata,
    ) = _schedule_sync_state_context(
        ordered_events,
        modeled_events,
        enabled=bool(sync_state),
        max_events=min(512, event_cap * 5),
        order_encoding=lifecycle_order,
    )
    memory_model_smt2, memory_model_metadata = _memory_model_context(
        modeled_events,
        memory_model=normalized_memory_model,
        max_memory_events=memory_event_cap,
    )
    base_smt2, program_order_count, observed_hb_assumptions = (
        _schedule_smt_base(
            modeled_events,
            observed_points,
            sync_state_smt2,
            memory_model=normalized_memory_model,
            memory_model_smt2=memory_model_smt2,
        )
    )
    relaxed_base_smt2, relaxed_program_order_count, relaxed_hb = (
        _schedule_smt_base(
            modeled_events,
            observed_points,
            relaxed_sync_state_smt2,
            memory_model=normalized_memory_model,
            memory_model_smt2=memory_model_smt2,
        )
    )
    if (
        relaxed_program_order_count != program_order_count
        or relaxed_hb != observed_hb_assumptions
    ):
        raise AssertionError("exact and relaxed schedule contexts diverged")
    candidates = _source_replay_candidates(
        ordered_events,
        current,
        max_depth=depth,
        max_window=window,
        max_prefixes=prefix_cap,
    )
    queries: list[dict[str, Any]] = []
    dropped_event_bound = 0
    dropped_query_limit = 0
    for prefix, conflict in candidates:
        if (len(prefix) > len(modeled_events)
                or conflict.left_seq not in modeled_sequences
                or conflict.right_seq not in modeled_sequences):
            dropped_event_bound += 1
            continue
        if len(queries) >= query_cap:
            dropped_query_limit += 1
            continue
        query_index = len(queries)
        delta_smt2 = _schedule_smt_query_delta(
            modeled_events,
            prefix,
            conflict,
            query_index,
        )
        standalone_smt2 = base_smt2 + delta_smt2 + "(check-sat)\n"
        queries.append({
            "prefix": list(prefix),
            "conflict": conflict.to_mapping(),
            "program_order_count": program_order_count,
            "delta_smt2_sha256": hashlib.sha256(
                delta_smt2.encode("utf-8")
            ).hexdigest(),
            "standalone_smt2_sha256": hashlib.sha256(
                standalone_smt2.encode("utf-8")
            ).hexdigest(),
            "delta_smt2": delta_smt2,
        })

    incremental_parts = [base_smt2]
    for query in queries:
        incremental_parts.extend((
            "(push 1)\n",
            query["delta_smt2"],
            "(check-sat)\n",
            "(pop 1)\n",
        ))
    incremental_smt2 = "".join(incremental_parts)
    artifact: dict[str, Any] = {
        "schema": SCHEDULE_SMT_SCHEMA,
        "input_id": str(input_id or ""),
        "target_branch": _nonnegative_int(target_branch),
        "trace_digest": schedule_trace_digest(ordered_events),
        "current_prefix": list(current),
        "memory_model": normalized_memory_model,
        "encoding": (
            f"bounded-{normalized_memory_model.lower()}-"
            f"{lifecycle_order}-order-"
            "lock-condition-thread-state-v7-lazy-refinement"
        ),
        "lifecycle_order_encoding": lifecycle_order,
        "hard_constraints": [
            "bounded_event_positions",
            "event_position_permutation",
            "per_thread_program_order",
            f"{normalized_memory_model.lower()}_memory_consistency",
            "memory_read_from",
            "replay_thread_prefix",
            "source_conflict_reversal",
        ] + ([
            (
                "sync_lifecycle_partial_order_linear_extension"
                if lifecycle_order == "partial"
                else "sync_lifecycle_position_permutation"
            ),
            "sync_lifecycle_program_order",
            (
                "schedule_sync_scaled_anchor_link"
                if lifecycle_order == "partial"
                else "schedule_sync_pairwise_order_link"
            ),
            "complete_critical_section_nonoverlap",
            "violation_driven_lazy_section_refinement",
            "condition_wait_mutex_release_reacquire",
            "thread_create_before_start",
            "thread_exit_before_join_success",
            "thread_exit_and_retirement_trigger_before_identity_retire",
        ] if sync_state else []),
        "optional_constraints": [
            "observed_vector_clock_happens_before",
        ] + ([
            "trylock_failure_busy_witness",
            "condition_signal_or_broadcast_wake_witness",
        ] if sync_state else []),
        "not_encoded": ([
            "general_runtime_enabledness",
            "condition_spurious_wakeup_cause",
            "alias_equivalence",
            "path_constraints",
            "recursive_mutex_and_rwlock_upgrade_semantics",
            "pthread_detach_join_undefined_races",
            "pthread_cancel_delivery_state",
        ] + ([] if sync_state else [
            "mutex_and_rwlock_ownership_state",
        ])),
        "thread_ids": sorted({event.tid for event in modeled_events}),
        "event_count": len(schedulable),
        "modeled_event_count": len(modeled_events),
        "program_order_count": program_order_count,
        "observed_hb_assumption_count": len(observed_hb_assumptions),
        "observed_hb_assumptions": list(observed_hb_assumptions),
        "sync_state": sync_state_metadata,
        "memory_consistency": memory_model_metadata,
        "candidate_count": len(candidates),
        "query_count": len(queries),
        "dropped_query_count": (
            dropped_event_bound + dropped_query_limit
        ),
        "dropped_event_bound_count": dropped_event_bound,
        "dropped_query_limit_count": dropped_query_limit,
        "bounds": {
            "max_depth": depth,
            "max_window": window,
            "max_prefixes": prefix_cap,
            "max_events": event_cap,
            "max_memory_events": memory_event_cap,
            "max_sync_events": sync_state_metadata["max_events"],
            "max_queries": query_cap,
        },
        "events": [_event_mapping(event) for event in modeled_events],
        "base_smt2_sha256": hashlib.sha256(
            base_smt2.encode("utf-8")
        ).hexdigest(),
        "base_smt2": base_smt2,
        "relaxed_base_smt2_sha256": hashlib.sha256(
            relaxed_base_smt2.encode("utf-8")
        ).hexdigest(),
        "relaxed_base_smt2": relaxed_base_smt2,
        "relaxed_base_scope": (
            "internal_solver_only; exact base_smt2 remains authoritative"
        ),
        "incremental_smt2_sha256": hashlib.sha256(
            incremental_smt2.encode("utf-8")
        ).hexdigest(),
        "incremental_smt2": incremental_smt2,
        "queries": queries,
        "truncated": {
            "events": len(schedulable) > event_cap,
            "memory_events": memory_model_metadata["truncated"],
            "queries": (
                dropped_event_bound > 0 or dropped_query_limit > 0
            ),
        },
    }
    if (
        sync_state
        and sync_state_metadata["modeled_lifecycle_event_count"] > 0
    ):
        certificate = schedule_linear_extension_certificate(artifact)
        artifact["observed_linear_extension"] = certificate
        artifact["observed_topology_replay_prefix"] = list(
            certificate["replay_prefix"]
        )
    else:
        artifact["observed_linear_extension"] = None
        artifact["observed_topology_replay_prefix"] = []
    canonical = json.dumps(
        artifact,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    artifact["artifact_sha256"] = hashlib.sha256(canonical).hexdigest()
    return artifact


def _native_memory_location_groups(
    events: list[ScheduleEvent],
    allowed_indices: set[int],
) -> list[dict[str, Any]]:
    memory = [
        (index, event)
        for index, event in enumerate(events)
        if event.memory and index in allowed_indices
    ]
    parent = {index: index for index, _ in memory}

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        low, high = sorted((left_root, right_root))
        parent[high] = low

    for offset, (left_index, left) in enumerate(memory):
        for right_index, right in memory[offset + 1:]:
            if _memory_events_overlap(left, right):
                union(left_index, right_index)

    members: dict[int, list[tuple[int, ScheduleEvent]]] = {}
    for index, event in memory:
        members.setdefault(find(index), []).append((index, event))
    groups: list[dict[str, Any]] = []
    for rows in members.values():
        writes = sorted(index for index, event in rows if event.write)
        if not writes:
            continue
        groups.append({
            "location": min(index for index, _ in rows),
            "objects": sorted({event.obj for _, event in rows}),
            "write_indices": writes,
        })
    return sorted(groups, key=lambda row: int(row["location"]))


def _native_value_evidence(
    events: list[ScheduleEvent],
    read_from: list[dict[str, Any]],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    counts = {"confirmed": 0, "contradicted": 0, "unknown": 0}
    for relation in read_from:
        read_index = int(relation["read_index"])
        source_index = int(relation["source_index"])
        read_value = _tag_value(events[read_index].tags, "value")
        source_value = (
            _tag_value(events[read_index].tags, "init")
            if source_index < 0
            else _tag_value(events[source_index].tags, "value")
        )
        if not read_value or not source_value:
            status = "unknown"
        else:
            try:
                status = (
                    "confirmed"
                    if int(read_value, 0) == int(source_value, 0)
                    else "contradicted"
                )
            except ValueError:
                status = "unknown"
        counts[status] += 1
        rows.append({
            "read_index": read_index,
            "source_index": source_index,
            "read_value": read_value or None,
            "source_value": source_value or None,
            "status": status,
        })
    return {
        "rows": rows,
        **counts,
        "hardware_compatible": counts["contradicted"] == 0,
        "fully_value_witnessed": (
            bool(rows)
            and counts["contradicted"] == 0
            and counts["unknown"] == 0
        ),
    }


def _native_condpor_memory_graph_digest(
    certificate: Mapping[str, Any],
) -> str:
    return _canonical_json_digest({
        key: value
        for key, value in certificate.items()
        if key != "certificate_sha256"
    })


def native_condpor_memory_graph_certificate(
    events: list[ScheduleEvent],
    *,
    memory_model: str = "SC",
    max_events: int = 64,
    max_memory_events: int = 32,
    max_candidates: int = 4096,
    sync_state: bool = True,
) -> dict[str, Any]:
    """Enumerate bounded native ``rf/mo/sc`` graph equivalence classes.

    Each relation assignment is admitted by the same authoritative SMT model
    used for schedule replay.  The certificate intentionally excludes Z3's
    arbitrary event-position model so its identity depends on execution-graph
    relations rather than solver tie-breaking.
    """
    normalized_model = _normalize_memory_model(memory_model)
    event_cap = int(max_events)
    memory_cap = int(max_memory_events)
    candidate_cap = int(max_candidates)
    if event_cap < 2 or event_cap > 512:
        raise ValueError("max_events must be in [2, 512]")
    if memory_cap < 0 or memory_cap > 64:
        raise ValueError("max_memory_events must be in [0, 64]")
    if candidate_cap < 1 or candidate_cap > 65_536:
        raise ValueError("max_candidates must be in [1, 65536]")
    if len(events) > 131_072:
        raise ValueError("native trace exceeds 131072 events")
    ordered = sorted(events, key=lambda event: event.seq)
    sequences = [event.seq for event in ordered]
    if (
        any(sequence < 0 for sequence in sequences)
        or len(set(sequences)) != len(sequences)
    ):
        raise ValueError("native trace sequence ids must be unique and non-negative")

    artifact = schedule_smt_artifact(
        ordered,
        max_events=event_cap,
        max_memory_events=memory_cap,
        max_queries=1,
        sync_state=bool(sync_state),
        memory_model=normalized_model,
    )
    modeled = [
        ScheduleEvent(
            seq=int(row["seq"]),
            tid=int(row["tid"]),
            op=str(row["op"]),
            obj=str(row["object"]),
            tags=tuple(str(tag) for tag in row.get("tags", ())),
        )
        for row in artifact["events"]
    ]
    consistency = artifact["memory_consistency"]
    read_rows = [dict(row) for row in consistency["reads"]]
    modeled_memory_indices = {
        index
        for index, event in enumerate(modeled)
        if (event.memory or event.op in FENCE_OPS)
    }
    modeled_memory_indices = set(sorted(modeled_memory_indices)[:memory_cap])
    location_groups = _native_memory_location_groups(
        modeled, modeled_memory_indices
    )
    seq_cst_indices = (
        [
            index
            for index in sorted(modeled_memory_indices)
            if _event_memory_order(modeled[index]) == "seq_cst"
        ]
        if normalized_model == "RA"
        else []
    )

    axes: list[dict[str, Any]] = []
    for row in read_rows:
        axes.append({
            "kind": "rf",
            "read_index": int(row["read_index"]),
            "variable": str(row["variable"]),
            "domain": tuple(int(source) for source in row["sources"]),
        })
    for group_index, group in enumerate(location_groups):
        writes = list(group["write_indices"])
        for left_offset, left in enumerate(writes):
            for right in writes[left_offset + 1:]:
                axes.append({
                    "kind": "mo",
                    "group": group_index,
                    "left": left,
                    "right": right,
                    "domain": (0, 1),
                })
    for left_offset, left in enumerate(seq_cst_indices):
        for right in seq_cst_indices[left_offset + 1:]:
            axes.append({
                "kind": "sc",
                "left": left,
                "right": right,
                "domain": (0, 1),
            })

    candidate_space = 1
    for axis in axes:
        candidate_space *= len(axis["domain"])
    graphs: list[dict[str, Any]] = []
    status_counts = {"sat": 0, "unsat": 0, "unknown": 0}
    domains = [axis["domain"] for axis in axes]
    combinations = itertools.islice(
        itertools.product(*domains), candidate_cap
    )
    enumerated = 0
    for ordinal, choices in enumerate(combinations):
        enumerated += 1
        assertions: list[str] = []
        read_from: list[dict[str, Any]] = []
        modification_predecessors = {
            group_index: {
                int(write): set()
                for write in group["write_indices"]
            }
            for group_index, group in enumerate(location_groups)
        }
        sc_predecessors = {int(index): set() for index in seq_cst_indices}
        for axis, choice in zip(axes, choices):
            kind = str(axis["kind"])
            if kind == "rf":
                read_index = int(axis["read_index"])
                source_index = int(choice)
                assertions.append(
                    f"(assert (= {axis['variable']} "
                    f"{_smt_integer(source_index)}))"
                )
                read_from.append({
                    "read_index": read_index,
                    "read_seq": modeled[read_index].seq,
                    "source_index": source_index,
                    "source_seq": (
                        modeled[source_index].seq
                        if source_index >= 0 else None
                    ),
                })
                continue
            left = int(axis["left"])
            right = int(axis["right"])
            before, after = (left, right) if int(choice) == 0 else (right, left)
            if kind == "mo":
                relation = "pos" if normalized_model in {"SC", "TSO"} else "mo"
                assertions.append(
                    f"(assert (< {relation}_{before} {relation}_{after}))"
                )
                modification_predecessors[int(axis["group"])][after].add(before)
            else:
                assertions.append(f"(assert (< sc_{before} sc_{after}))")
                sc_predecessors[after].add(before)
        result = solve_schedule_smt_query(
            artifact,
            None,
            lazy_refinement=False,
            extra_smt2="\n".join(assertions) + ("\n" if assertions else ""),
        )
        status = str(result["status"])
        if status not in status_counts:
            status = "unknown"
        status_counts[status] += 1
        if status != "sat":
            continue
        modification_orders = []
        for group_index, group in enumerate(location_groups):
            order = sorted(
                (int(write) for write in group["write_indices"]),
                key=lambda write: (
                    len(modification_predecessors[group_index][write]),
                    write,
                ),
            )
            modification_orders.append({
                "location": int(group["location"]),
                "objects": list(group["objects"]),
                "event_indices": order,
                "event_sequences": [modeled[index].seq for index in order],
            })
        seq_cst_order = sorted(
            seq_cst_indices,
            key=lambda index: (len(sc_predecessors[index]), index),
        )
        identity = {
            "memory_model": normalized_model,
            "read_from": read_from,
            "modification_orders": modification_orders,
            "seq_cst_order": seq_cst_order,
        }
        graphs.append({
            "ordinal": ordinal,
            **identity,
            "seq_cst_sequences": [
                modeled[index].seq for index in seq_cst_order
            ],
            "value_evidence": _native_value_evidence(modeled, read_from),
            "graph_sha256": _canonical_json_digest(identity),
        })

    graph_truncated = candidate_space > enumerated
    source_truncated = bool(
        artifact["truncated"]["events"]
        or artifact["truncated"]["memory_events"]
        or artifact["sync_state"].get("truncated", False)
    )
    complete = (
        not graph_truncated
        and not source_truncated
        and status_counts["unknown"] == 0
    )
    certificate: dict[str, Any] = {
        "schema": NATIVE_CONDPOR_MEMORY_GRAPH_SCHEMA,
        "semantics": (
            "bounded-native-trace-rf-mo-sc-equivalence-v1"
        ),
        "memory_model": normalized_model,
        "trace_digest": schedule_trace_digest(ordered),
        "trace_events": [_event_mapping(event) for event in ordered],
        "schedule_artifact_sha256": artifact["artifact_sha256"],
        "schedule_base_smt2_sha256": artifact["base_smt2_sha256"],
        "memory_consistency": consistency,
        "bounds": {
            "max_events": event_cap,
            "max_memory_events": memory_cap,
            "max_candidates": candidate_cap,
            "sync_state": bool(sync_state),
        },
        "candidate_space": candidate_space,
        "enumerated_candidate_count": enumerated,
        "status_counts": status_counts,
        "graph_count": len(graphs),
        "graphs": graphs,
        "status": "complete" if complete else "truncated",
        "bounded_exhaustive": complete,
        "truncated": {
            "source_events": source_truncated,
            "candidate_space": graph_truncated,
            "solver_unknown": status_counts["unknown"] > 0,
        },
        "proved_scope": [
            "bounded_native_trace_relation_enumeration",
            "authoritative_schedule_smt_admission_per_relation_class",
            "deterministic_rf_mo_sc_graph_identity",
            "concrete_atomic_value_compatibility_when_available",
        ],
        "not_proved": [
            "unobserved_native_event_generation",
            "hardware_enforcement_of_tso_or_ra_reads_from",
            "unbounded_condpor_soundness_completeness_optimality",
            "full_iso_c11_undefined_behavior_and_consume_semantics",
        ],
        "sound_complete_optimal_claimed": False,
    }
    certificate["certificate_sha256"] = (
        _native_condpor_memory_graph_digest(certificate)
    )
    return certificate


def verify_native_condpor_memory_graph_certificate(
    certificate: Mapping[str, Any],
) -> bool:
    """Recompute a bounded native memory-model execution-graph certificate."""
    try:
        if certificate.get("schema") != NATIVE_CONDPOR_MEMORY_GRAPH_SCHEMA:
            return False
        if certificate.get("certificate_sha256") != (
            _native_condpor_memory_graph_digest(certificate)
        ):
            return False
        bounds = certificate["bounds"]
        raw_events = certificate["trace_events"]
        if not isinstance(bounds, Mapping) or not isinstance(raw_events, list):
            return False
        events = [
            ScheduleEvent(
                seq=int(row["seq"]),
                tid=int(row["tid"]),
                op=str(row["op"]),
                obj=str(row["object"]),
                tags=tuple(str(tag) for tag in row.get("tags", ())),
            )
            for row in raw_events
        ]
        expected = native_condpor_memory_graph_certificate(
            events,
            memory_model=str(certificate["memory_model"]),
            max_events=int(bounds["max_events"]),
            max_memory_events=int(bounds["max_memory_events"]),
            max_candidates=int(bounds["max_candidates"]),
            sync_state=bool(bounds["sync_state"]),
        )
        return dict(certificate) == expected
    except (
        KeyError,
        TypeError,
        ValueError,
        IndexError,
        OverflowError,
        RuntimeError,
    ):
        return False


def schedule_constraint_artifact(
    events: list[ScheduleEvent],
    *,
    input_id: str = "",
    target_branch: int = 0,
    current_prefix: Iterable[int] = (),
    max_depth: int = 64,
    max_window: int = 32,
    max_prefixes: int = 256,
    max_events: int = 512,
    max_conflicts: int = 256,
) -> dict[str, Any]:
    """Build a deterministic schedule-constraint artifact for experiments."""
    depth = max(1, int(max_depth))
    window = max(1, int(max_window))
    prefix_cap = max(1, int(max_prefixes))
    event_cap = max(0, int(max_events))
    conflict_cap = max(0, int(max_conflicts))
    current = normalize_schedule_prefix(current_prefix, max_len=depth)
    schedulable = [event for event in events if event.schedulable]
    conflicts = classify_schedule_conflicts(events, max_window=window)
    reduction = source_dpor_certificate(
        events,
        current_prefix=current,
        max_depth=depth,
        max_window=window,
        max_prefixes=prefix_cap,
        max_events=event_cap,
    )
    prefixes = tuple(
        tuple(prefix) for prefix in reduction["replay_prefixes"]
    )
    execution_graph = condpor_execution_graph_certificate(
        events,
        current_prefix=current,
        max_depth=depth,
        max_window=window,
        max_prefixes=prefix_cap,
        max_events=event_cap,
    )
    return {
        "schema": SCHEDULE_CONSTRAINT_SCHEMA,
        "input_id": str(input_id or ""),
        "target_branch": _nonnegative_int(target_branch),
        "trace_digest": schedule_trace_digest(events),
        "event_count": len(events),
        "schedulable_count": len(schedulable),
        "current_prefix": list(current),
        "bounds": {
            "max_depth": depth,
            "max_window": window,
            "max_prefixes": prefix_cap,
            "max_events": event_cap,
            "max_conflicts": conflict_cap,
        },
        "conflict_count": len(conflicts),
        "sync_conflict_count": sum(
            1 for conflict in conflicts if conflict.kind == "sync"),
        "memory_conflict_count": sum(
            1 for conflict in conflicts if conflict.kind == "memory"),
        "provenance_counts": _provenance_counts(schedulable),
        "conflicts": [
            conflict.to_mapping()
            for conflict in conflicts[:conflict_cap]
        ],
        "replay_prefixes": [list(prefix) for prefix in prefixes],
        "source_dpor": reduction,
        "wakeup_tree": wakeup_tree_certificate(
            events,
            current_prefix=current,
            max_depth=depth,
            max_window=window,
            max_prefixes=prefix_cap,
            max_events=event_cap,
        ),
        "operational_enabledness": operational_enabledness_certificate(
            events
        ),
        "condpor_execution_graph": execution_graph,
        "events": [_event_mapping(event) for event in schedulable[:event_cap]],
        "truncated": {
            "events": len(schedulable) > event_cap,
            "conflicts": len(conflicts) > conflict_cap,
        },
    }


def _append_jsonl_artifact(
    path: str,
    artifact: dict[str, Any],
) -> bool:
    if not str(path or "").strip():
        return False
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(
                artifact,
                sort_keys=True,
                separators=(",", ":"),
            ))
            stream.write("\n")
        return True
    except (OSError, TypeError, ValueError):
        return False


def append_schedule_constraint_artifact(
    path: str,
    artifact: dict[str, Any],
) -> bool:
    """Append one schedule-constraint artifact as JSONL."""
    return _append_jsonl_artifact(path, artifact)


def append_schedule_smt_artifact(
    path: str,
    artifact: dict[str, Any],
) -> bool:
    """Append one schedule-SMT artifact as JSONL."""
    return _append_jsonl_artifact(path, artifact)


class DporScheduleExplorer:
    """Persistent bounded-DPOR queue over synchronization traces.

    The explorer maintains bounded source sets, causal wakeup sequences,
    observed-state sleep sets, and Mazurkiewicz dependency signatures.  Its
    black-box runtime does not expose complete enabledness, so this class does
    not claim the unbounded completeness or optimality of model-checker DPOR.
    """

    def __init__(
        self,
        state_path: str,
        *,
        max_depth: int = 64,
        max_window: int = 32,
        max_prefixes_per_input: int = 256,
        max_pending: int = 4096,
        constraint_path: str = "",
        smt_path: str = "",
        smt_max_events: int = 128,
        smt_max_memory_events: int = 32,
        smt_max_queries: int = 64,
        smt_sync_state: bool = True,
        smt_order_encoding: str = "partial",
        smt_memory_model: str = "SC",
        wakeup_tree: bool = True,
        condpor_graph: bool = True,
    ) -> None:
        self.state_path = state_path
        self.max_depth = max(1, int(max_depth))
        self.max_window = max(1, int(max_window))
        self.max_prefixes_per_input = max(1, int(max_prefixes_per_input))
        self.max_pending = max(1, int(max_pending))
        self.constraint_path = str(constraint_path or "")
        self.smt_path = str(smt_path or "")
        self.smt_max_events = min(512, max(2, int(smt_max_events)))
        self.smt_max_memory_events = min(
            64, max(0, int(smt_max_memory_events))
        )
        self.smt_max_queries = min(512, max(1, int(smt_max_queries)))
        self.smt_sync_state = bool(smt_sync_state)
        self.smt_order_encoding = _normalize_schedule_order_encoding(
            smt_order_encoding
        )
        self.smt_memory_model = _normalize_memory_model(smt_memory_model)
        self.wakeup_tree_enabled = bool(wakeup_tree)
        self.condpor_graph_enabled = bool(condpor_graph)
        self._seen: dict[str, set[str]] = {}
        self._sleep_sets: dict[str, dict[str, set[int]]] = {}
        self._equivalence_seen: dict[str, set[str]] = {}
        self._wakeup_trees: dict[
            str, dict[str, list[tuple[int, ...]]]
        ] = {}
        self._condpor_revisit_seen: dict[str, set[str]] = {}
        self._pending: deque[DporReplayJob] = deque()
        self.observed_traces = 0
        self.generated_prefixes = 0
        self.sleep_pruned_prefixes = 0
        self.equivalent_traces = 0
        self.wakeup_pruned_prefixes = 0
        self.explored_wakeup_leaves = 0
        self.condpor_revisits = 0
        self.condpor_duplicate_revisits = 0
        self._load()

    @staticmethod
    def input_key(path: str, sha256: str = "") -> str:
        digest = str(sha256 or "").strip().lower()
        if len(digest) == 64 and all(ch in "0123456789abcdef" for ch in digest):
            return digest
        return "path:" + hashlib.sha256(
            os.path.abspath(path).encode("utf-8", errors="surrogateescape")
        ).hexdigest()

    @staticmethod
    def prefix_key(prefix: Iterable[int]) -> str:
        return _prefix_key(prefix)

    def _load(self) -> None:
        try:
            with open(self.state_path, encoding="utf-8") as stream:
                data = json.load(stream)
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(data, dict):
            return
        seen = data.get("seen", {})
        if isinstance(seen, dict):
            for key, values in seen.items():
                if isinstance(key, str) and isinstance(values, list):
                    self._seen[key] = {str(value) for value in values}
        sleep_sets = data.get("sleep_sets", {})
        if isinstance(sleep_sets, dict):
            for input_id, prefixes in sleep_sets.items():
                if not isinstance(input_id, str) or not isinstance(
                    prefixes, dict
                ):
                    continue
                loaded: dict[str, set[int]] = {}
                for prefix_key, tids in prefixes.items():
                    if not isinstance(prefix_key, str) or not isinstance(
                        tids, list
                    ):
                        continue
                    loaded_tids: set[int] = set()
                    for value in tids:
                        try:
                            tid = int(value)
                        except (TypeError, ValueError):
                            continue
                        if tid >= 0:
                            loaded_tids.add(tid)
                    loaded[prefix_key] = loaded_tids
                self._sleep_sets[input_id] = loaded
        equivalence_seen = data.get("equivalence_seen", {})
        if isinstance(equivalence_seen, dict):
            for input_id, digests in equivalence_seen.items():
                if isinstance(input_id, str) and isinstance(digests, list):
                    self._equivalence_seen[input_id] = {
                        str(digest) for digest in digests
                        if len(str(digest)) == 64
                    }
        wakeup_trees = data.get("wakeup_trees", {})
        if isinstance(wakeup_trees, Mapping):
            for input_id, raw_trees in wakeup_trees.items():
                if not isinstance(input_id, str) or not isinstance(
                    raw_trees, Mapping
                ):
                    continue
                trees: dict[str, list[tuple[int, ...]]] = {}
                for base_key, raw_leaves in raw_trees.items():
                    if not isinstance(base_key, str) or not isinstance(
                        raw_leaves, list
                    ):
                        continue
                    leaves: list[tuple[int, ...]] = []
                    for raw_leaf in raw_leaves:
                        leaf = normalize_schedule_prefix(
                            raw_leaf, max_len=self.max_depth
                        )
                        if leaf and leaf not in leaves:
                            leaves.append(leaf)
                    trees[base_key] = leaves
                self._wakeup_trees[input_id] = trees
        revisit_seen = data.get("condpor_revisit_seen", {})
        if isinstance(revisit_seen, Mapping):
            for input_id, digests in revisit_seen.items():
                if isinstance(input_id, str) and isinstance(digests, list):
                    self._condpor_revisit_seen[input_id] = {
                        str(digest) for digest in digests
                        if len(str(digest)) == 64
                    }
        pending = data.get("pending", [])
        if isinstance(pending, list):
            for item in pending:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path", "") or "")
                input_id = str(item.get("input_id", "") or "")
                prefix = normalize_schedule_prefix(
                    item.get("prefix", ()), max_len=self.max_depth)
                if path and input_id and prefix:
                    self._pending.append(DporReplayJob(path, input_id, prefix))
                    if len(self._pending) >= self.max_pending:
                        break
        try:
            self.observed_traces = max(0, int(data.get("observed_traces", 0)))
            self.generated_prefixes = max(0, int(data.get("generated_prefixes", 0)))
            self.sleep_pruned_prefixes = max(
                0, int(data.get("sleep_pruned_prefixes", 0)))
            self.equivalent_traces = max(
                0, int(data.get("equivalent_traces", 0)))
            self.wakeup_pruned_prefixes = max(
                0, int(data.get("wakeup_pruned_prefixes", 0)))
            self.explored_wakeup_leaves = max(
                0, int(data.get("explored_wakeup_leaves", 0)))
            self.condpor_revisits = max(
                0, int(data.get("condpor_revisits", 0)))
            self.condpor_duplicate_revisits = max(
                0, int(data.get("condpor_duplicate_revisits", 0)))
        except (TypeError, ValueError):
            self.observed_traces = 0
            self.generated_prefixes = 0
            self.sleep_pruned_prefixes = 0
            self.equivalent_traces = 0
            self.wakeup_pruned_prefixes = 0
            self.explored_wakeup_leaves = 0
            self.condpor_revisits = 0
            self.condpor_duplicate_revisits = 0

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        payload = {
            "schema": 4,
            "observed_traces": self.observed_traces,
            "generated_prefixes": self.generated_prefixes,
            "sleep_pruned_prefixes": self.sleep_pruned_prefixes,
            "equivalent_traces": self.equivalent_traces,
            "wakeup_pruned_prefixes": self.wakeup_pruned_prefixes,
            "explored_wakeup_leaves": self.explored_wakeup_leaves,
            "condpor_revisits": self.condpor_revisits,
            "condpor_duplicate_revisits": (
                self.condpor_duplicate_revisits
            ),
            "seen": {
                key: sorted(values)
                for key, values in sorted(self._seen.items())
            },
            "sleep_sets": {
                input_id: {
                    prefix: sorted(tids)
                    for prefix, tids in sorted(prefixes.items())
                }
                for input_id, prefixes in sorted(self._sleep_sets.items())
            },
            "equivalence_seen": {
                input_id: sorted(digests)
                for input_id, digests in sorted(
                    self._equivalence_seen.items())
            },
            "wakeup_trees": {
                input_id: {
                    base: [list(leaf) for leaf in leaves]
                    for base, leaves in sorted(trees.items())
                }
                for input_id, trees in sorted(self._wakeup_trees.items())
            },
            "condpor_revisit_seen": {
                input_id: sorted(digests)
                for input_id, digests in sorted(
                    self._condpor_revisit_seen.items()
                )
            },
            "pending": [
                {
                    "path": job.path,
                    "input_id": job.input_id,
                    "prefix": list(job.prefix),
                }
                for job in list(self._pending)[:self.max_pending]
            ],
        }
        directory = os.path.dirname(self.state_path) or "."
        fd, tmp = tempfile.mkstemp(
            prefix=os.path.basename(self.state_path) + ".",
            suffix=".tmp",
            dir=directory,
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            os.replace(tmp, self.state_path)
        except (OSError, TypeError, ValueError):
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _remember(self, input_id: str, prefix: tuple[int, ...]) -> bool:
        if not prefix:
            return False
        key = self.prefix_key(prefix)
        seen = self._seen.setdefault(input_id, set())
        if key in seen or len(seen) >= self.max_prefixes_per_input:
            return False
        seen.add(key)
        return True

    @staticmethod
    def dependent(left: ScheduleEvent, right: ScheduleEvent) -> bool:
        return _schedule_events_dependent(left, right)

    def propose_prefixes(
        self,
        events: list[ScheduleEvent],
        current_prefix: Iterable[int] = (),
    ) -> list[tuple[int, ...]]:
        return list(propose_source_replay_prefixes(
            events,
            current_prefix,
            max_depth=self.max_depth,
            max_window=self.max_window,
            max_prefixes=self.max_prefixes_per_input,
        ))

    def observe(
        self,
        path: str,
        trace: str | Iterable[str],
        *,
        sha256: str = "",
        current_prefix: Iterable[int] = (),
        target_branch: int = 0,
    ) -> int:
        events = parse_schedule_trace(trace)
        if not events:
            return 0
        input_id = self.input_key(path, sha256)
        normalized_current = normalize_schedule_prefix(
            current_prefix, max_len=self.max_depth
        )
        self._seen.setdefault(input_id, set()).add(
            self.prefix_key(normalized_current))
        input_wakeup_trees = self._wakeup_trees.setdefault(input_id, {})
        if normalized_current:
            for base_key, leaves in input_wakeup_trees.items():
                base = normalize_schedule_prefix(
                    base_key, max_len=self.max_depth
                )
                retained = [
                    leaf for leaf in leaves
                    if base + leaf != normalized_current
                ]
                self.explored_wakeup_leaves += len(leaves) - len(retained)
                input_wakeup_trees[base_key] = retained
        self.observed_traces += 1
        reduction = source_dpor_certificate(
            events,
            current_prefix=current_prefix,
            max_depth=self.max_depth,
            max_window=self.max_window,
            max_prefixes=self.max_prefixes_per_input,
            max_events=self.smt_max_events,
        )
        execution_graph = (
            condpor_execution_graph_certificate(
                events,
                current_prefix=current_prefix,
                max_depth=self.max_depth,
                max_window=self.max_window,
                max_prefixes=self.max_prefixes_per_input,
                max_events=self.smt_max_events,
            )
            if self.condpor_graph_enabled
            else None
        )
        if execution_graph is not None:
            revisit_seen = self._condpor_revisit_seen.setdefault(
                input_id, set()
            )
            for revisit in execution_graph["revisits"]:
                revisit_id = str(revisit["revisit_sha256"])
                if revisit_id in revisit_seen:
                    self.condpor_duplicate_revisits += 1
                else:
                    revisit_seen.add(revisit_id)
                    self.condpor_revisits += 1
        equivalence = reduction["dependency_graph"]["equivalence_sha256"]
        equivalence_seen = self._equivalence_seen.setdefault(input_id, set())
        if equivalence in equivalence_seen:
            self.equivalent_traces += 1
        else:
            equivalence_seen.add(equivalence)

        observed_tids = [
            event.tid
            for event in sorted(events, key=lambda item: item.seq)
            if event.schedulable
        ][:self.max_depth]
        input_sleep_sets = self._sleep_sets.setdefault(input_id, {})
        for index, tid in enumerate(observed_tids):
            base_key = self.prefix_key(observed_tids[:index])
            input_sleep_sets.setdefault(base_key, set()).add(tid)
        if self.constraint_path:
            constraint = schedule_constraint_artifact(
                events,
                input_id=input_id,
                target_branch=target_branch,
                current_prefix=current_prefix,
                max_depth=self.max_depth,
                max_window=self.max_window,
                max_prefixes=self.max_prefixes_per_input,
                max_events=self.smt_max_events,
            )
            constraint["source_dpor"] = reduction
            constraint["wakeup_tree"] = wakeup_tree_certificate(
                events,
                current_prefix=current_prefix,
                sleep_sets=input_sleep_sets,
                max_depth=self.max_depth,
                max_window=self.max_window,
                max_prefixes=self.max_prefixes_per_input,
                max_events=self.smt_max_events,
            )
            if execution_graph is not None:
                constraint["condpor_execution_graph"] = execution_graph
            append_schedule_constraint_artifact(
                self.constraint_path,
                constraint,
            )
        if self.smt_path:
            append_schedule_smt_artifact(
                self.smt_path,
                schedule_smt_artifact(
                    events,
                    input_id=input_id,
                    target_branch=target_branch,
                    current_prefix=current_prefix,
                    max_depth=self.max_depth,
                    max_window=self.max_window,
                    max_prefixes=self.max_prefixes_per_input,
                    max_events=self.smt_max_events,
                    max_memory_events=self.smt_max_memory_events,
                    max_queries=self.smt_max_queries,
                    sync_state=self.smt_sync_state,
                    order_encoding=self.smt_order_encoding,
                    memory_model=self.smt_memory_model,
                ),
            )
        added = 0
        ready_sets = runtime_ready_evidence(events)
        for candidate in reduction["candidate_rows"]:
            if len(self._pending) >= self.max_pending:
                break
            prefix = tuple(candidate["prefix"])
            base_key = self.prefix_key(candidate["base_prefix"])
            wakeup = candidate["wakeup_sequence"]
            if self.wakeup_tree_enabled:
                ready = ready_sets.get(len(candidate["base_prefix"]), {})
                tree = BoundedWakeupTree(
                    events,
                    candidate["base_prefix"],
                    sleep=input_sleep_sets.get(base_key, set()),
                    ready=ready.get("threads", ()),
                    ready_complete=bool(ready.get("complete", False)),
                    leaves=input_wakeup_trees.get(base_key, ()),
                    max_depth=self.max_depth,
                    max_leaves=self.max_prefixes_per_input,
                )
                insertion = tree.insert(wakeup)
                input_wakeup_trees[base_key] = list(tree.leaves)
                if not insertion["inserted"]:
                    self.wakeup_pruned_prefixes += 1
                    if insertion["reason"] == "sleep_weak_initial":
                        self.sleep_pruned_prefixes += 1
                    continue
            elif (
                wakeup
                and int(wakeup[0]) in input_sleep_sets.get(base_key, set())
            ):
                self.sleep_pruned_prefixes += 1
                continue
            if self._remember(input_id, prefix):
                self._pending.append(DporReplayJob(path, input_id, prefix))
                self.generated_prefixes += 1
                added += 1
        self.save()
        return added

    def pop_pending(self, limit: int) -> list[DporReplayJob]:
        jobs: list[DporReplayJob] = []
        cap = max(0, int(limit))
        while self._pending and len(jobs) < cap:
            jobs.append(self._pending.popleft())
        if jobs:
            self.save()
        return jobs

    def pending_count(self) -> int:
        return len(self._pending)
