#!/usr/bin/env python3
"""
MPI-parallel concolic execution driver for SymCC.

Uses a Master-Worker pattern with automatic multi-master scaling.
Workers write results to private staging roots and communicate hashes plus
staging identities over MPI. Masters verify fenced leases before promoting
content-addressed objects into the shared corpus.

Architecture auto-selection based on process count:
  np <= 91:  1 master  + (np-1) workers          (single-master)
  np > 91:   M masters + (np-M) workers           (multi-master)
             where M = ceil((np-1) / workers_per_master)

  workers_per_master defaults to 90: a single (轻量、仅做 hash 去重的) master 经实测可
  近线性喂饱 ~96+ 个 worker（去除每结果 print + 自适应 sleep 后单核 ~87% @ 96w，最坏
  情况的极快目标；真实 SymCC 每任务 0.5-30s，master 负载远低于此，可喂更多）。

In multi-master mode, each master manages its own worker group via a
MPI sub-communicator (comm.Split). Content hashes are assigned by rendezvous
hashing and protected by shared fencing leases, so every group explores a
disjoint part of one shared frontier.

Usage:
    mpirun -np <N> python3 mpi_concolic_execution.py \
        -i INPUT_DIR [-o OUTPUT_DIR] [-t TIMEOUT] -- TARGET [ARGS...]

Requirements:
    - mpi4py  (pip install mpi4py)
    - An MPI implementation (OpenMPI, MPICH, etc.)
    - SymCC-instrumented target binary
"""

import argparse
import errno
import heapq
import hashlib
import json
import math
import os
import random
import re
import signal
import shutil
import subprocess
import stat
import sys
import tempfile
import time
import typing
from array import array
from collections import deque
from dataclasses import dataclass

from concolic_engine import get_engine   # concolic 引擎抽象(symcc / symsan 可切换)
from distributed_state import (
    FULL_SHARED_FILESYSTEM_REQUIREMENTS,
    FencedWorkLeaseTable,
    LeaseHeartbeatBatch,
    SharedFilesystemCapabilities,
    bounded_advisory_lock,
    durable_link,
    durable_makedirs,
    durable_rename_noreplace,
    durable_replace,
    durable_rmtree,
    durable_rmtree_step,
    fsync_directory,
    probe_shared_state_filesystem,
)
from mpi4py import MPI

try:
    from .ulfm_snapshot_store import AtomicUlfmSnapshotStore
except ImportError:
    from ulfm_snapshot_store import AtomicUlfmSnapshotStore

try:
    from .mpi_ulfm_recovery import (
        DurableUlfmCoordinator,
        EndpointIdentity,
        RecoveryShard,
        UlfmRecoveryController,
        UlfmRecoveryError,
        UlfmRecoveryPolicy,
        UlfmCollectiveTimeout,
        UlfmRuntimeError,
        build_work_envelope,
        build_work_fence,
        content_digest as ulfm_content_digest,
        complete_collective_before_deadline,
        deadline_agree,
        deadline_shrink,
        is_ulfm_failure,
        probe_ulfm_runtime,
        shrink_and_attest,
        verify_work_envelope,
    )
except ImportError:
    from mpi_ulfm_recovery import (
        DurableUlfmCoordinator,
        EndpointIdentity,
        RecoveryShard,
        UlfmRecoveryController,
        UlfmRecoveryError,
        UlfmRecoveryPolicy,
        UlfmCollectiveTimeout,
        UlfmRuntimeError,
        build_work_envelope,
        build_work_fence,
        content_digest as ulfm_content_digest,
        complete_collective_before_deadline,
        deadline_agree,
        deadline_shrink,
        is_ulfm_failure,
        probe_ulfm_runtime,
        shrink_and_attest,
        verify_work_envelope,
    )

try:
    from .mpi_lifecycle import (
        TAG_READY,
        TAG_RESULT,
        TAG_STOP,
        _bounded_mpi_barrier,
        _bounded_mpi_timeout,
        _cooperative_shutdown_workers,
        _send_shutdown_ack,
        _shutdown_stop_token,
    )
except ImportError:
    from mpi_lifecycle import (
        TAG_READY,
        TAG_RESULT,
        TAG_STOP,
        _bounded_mpi_barrier,
        _bounded_mpi_timeout,
        _cooperative_shutdown_workers,
        _send_shutdown_ack,
        _shutdown_stop_token,
    )

try:
    from .mpi_filesystem_qualification import (
        ClusterLockRenewalController,
        begin_cluster_lock_renewal,
        complete_cluster_lock_renewal,
        observe_cluster_lock_qualification_inputs,
        observe_cluster_lock_renewal_delivery,
        observe_work_lease_heartbeat,
        poll_cluster_lock_renewal,
        qualify_cluster_lock_renewal_configuration,
        qualify_mpi_cluster_advisory_lock,
    )
except ImportError:
    from mpi_filesystem_qualification import (
        ClusterLockRenewalController,
        begin_cluster_lock_renewal,
        complete_cluster_lock_renewal,
        observe_cluster_lock_qualification_inputs,
        observe_cluster_lock_renewal_delivery,
        observe_work_lease_heartbeat,
        poll_cluster_lock_renewal,
        qualify_cluster_lock_renewal_configuration,
        qualify_mpi_cluster_advisory_lock,
    )

# --- 辅助函数 ---


_DEFAULT_RESULT_MAX_OBJECTS = 4096
_MAX_RESULT_MAX_OBJECTS = 1_000_000
_DEFAULT_RESULT_MAX_BYTES = 256 * 1024 * 1024
_MAX_RESULT_MAX_BYTES = 1024 * 1024 * 1024 * 1024
_DEFAULT_INPUT_MAX_BYTES = 256 * 1024 * 1024
_MAX_INPUT_MAX_BYTES = 1024 * 1024 * 1024 * 1024
_RESULT_STREAM_CHUNK_BYTES = 1024 * 1024
_SIMULATION_OUTPUT_BATCH_SIZE = 32
_RESULT_BUDGET_PROTOCOL_ERROR = "result-budget-exceeded"
_INPUT_BUDGET_PROTOCOL_ERROR = "input-budget-exceeded"


class _ResultBudgetExceeded(ValueError):
    """Report one exact standalone-result admission limit violation."""

    def __init__(self, resource: str, observed: int, limit: int) -> None:
        if resource not in {"objects", "bytes"}:
            raise ValueError("unsupported result-budget resource")
        if (isinstance(observed, bool) or not isinstance(observed, int)
                or isinstance(limit, bool) or not isinstance(limit, int)
                or observed < 0 or limit < 0 or observed <= limit):
            raise ValueError("invalid result-budget violation")
        self.resource = resource
        self.observed = observed
        self.limit = limit
        self.staging_id = ""
        super().__init__(
            f"standalone result {resource} budget exceeded: "
            f"observed={observed}, limit={limit}"
        )

    def payload(self) -> dict[str, typing.Any]:
        return {
            "resource": self.resource,
            "observed": self.observed,
            "limit": self.limit,
        }


class _InputBudgetExceeded(ValueError):
    """Report one exact standalone input-byte admission violation."""

    def __init__(self, observed: int, limit: int) -> None:
        if (isinstance(observed, bool) or not isinstance(observed, int)
                or isinstance(limit, bool) or not isinstance(limit, int)
                or observed < 0 or limit < 0 or observed <= limit):
            raise ValueError("invalid input-budget violation")
        self.observed = observed
        self.limit = limit
        super().__init__(
            "standalone input bytes budget exceeded: "
            f"observed={observed}, limit={limit}"
        )

    def payload(self) -> dict[str, int]:
        return {"observed": self.observed, "limit": self.limit}


class _SourceSnapshotChanged(OSError):
    """A regular source or its directory entry changed during admission."""


@dataclass(frozen=True)
class _RegularFileIdentity:
    """Stable identity fields used to detect in-place or path replacement."""

    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class _CorpusProvenanceCounts:
    """Final public-corpus cardinalities over one shared namespace."""

    public: int
    external: int

    @property
    def generated(self) -> int:
        return self.public - self.external


def _regular_file_identity(metadata: os.stat_result) -> _RegularFileIdentity:
    return _RegularFileIdentity(
        device=int(metadata.st_dev),
        inode=int(metadata.st_ino),
        size=int(metadata.st_size),
        mtime_ns=int(metadata.st_mtime_ns),
        ctime_ns=int(metadata.st_ctime_ns),
    )


def _atomic_write(dest: str, content: bytes) -> None:
    """原子写入文件：先写临时文件再 rename，避免并发写入导致数据损坏。"""
    tmp = (
        f"{dest}.tmp.{os.getpid()}.{time.monotonic_ns()}."
        f"{os.urandom(8).hex()}"
    )
    try:
        with open(tmp, "xb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        durable_replace(tmp, dest)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _write_all_descriptor(
    descriptor: int,
    content: bytes | bytearray | memoryview,
) -> None:
    """Write one buffer completely without materializing another copy."""
    view = memoryview(content)
    try:
        offset = 0
        while offset < len(view):
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                raise OSError(errno.EIO, "short regular-file stream write")
            offset += written
    finally:
        view.release()


def _regular_file_sha256_snapshot(
    path: str,
    *,
    byte_limit: int | None = None,
) -> tuple[str, int, _RegularFileIdentity] | None:
    """Digest one stable path-bound regular inode without following links."""
    if byte_limit is not None and (
            isinstance(byte_limit, bool) or not isinstance(byte_limit, int)
            or byte_limit < 0):
        raise ValueError("regular-file byte limit must be non-negative")
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        return None
    flags = (
        os.O_RDONLY
        | no_follow
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = -1
    digest = hashlib.sha256()
    total = 0
    try:
        descriptor = os.open(path, flags)
        metadata_before = os.fstat(descriptor)
        if not stat.S_ISREG(metadata_before.st_mode):
            return None
        identity = _regular_file_identity(metadata_before)
        if byte_limit is not None and identity.size > byte_limit:
            raise _ResultBudgetExceeded(
                "bytes", identity.size, byte_limit)
        while chunk := os.read(descriptor, _RESULT_STREAM_CHUNK_BYTES):
            total += len(chunk)
            if byte_limit is not None and total > byte_limit:
                raise _ResultBudgetExceeded("bytes", total, byte_limit)
            digest.update(chunk)
        metadata_after = os.fstat(descriptor)
        path_metadata = os.stat(path, follow_symlinks=False)
        if (not stat.S_ISREG(path_metadata.st_mode)
                or _regular_file_identity(metadata_after) != identity
                or _regular_file_identity(path_metadata) != identity
                or total != identity.size):
            return None
    except _ResultBudgetExceeded:
        raise
    except (OSError, TypeError, ValueError):
        return None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
    return digest.hexdigest(), total, identity


def _regular_file_sha256_size(
    path: str,
    *,
    byte_limit: int | None = None,
) -> tuple[str, int] | None:
    """Digest and size one stable regular path without following symlinks."""
    result = _regular_file_sha256_snapshot(path, byte_limit=byte_limit)
    return result[:2] if result is not None else None


def _file_sha256(path: str) -> str:
    """Digest one opened regular inode without following a final symlink."""
    result = _regular_file_sha256_size(path)
    return result[0] if result is not None else ""


def _shared_corpus_file_mode() -> int:
    """Read the explicit non-executable mode for public corpus objects."""
    raw = os.environ.get("SYMCC_SHARED_CORPUS_FILE_MODE", "0600")
    if not re.fullmatch(r"[0-7]{3,4}", raw):
        raise ValueError(
            "SYMCC_SHARED_CORPUS_FILE_MODE must be a three- or four-digit "
            "octal mode"
        )
    mode = int(raw, 8)
    if mode & ~0o777 or mode & 0o111 or mode & 0o400 != 0o400:
        raise ValueError(
            "SYMCC_SHARED_CORPUS_FILE_MODE must be owner-readable and "
            "non-executable"
        )
    return mode


def _set_durable_regular_file_mode(path: str, mode: int) -> None:
    """Persist one already-written regular inode's publication mode."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise OSError(errno.EOPNOTSUPP, "O_NOFOLLOW is unavailable", path)
    descriptor = os.open(path, flags | no_follow)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("corpus publication object is not regular")
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


_STAGING_ID_LENGTH = 32


def _normalize_staging_id(value: typing.Any) -> str:
    if not isinstance(value, str) or len(value) != _STAGING_ID_LENGTH:
        return ""
    if any(char not in "0123456789abcdef" for char in value):
        return ""
    return value


def _staging_directory(
    work_state_dir: str,
    worker_global_rank: int,
    staging_id: str,
) -> str:
    normalized = _normalize_staging_id(staging_id)
    if int(worker_global_rank) < 0 or not normalized:
        raise ValueError("invalid staged-output identity")
    return os.path.join(
        work_state_dir, "staging", str(int(worker_global_rank)), normalized)


def _verify_staged_outputs(
    work_state_dir: str,
    worker_global_rank: int,
    staging_id: str,
    hashes: typing.Iterable[str],
    *,
    declared_bytes: int | None = None,
    max_objects: int | None = None,
    max_bytes: int | None = None,
) -> bool:
    normalized_hashes = tuple(_normalize_work_hash(value) for value in hashes)
    if max_objects is not None:
        if (isinstance(max_objects, bool) or not isinstance(max_objects, int)
                or max_objects < 0):
            raise ValueError("staged-output object limit must be non-negative")
        if len(normalized_hashes) > max_objects:
            raise _ResultBudgetExceeded(
                "objects", len(normalized_hashes), max_objects)
    if max_bytes is not None and (
            isinstance(max_bytes, bool) or not isinstance(max_bytes, int)
            or max_bytes < 0):
        raise ValueError("staged-output byte limit must be non-negative")
    if declared_bytes is not None and (
            isinstance(declared_bytes, bool)
            or not isinstance(declared_bytes, int)
            or declared_bytes < 0):
        return False
    if not normalized_hashes or any(not value for value in normalized_hashes):
        return (
            not normalized_hashes
            and not staging_id
            and (declared_bytes is None or declared_bytes == 0)
        )
    try:
        directory = _staging_directory(
            work_state_dir, worker_global_rank, staging_id)
    except (TypeError, ValueError):
        return False
    expected = set(normalized_hashes)
    inventory = _verified_staged_output_inventory(
        directory, expected, max_bytes=max_bytes)
    if inventory is None:
        return False
    observed, sizes = inventory
    if observed != expected:
        return False
    logical_bytes = sum(sizes[value] for value in normalized_hashes)
    if max_bytes is not None and logical_bytes > max_bytes:
        raise _ResultBudgetExceeded("bytes", logical_bytes, max_bytes)
    return declared_bytes is None or logical_bytes == declared_bytes


def _verified_staged_output_hashes(
    directory: str,
    expected: set[str],
) -> set[str] | None:
    """Stream one staging root and reject uncommitted names before I/O."""
    inventory = _verified_staged_output_inventory(directory, expected)
    return inventory[0] if inventory is not None else None


def _verified_staged_output_inventory(
    directory: str,
    expected: set[str],
    *,
    max_bytes: int | None = None,
) -> tuple[set[str], dict[str, int]] | None:
    """Verify expected objects once and retain only their bounded sizes."""
    if max_bytes is not None and (
            isinstance(max_bytes, bool) or not isinstance(max_bytes, int)
            or max_bytes < 0):
        raise ValueError("staged-output byte limit must be non-negative")
    observed: set[str] = set()
    sizes: dict[str, int] = {}
    unique_bytes = 0
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                work_hash = _normalize_work_hash(entry.name)
                if (not work_hash or work_hash not in expected
                        or work_hash in observed):
                    return None
                try:
                    regular = entry.is_file(follow_symlinks=False)
                except OSError:
                    return None
                if not regular:
                    return None
                remaining = (
                    max_bytes - unique_bytes
                    if max_bytes is not None else None
                )
                try:
                    result = _regular_file_sha256_size(
                        entry.path, byte_limit=remaining)
                except _ResultBudgetExceeded as error:
                    if max_bytes is None:
                        raise
                    raise _ResultBudgetExceeded(
                        "bytes", unique_bytes + error.observed, max_bytes
                    ) from error
                if result is None or result[0] != work_hash:
                    return None
                size = result[1]
                observed.add(work_hash)
                sizes[work_hash] = size
                unique_bytes += size
    except FileNotFoundError:
        return set(), {}
    except _ResultBudgetExceeded:
        raise
    except (OSError, TypeError, ValueError):
        return None
    return observed, sizes


def _verify_replayable_outputs(
    work_state_dir: str,
    shared_dir: str,
    worker_global_rank: int,
    staging_id: str,
    hashes: typing.Iterable[str],
) -> bool:
    """Verify every committed child in either staging or public storage."""
    normalized_hashes = tuple(_normalize_work_hash(value) for value in hashes)
    if not normalized_hashes or any(not value for value in normalized_hashes):
        return not normalized_hashes and not staging_id
    expected = set(normalized_hashes)
    try:
        directory = _staging_directory(
            work_state_dir, worker_global_rank, staging_id)
    except (TypeError, ValueError):
        return False
    staged = _verified_staged_output_hashes(directory, expected)
    if staged is None:
        return False
    for work_hash in expected - staged:
        if _file_sha256(os.path.join(shared_dir, work_hash)) != work_hash:
            return False
    return True


def _promote_staged_outputs(
    work_state_dir: str,
    shared_dir: str,
    worker_global_rank: int,
    staging_id: str,
    hashes: typing.Iterable[str],
) -> None:
    """Publish verified child objects only after the parent commit fence."""
    unique_hashes = tuple(sorted(set(hashes)))
    if not unique_hashes:
        return
    publication_mode = _shared_corpus_file_mode()
    directory = _staging_directory(
        work_state_dir, worker_global_rank, staging_id)
    for work_hash in unique_hashes:
        normalized = _normalize_work_hash(work_hash)
        if not normalized:
            raise ValueError("invalid staged output hash")
        source = os.path.join(directory, normalized)
        destination = os.path.join(shared_dir, normalized)
        if os.path.exists(destination):
            if _file_sha256(destination) != normalized:
                raise ValueError(
                    f"existing corpus digest mismatch for {normalized}")
            _set_durable_regular_file_mode(destination, publication_mode)
            continue
        _set_durable_regular_file_mode(source, publication_mode)
        try:
            durable_replace(source, destination)
        except FileNotFoundError:
            # Another recovery helper may have promoted the same immutable
            # object after our existence check.  Accept only the exact digest.
            if _file_sha256(destination) == normalized:
                continue
            raise
        if _file_sha256(destination) != normalized:
            raise ValueError(
                f"promoted corpus digest mismatch for {normalized}")


def _remove_staged_outputs(
    work_state_dir: str,
    worker_global_rank: int,
    staging_id: str,
) -> None:
    if not _normalize_staging_id(staging_id):
        return
    try:
        directory = _staging_directory(
            work_state_dir, worker_global_rank, staging_id)
    except (TypeError, ValueError):
        return
    shutil.rmtree(directory, ignore_errors=True)


def _receive_worker_control(
    group_comm: typing.Any,
    recovery_comm: typing.Any | None,
    *,
    master: int = 0,
    status: typing.Any | None = None,
    monotonic: typing.Callable[[], float] = time.monotonic,
    sleep: typing.Callable[[float], None] = time.sleep,
) -> tuple[typing.Any, typing.Any]:
    """Receive one group command while polling a distinct recovery domain."""
    observed_status = MPI.Status() if status is None else status
    if recovery_comm is None or recovery_comm is group_comm:
        message = group_comm.recv(
            source=master, tag=MPI.ANY_TAG, status=observed_status
        )
        return message, observed_status

    failure_poll_interval = _environment_float(
        "SYMCC_ULFM_FAILURE_POLL_INTERVAL", 0.1, 0.01, 30.0
    )
    last_failure_poll = 0.0
    while True:
        if group_comm.iprobe(source=master, tag=MPI.ANY_TAG):
            message = group_comm.recv(
                source=master, tag=MPI.ANY_TAG, status=observed_status
            )
            return message, observed_status
        now = monotonic()
        if now - last_failure_poll >= failure_poll_interval:
            last_failure_poll = now
            failed_member_count = _ulfm_failed_member_count(recovery_comm)
            if failed_member_count:
                try:
                    recovery_comm.Revoke()
                except Exception as error:
                    if not is_ulfm_failure(error, MPI):
                        raise
                raise _UlfmProcessFailureDetected(
                    "ULFM worker detector observed "
                    f"{failed_member_count} failed member(s)"
                )
        sleep(min(0.01, failure_poll_interval))


# MPI message tags — group communicator (master <-> workers)
TAG_WORK = 1       # Master -> Worker: hash string of input to process

# MPI message tags — global communicator (master <-> master)
TAG_MASTER_STATUS = 10  # All masters -> peers: current local work state
TAG_MASTER_PROBE = 11  # Root -> sub-masters: exact quiescence probe
TAG_MASTER_STATS = 12  # Sub-masters -> Root: final statistics
TAG_MASTER_STATS_ACK = 13  # Root -> Sub-masters: exact stats receipt
TAG_MASTER_PROBE_REPLY = 14  # Sub-masters -> Root: probe result
TAG_MASTER_QUIESCE = 15  # Root -> sub-masters: commit/abort exact probe
TAG_MASTER_QUIESCE_ACK = 16  # Sub-masters -> Root: exact commit receipt
TAG_ULFM_STANDBY_STOP = 17  # Repaired root -> prelaunched warm standbys
TAG_ULFM_FAILURE_SENTINEL = 18  # Master posts one receive per owned worker


def compute_roles(comm_size: int, workers_per_master: int = 90
                  ) -> "tuple[list[int], dict[int, list[int]]]":
    """Auto-compute master/worker role assignment.

    Returns:
        master_ranks: sorted list of ranks acting as masters
        worker_groups: dict mapping master_rank -> [worker_ranks]
    """
    if comm_size <= 2:
        return [0], {0: list(range(1, comm_size))}

    num_avail = comm_size - 1  # rank 0 is always a master

    if num_avail <= workers_per_master:
        # Single master suffices
        return [0], {0: list(range(1, comm_size))}

    # Multiple masters needed
    num_masters = (num_avail + workers_per_master - 1) // workers_per_master
    # Each master needs at least 3 workers to be worthwhile
    num_masters = min(num_masters, num_avail // 3)
    num_masters = max(1, num_masters)

    master_ranks = list(range(num_masters))
    worker_ranks = list(range(num_masters, comm_size))

    # Round-robin assignment for even distribution
    groups = {m: [] for m in master_ranks}
    for i, w in enumerate(worker_ranks):
        m = master_ranks[i % num_masters]
        groups[m].append(w)

    return master_ranks, groups


def _ulfm_generation_layout(
    transport_endpoint_ranks: typing.Mapping[int, int],
    workers_per_master: int,
) -> tuple[list[int], dict[int, list[int]], dict[int, list[int]]]:
    """Return transport and stable worker groups for one repaired generation."""
    if (
        type(workers_per_master) is not int
        or workers_per_master < 1
        or any(
            type(transport) is not int or type(stable) is not int
            for transport, stable in transport_endpoint_ranks.items()
        )
    ):
        raise UlfmRuntimeError("ULFM generation layout input is invalid")
    current = dict(transport_endpoint_ranks)
    if (
        len(current) < 2
        or set(current) != set(range(len(current)))
        or len(set(current.values())) != len(current)
    ):
        raise UlfmRuntimeError(
            "ULFM generation transport membership is not dense and unique"
        )
    masters, transport_groups = compute_roles(
        len(current), workers_per_master
    )
    stable_groups = {
        master: [current[worker] for worker in workers]
        for master, workers in transport_groups.items()
    }
    return masters, transport_groups, stable_groups


def _ulfm_generation_manifest(
    *,
    generation: int,
    active_budget: int,
    transport_endpoint_ranks: typing.Mapping[int, int],
    master_ranks: typing.Sequence[int],
    transport_worker_groups: typing.Mapping[int, typing.Sequence[int]],
) -> dict[str, typing.Any]:
    """Seal the active/standby role assignment for one communicator generation."""
    current = dict(transport_endpoint_ranks)
    active_size = min(int(active_budget), len(current))
    if (
        type(generation) is not int
        or generation < 0
        or type(active_budget) is not int
        or active_budget < 3
        or len(current) < 2
        or set(current) != set(range(len(current)))
        or any(type(value) is not int for value in current.values())
        or list(master_ranks) != sorted(set(master_ranks))
        or any(
            master not in set(range(active_size))
            for master in master_ranks
        )
    ):
        raise UlfmRuntimeError("ULFM generation manifest input is invalid")
    active_transports = set(range(active_size))
    assigned_workers = {
        int(worker)
        for workers in transport_worker_groups.values()
        for worker in workers
    }
    if (
        set(transport_worker_groups) != set(master_ranks)
        or assigned_workers != active_transports - set(master_ranks)
        or any(
            worker not in active_transports
            for workers in transport_worker_groups.values()
            for worker in workers
        )
    ):
        raise UlfmRuntimeError("ULFM generation worker partition is invalid")
    body: dict[str, typing.Any] = {
        "schema": "symcc-ulfm-generation-layout-v1",
        "generation": generation,
        "active_budget": active_budget,
        "active_endpoints": [
            current[transport] for transport in range(active_size)
        ],
        "standby_endpoints": [
            current[transport] for transport in range(active_size, len(current))
        ],
        "masters": [
            {
                "transport_rank": master,
                "stable_endpoint": current[master],
                "workers": [
                    current[worker]
                    for worker in transport_worker_groups[master]
                ],
            }
            for master in master_ranks
        ],
    }
    body["manifest_sha256"] = ulfm_content_digest(body)
    return body


def _record_ulfm_generation_manifest(
    store_root: str,
    manifest: typing.Mapping[str, typing.Any],
) -> str:
    """Durably publish one sealed generation layout from the repaired root."""
    normalized = dict(manifest)
    supplied = normalized.pop("manifest_sha256", "")
    if (
        normalized.get("schema") != "symcc-ulfm-generation-layout-v1"
        or type(normalized.get("generation")) is not int
        or ulfm_content_digest(normalized) != supplied
    ):
        raise UlfmRuntimeError("ULFM generation manifest is not sealed")
    normalized["manifest_sha256"] = supplied
    durable_makedirs(store_root)
    path = os.path.join(
        store_root, f"generation-{normalized['generation']}-layout.json"
    )
    content = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii") + b"\n"
    try:
        observed = _read_bounded_regular_file(path, 1 << 20)
    except FileNotFoundError:
        _atomic_write(path, content)
    else:
        if observed != content:
            raise UlfmRuntimeError(
                "ULFM generation manifest conflicts with durable evidence"
            )
    return path


def _wait_ulfm_standby(
    comm: typing.Any,
    *,
    generation: int,
    monotonic: typing.Callable[[], float] = time.monotonic,
    sleep: typing.Callable[[float], None] = time.sleep,
) -> None:
    """Keep a prelaunched spare responsive to failure and clean shutdown."""
    failure_poll_interval = _environment_float(
        "SYMCC_ULFM_FAILURE_POLL_INTERVAL", 0.1, 0.01, 30.0
    )
    last_failure_poll = 0.0
    while True:
        if comm.iprobe(source=0, tag=TAG_ULFM_STANDBY_STOP):
            message = comm.recv(source=0, tag=TAG_ULFM_STANDBY_STOP)
            if (
                isinstance(message, dict)
                and message.get("schema") == "symcc-ulfm-standby-stop-v1"
                and message.get("generation") == generation
            ):
                return
        now = monotonic()
        if now - last_failure_poll >= failure_poll_interval:
            last_failure_poll = now
            failed_member_count = _ulfm_failed_member_count(comm)
            if failed_member_count:
                try:
                    comm.Revoke()
                except Exception as error:
                    if not is_ulfm_failure(error, MPI):
                        raise
                raise _UlfmProcessFailureDetected(
                    "ULFM standby detector observed "
                    f"{failed_member_count} failed member(s)"
                )
        sleep(min(0.01, failure_poll_interval))


def _environment_float(
    name: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    """Read one finite, bounded floating-point environment setting."""
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError, OverflowError):
        value = default
    if not math.isfinite(value):
        value = default
    return min(maximum, max(minimum, value))


def _environment_enabled(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).lower() not in {
        "0", "false", "off", "no"
    }


def _startup_trace(rank: int, stage: str) -> None:
    """Emit an opt-in rank-local checkpoint around MPI startup collectives."""
    if _environment_enabled("SYMCC_MPI_STARTUP_TRACE", "0"):
        print(
            f"[MPI startup trace] rank={rank} stage={stage}",
            file=sys.stderr,
            flush=True,
        )


class _UlfmProcessFailureDetected(UlfmRuntimeError):
    """A health poll observed a failed member before data traffic did."""


def _ulfm_failed_member_count(comm: typing.Any) -> int:
    """Acknowledge and count the communicator's locally known failures."""
    comm.Ack_failed()
    failed_group = comm.Get_failed()
    try:
        count = int(failed_group.Get_size())
    finally:
        try:
            failed_group.Free()
        except Exception:
            pass
    if count < 0 or count >= int(comm.Get_size()):
        raise UlfmRuntimeError("ULFM failure detector returned an invalid count")
    return count


def _start_ulfm_failure_sentinels(
    group_comm: typing.Any,
    workers: typing.Iterable[int],
) -> dict[int, tuple[bytearray, typing.Any]]:
    """Keep a transport operation pending against every owned worker.

    ``Get_failed`` reports locally known failures; it is not itself a failure
    detector. A worker can therefore disappear while an otherwise idle master
    sees an empty failed group indefinitely. These one-byte receives never
    carry application data, but make a connection failure observable as
    ``ERR_PROC_FAILED``.
    """
    sentinels: dict[int, tuple[bytearray, typing.Any]] = {}
    for raw_worker in workers:
        worker = int(raw_worker)
        if worker < 1 or worker in sentinels:
            raise ValueError(
                "ULFM sentinel worker ranks must be unique and positive"
            )
        buffer = bytearray(1)
        request = group_comm.Irecv(
            [buffer, MPI.BYTE],
            source=worker,
            tag=TAG_ULFM_FAILURE_SENTINEL,
        )
        sentinels[worker] = (buffer, request)
    return sentinels


def _cancel_ulfm_failure_sentinels(
    sentinels: typing.Mapping[int, tuple[bytearray, typing.Any]],
) -> None:
    """Cancel and retire healthy-generation sentinel receives."""
    for _buffer, request in sentinels.values():
        try:
            request.Cancel()
        except Exception:
            continue
        try:
            request.Wait()
        except Exception:
            # A concurrently revoked communicator already retired the request.
            pass


def _poll_ulfm_failure_sentinels(
    sentinels: dict[int, tuple[bytearray, typing.Any]],
    *,
    group_comm: typing.Any,
    recovery_comm: typing.Any,
) -> None:
    """Convert one transport-level worker failure into a global revoke."""
    for worker, (_buffer, request) in tuple(sentinels.items()):
        try:
            completed = request.Test()
            if isinstance(completed, tuple):
                completed = completed[0]
        except Exception as error:
            if not is_ulfm_failure(error, MPI):
                raise
            try:
                recovery_comm.Revoke()
            except Exception as revoke_error:
                if not is_ulfm_failure(revoke_error, MPI):
                    raise
            raise _UlfmProcessFailureDetected(
                f"ULFM transport sentinel observed failed worker {worker}"
            ) from error
        if completed:
            # No framework endpoint sends this tag. Re-arm defensively so an
            # injected/foreign message cannot disable subsequent detection.
            buffer = bytearray(1)
            sentinels[worker] = (
                buffer,
                group_comm.Irecv(
                    [buffer, MPI.BYTE],
                    source=worker,
                    tag=TAG_ULFM_FAILURE_SENTINEL,
                ),
            )


def _ulfm_standalone_policy(
    endpoint_count: int,
    shard_count: int,
) -> UlfmRecoveryPolicy:
    """Build the environment-scoped policy that every campaign rank seals."""
    return UlfmRecoveryPolicy(
        max_endpoints=max(2, int(endpoint_count)),
        max_shards=max(1, int(shard_count)),
        max_recovery_queue=max(1, int(shard_count)),
        max_repair_attempts=_environment_integer(
            "SYMCC_ULFM_REPAIR_ATTEMPTS", 3, 1, 64
        ),
        collective_timeout_seconds=_environment_float(
            "SYMCC_ULFM_COLLECTIVE_TIMEOUT", 30.0, 0.001, 3600.0
        ),
        poll_interval_seconds=_environment_float(
            "SYMCC_ULFM_POLL_INTERVAL", 0.01, 0.0001, 1.0
        ),
    )


def _ulfm_snapshot_file_mode() -> int:
    """Read a non-executable owner-writable octal snapshot mode."""
    raw = os.environ.get("SYMCC_ULFM_SNAPSHOT_FILE_MODE", "0600")
    if not re.fullmatch(r"[0-7]{3,4}", raw):
        raise ValueError(
            "SYMCC_ULFM_SNAPSHOT_FILE_MODE must be a three- or four-digit "
            "octal mode"
        )
    mode = int(raw, 8)
    if mode & ~0o777 or mode & 0o111 or mode & 0o600 != 0o600:
        raise ValueError(
            "SYMCC_ULFM_SNAPSHOT_FILE_MODE must be owner-readable, "
            "owner-writable, and non-executable"
        )
    return mode


def _initialize_ulfm_hot_path(
    *,
    run_id: str,
    master_global_rank: int,
    worker_global_ranks: typing.Sequence[int],
    endpoint_hosts: typing.Mapping[int, str],
    store_root: str,
    transport_endpoint_ranks: typing.Mapping[int, int] | None = None,
) -> tuple[DurableUlfmCoordinator, dict[int, str]]:
    """Open one durable generation fence and bind its current transport ranks."""
    if any(type(value) is not int for value in worker_global_ranks):
        raise UlfmRecoveryError("ULFM worker endpoint ranks must be integers")
    workers = tuple(int(value) for value in worker_global_ranks)
    if type(master_global_rank) is not int:
        raise UlfmRecoveryError("ULFM master endpoint rank must be an integer")
    if not workers or len(set(workers)) != len(workers):
        raise UlfmRecoveryError("ULFM worker group is empty or contains duplicates")
    global_ranks = (int(master_global_rank), *workers)
    if len(set(global_ranks)) != len(global_ranks):
        raise UlfmRecoveryError("ULFM master is also present in its worker group")
    endpoints: list[EndpointIdentity] = []
    for local_rank, global_rank in enumerate(global_ranks):
        host = endpoint_hosts.get(global_rank)
        if not isinstance(host, str) or not host:
            raise UlfmRecoveryError("ULFM endpoint host identity is missing")
        endpoint_id = f"rank-{global_rank}"
        incarnation = hashlib.sha256(
            f"{run_id}\0{endpoint_id}\0{host}".encode("utf-8")
        ).hexdigest()
        endpoints.append(
            EndpointIdentity(endpoint_id, local_rank, incarnation, host)
        )
    empty_checkpoint = hashlib.sha256(b"").hexdigest()
    shards = [
        RecoveryShard(
            shard_id=f"worker-{local_rank}",
            owner_endpoint=f"rank-{global_rank}",
            checkpoint_sha256=empty_checkpoint,
        )
        for local_rank, global_rank in enumerate(workers, 1)
    ]
    policy = _ulfm_standalone_policy(len(endpoints), len(shards))
    try:
        store = AtomicUlfmSnapshotStore(
            store_root,
            timeout=policy.collective_timeout_seconds,
            file_mode=_ulfm_snapshot_file_mode(),
        )
        if store.load_ulfm_recovery_state(run_id, policy) is None:
            controller = UlfmRecoveryController(run_id, endpoints, shards, policy)
            durable = DurableUlfmCoordinator(controller, store)
        else:
            durable = DurableUlfmCoordinator.restore(run_id, policy, store)
    except (OSError, RuntimeError, ValueError) as error:
        raise UlfmRecoveryError(
            "ULFM QueryStore initialization failed: "
            f"{type(error).__name__}: {error}"
        ) from error

    if transport_endpoint_ranks is None:
        current_transport = {
            local_rank: global_rank
            for local_rank, global_rank in enumerate(global_ranks)
        }
    else:
        if any(
            type(local) is not int or type(stable) is not int
            for local, stable in transport_endpoint_ranks.items()
        ):
            raise UlfmRecoveryError(
                "ULFM repaired endpoint ranks must be integers"
            )
        current_transport = dict(transport_endpoint_ranks)
    snapshot = durable.controller.snapshot()
    members = snapshot["members"]
    expected_transport_ranks = set(range(len(members)))
    if set(current_transport) != expected_transport_ranks:
        raise UlfmRecoveryError("ULFM repaired transport ranks are not dense")
    endpoint_by_transport = {
        transport_rank: f"rank-{stable_rank}"
        for transport_rank, stable_rank in current_transport.items()
    }
    if set(endpoint_by_transport.values()) != set(members) or any(
        members[endpoint]["rank"] != transport_rank
        for transport_rank, endpoint in endpoint_by_transport.items()
    ):
        raise UlfmRecoveryError(
            "ULFM repaired transport membership differs from durable state"
        )

    # Prefer a shard owned by the physical endpoint.  After a master failure a
    # promoted worker may own a shard itself, so the fallback assigns any
    # remaining logical shard while the exact send/result fence still binds it
    # to one transport worker.
    unused_shards = set(snapshot["shards"])
    worker_shards: dict[int, str] = {}
    for transport_rank in range(1, len(members)):
        endpoint = endpoint_by_transport[transport_rank]
        owned = sorted(
            shard_id
            for shard_id in unused_shards
            if snapshot["shards"][shard_id]["owner_endpoint"] == endpoint
        )
        candidates = owned or sorted(unused_shards)
        if not candidates:
            raise UlfmRecoveryError("ULFM repaired worker has no schedulable shard")
        worker_shards[transport_rank] = candidates[0]
        unused_shards.remove(candidates[0])
    return durable, worker_shards


def _ulfm_hot_path_store_root(
    work_state_dir: str,
    stable_master_rank: int,
    generation: int,
) -> str:
    """Return the generation-scoped durable state root for one master group."""
    if (
        not isinstance(work_state_dir, str)
        or not work_state_dir
        or type(stable_master_rank) is not int
        or stable_master_rank < 0
        or type(generation) is not int
        or generation < 0
    ):
        raise ValueError("ULFM hot-path store scope is invalid")
    return os.path.join(
        work_state_dir,
        "ulfm-query-store",
        f"master-{stable_master_rank}",
        f"generation-{generation}",
    )


def _repair_ulfm_hot_path(
    comm: typing.Any,
    *,
    run_id: str,
    local_endpoint_rank: int,
    initial_master_rank: int,
    initial_worker_ranks: typing.Sequence[int],
    endpoint_hosts: typing.Mapping[int, str],
    store_root: str,
    transport_endpoint_ranks: typing.Mapping[int, int],
) -> tuple[typing.Any, dict[str, typing.Any]]:
    """Repair one failed standalone communicator and durably advance its fence."""
    durable, _worker_shards = _initialize_ulfm_hot_path(
        run_id=run_id,
        master_global_rank=initial_master_rank,
        worker_global_ranks=initial_worker_ranks,
        endpoint_hosts=endpoint_hosts,
        store_root=store_root,
        transport_endpoint_ranks=transport_endpoint_ranks,
    )
    return _advance_ulfm_recovery(
        comm,
        durable=durable,
        local_endpoint_rank=local_endpoint_rank,
    )


def _initialize_ulfm_membership(
    *,
    run_id: str,
    endpoint_hosts: typing.Mapping[int, str],
    store_root: str,
    transport_endpoint_ranks: typing.Mapping[int, int],
) -> DurableUlfmCoordinator:
    """Open the global communicator-generation controller for all groups."""
    initial_ranks = tuple(sorted(endpoint_hosts))
    if (
        len(initial_ranks) < 3
        or initial_ranks != tuple(range(len(initial_ranks)))
        or any(
            type(rank) is not int
            or not isinstance(endpoint_hosts[rank], str)
            or not endpoint_hosts[rank]
            for rank in initial_ranks
        )
    ):
        raise UlfmRecoveryError("ULFM global endpoint inventory is invalid")
    endpoints = [
        EndpointIdentity(
            endpoint_id=f"rank-{rank}",
            initial_rank=rank,
            incarnation_sha256=hashlib.sha256(
                f"{run_id}\0rank-{rank}\0{endpoint_hosts[rank]}".encode("utf-8")
            ).hexdigest(),
            host_id=endpoint_hosts[rank],
        )
        for rank in initial_ranks
    ]
    policy = _ulfm_standalone_policy(len(endpoints), len(endpoints) - 1)
    try:
        store = AtomicUlfmSnapshotStore(
            store_root,
            timeout=policy.collective_timeout_seconds,
            file_mode=_ulfm_snapshot_file_mode(),
        )
        if store.load_ulfm_recovery_state(run_id, policy) is None:
            controller = UlfmRecoveryController(
                run_id,
                endpoints,
                [
                    RecoveryShard(
                        shard_id="global-membership",
                        owner_endpoint="rank-0",
                        checkpoint_sha256=hashlib.sha256(b"").hexdigest(),
                    )
                ],
                policy,
            )
            durable = DurableUlfmCoordinator(controller, store)
        else:
            durable = DurableUlfmCoordinator.restore(run_id, policy, store)
    except (OSError, RuntimeError, ValueError) as error:
        raise UlfmRecoveryError(
            "ULFM global QueryStore initialization failed: "
            f"{type(error).__name__}: {error}"
        ) from error

    if any(
        type(local) is not int or type(stable) is not int
        for local, stable in transport_endpoint_ranks.items()
    ):
        raise UlfmRecoveryError("ULFM global transport ranks must be integers")
    current_transport = dict(transport_endpoint_ranks)
    snapshot = durable.controller.snapshot()
    if set(current_transport) != set(range(len(snapshot["members"]))):
        raise UlfmRecoveryError("ULFM global transport ranks are not dense")
    endpoints_by_transport = {
        local: f"rank-{stable}" for local, stable in current_transport.items()
    }
    if set(endpoints_by_transport.values()) != set(snapshot["members"]) or any(
        snapshot["members"][endpoint]["rank"] != local
        for local, endpoint in endpoints_by_transport.items()
    ):
        raise UlfmRecoveryError(
            "ULFM global transport membership differs from durable state"
        )
    return durable


def _repair_ulfm_membership(
    comm: typing.Any,
    *,
    run_id: str,
    local_endpoint_rank: int,
    endpoint_hosts: typing.Mapping[int, str],
    store_root: str,
    transport_endpoint_ranks: typing.Mapping[int, int],
) -> tuple[typing.Any, dict[str, typing.Any]]:
    """Repair the all-rank communicator used by multiple master groups."""
    durable = _initialize_ulfm_membership(
        run_id=run_id,
        endpoint_hosts=endpoint_hosts,
        store_root=store_root,
        transport_endpoint_ranks=transport_endpoint_ranks,
    )
    return _advance_ulfm_recovery(
        comm,
        durable=durable,
        local_endpoint_rank=local_endpoint_rank,
    )


def _advance_ulfm_recovery(
    comm: typing.Any,
    *,
    durable: DurableUlfmCoordinator,
    local_endpoint_rank: int,
) -> tuple[typing.Any, dict[str, typing.Any]]:
    """Advance one already-open durable controller across a failed comm."""
    before = durable.controller.snapshot()
    endpoint_id = f"rank-{int(local_endpoint_rank)}"
    local_member = before["members"].get(endpoint_id)
    if not isinstance(local_member, dict):
        raise UlfmRuntimeError("local endpoint is outside durable ULFM membership")

    # Get_failed exposes only failures known to the local process. A remote
    # survivor woken by Revoke may therefore still observe an empty group. The
    # first shrink establishes a common survivor view; missing stable
    # identities are the exact failures.
    discovered_comm, failed_endpoints = _shrink_and_discover_ulfm_failures(
        comm,
        local_endpoint_rank=local_endpoint_rank,
        members=before["members"],
        policy=durable.controller.policy,
    )
    pending_plan = before.get("pending_recovery")
    collective_timeout = False
    try:
        if pending_plan is None:
            plan = durable.prepare_recovery(failed_endpoints)
        else:
            plan = dict(pending_plan)
            if not set(plan["suspected_failed_endpoints"]) <= set(
                failed_endpoints
            ):
                raise UlfmRuntimeError(
                    "durable ULFM plan exceeds the repaired failure set"
                )
        local_endpoint = EndpointIdentity(
            endpoint_id=endpoint_id,
            initial_rank=int(local_member["rank"]),
            incarnation_sha256=str(local_member["incarnation_sha256"]),
            host_id=str(local_member["host_id"]),
        )
        # Attest on a fresh communicator context. This second recovery-only
        # shrink also absorbs a failure that races the discovery collective.
        repaired = shrink_and_attest(
            discovered_comm,
            local_endpoint=local_endpoint,
            recovery_plan=plan,
            policy=durable.controller.policy,
            mpi=MPI,
        )
    except UlfmCollectiveTimeout:
        collective_timeout = True
        raise
    finally:
        if not collective_timeout:
            try:
                discovered_comm.Free()
            except Exception:
                pass
    receipt = durable.commit_recovery(plan["plan_sha256"], repaired.attestations)
    if tuple(receipt["failed_endpoints"]) != repaired.failed_endpoints:
        raise UlfmRuntimeError("ULFM receipt and repaired membership disagree")
    return repaired.communicator, receipt


def _shrink_and_discover_ulfm_failures(
    comm: typing.Any,
    *,
    local_endpoint_rank: int,
    members: typing.Mapping[str, typing.Mapping[str, typing.Any]],
    policy: UlfmRecoveryPolicy,
    monotonic: typing.Callable[[], float] = time.monotonic,
    sleep: typing.Callable[[float], None] = time.sleep,
) -> tuple[typing.Any, tuple[str, ...]]:
    """Shrink first, then agree on the exact missing stable endpoints."""
    endpoint_id = f"rank-{int(local_endpoint_rank)}"
    if endpoint_id not in members:
        raise UlfmRuntimeError("local endpoint is outside ULFM discovery membership")
    rank_to_endpoint: dict[int, str] = {}
    for raw_endpoint, raw_member in members.items():
        if (
            not isinstance(raw_endpoint, str)
            or not isinstance(raw_member, typing.Mapping)
            or type(raw_member.get("rank")) is not int
        ):
            raise UlfmRuntimeError("ULFM discovery membership is invalid")
        old_rank = int(raw_member["rank"])
        if old_rank in rank_to_endpoint:
            raise UlfmRuntimeError("ULFM discovery membership contains duplicates")
        rank_to_endpoint[old_rank] = raw_endpoint
    if set(rank_to_endpoint) != set(range(len(rank_to_endpoint))):
        raise UlfmRuntimeError("ULFM discovery membership is not dense")

    current = comm
    owns_current = False
    last_error = ""
    for _attempt in range(policy.max_repair_attempts):
        candidate = None
        try:
            current.Set_errhandler(MPI.ERRORS_RETURN)
            try:
                current.Revoke()
            except Exception as error:
                if not is_ulfm_failure(error, MPI):
                    raise
            candidate = deadline_shrink(
                current,
                policy=policy,
                monotonic=monotonic,
                sleep=sleep,
            )
            candidate.Set_errhandler(MPI.ERRORS_RETURN)
            candidate_size = int(candidate.Get_size())
            if not 1 <= candidate_size <= policy.max_endpoints:
                raise UlfmRuntimeError(
                    "ULFM repaired discovery size exceeds its budget"
                )
            send_rank = array("q", [int(local_endpoint_rank)])
            survivor_ranks = array("q", [0]) * candidate_size
            request = candidate.Iallgather(
                [send_rank, MPI.LONG_LONG],
                [survivor_ranks, MPI.LONG_LONG],
            )
            complete_collective_before_deadline(
                request,
                candidate,
                policy=policy,
                monotonic=monotonic,
                sleep=sleep,
            )
            observed = tuple(int(value) for value in survivor_ranks)
            survivors = tuple(f"rank-{value}" for value in observed)
            if (
                len(set(survivors)) != candidate_size
                or endpoint_id not in survivors
                or not set(survivors) <= set(members)
            ):
                raise UlfmRuntimeError(
                    "ULFM survivor discovery returned invalid identities"
                )
            failed = tuple(sorted(set(members) - set(survivors)))
            if not failed:
                raise UlfmRuntimeError(
                    "ULFM survivor discovery removed no endpoint"
                )
            if not deadline_agree(
                candidate,
                True,
                policy=policy,
                monotonic=monotonic,
                sleep=sleep,
            ):
                raise UlfmRuntimeError(
                    "ULFM survivor discovery agreement was rejected"
                )
            if owns_current and current is not candidate:
                try:
                    current.Free()
                except Exception:
                    pass
            return candidate, failed
        except UlfmCollectiveTimeout:
            raise
        except Exception as error:
            last_error = f"{type(error).__name__}: {error}"
        if candidate is not None:
            if owns_current and current is not candidate:
                try:
                    current.Free()
                except Exception:
                    pass
            current = candidate
            owns_current = True
    if owns_current:
        try:
            current.Free()
        except Exception:
            pass
    raise UlfmRuntimeError(
        "ULFM survivor discovery failed after "
        f"{policy.max_repair_attempts} attempt(s): "
        f"{last_error or 'unknown error'}"
    )


def _ulfm_test_failure_target(generation: int) -> tuple[int, bool] | None:
    """Return a test-only stable endpoint target and schedule-mode flag."""
    if type(generation) is not int or generation < 0:
        raise ValueError("ULFM test failure generation must be non-negative")
    raw_schedule = os.environ.get("SYMCC_ULFM_TEST_FAILURE_SCHEDULE")
    if raw_schedule is not None:
        entries = raw_schedule.split(",") if raw_schedule else []
        if not entries or len(entries) > 64:
            raise ValueError(
                "SYMCC_ULFM_TEST_FAILURE_SCHEDULE must contain 1..64 entries"
            )
        schedule: dict[int, int] = {}
        try:
            for entry in entries:
                raw_generation, raw_rank = entry.split(":", 1)
                selected_generation = int(raw_generation, 10)
                selected_rank = int(raw_rank, 10)
                if (
                    selected_generation < 0
                    or selected_rank < 0
                    or selected_generation in schedule
                ):
                    raise ValueError
                schedule[selected_generation] = selected_rank
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError(
                "SYMCC_ULFM_TEST_FAILURE_SCHEDULE entries must be unique "
                "non-negative generation:rank pairs"
            ) from error
        target = schedule.get(generation)
        return None if target is None else (target, True)

    raw_rank = os.environ.get("SYMCC_ULFM_TEST_FAIL_INITIAL_RANK")
    if raw_rank is None or generation != 0:
        return None
    try:
        selected_rank = int(raw_rank, 10)
        if selected_rank < 0:
            raise ValueError
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(
            "SYMCC_ULFM_TEST_FAIL_INITIAL_RANK must be an integer and "
            "non-negative"
        ) from error
    return selected_rank, False


def _claim_ulfm_test_failure(
    work_state_dir: str,
    stable_rank: int,
    generation: int = 0,
) -> bool:
    """Claim one generation-scoped physical-failure injection target."""
    selected = _ulfm_test_failure_target(generation)
    if selected is None:
        return False
    selected_rank, scheduled = selected
    if selected_rank != int(stable_rank):
        return False
    marker_name = (
        f".ulfm-test-failure-generation-{generation}-rank-{selected_rank}"
        if scheduled
        else f".ulfm-test-failure-rank-{selected_rank}"
    )
    marker = os.path.join(work_state_dir, marker_name)
    try:
        descriptor = os.open(
            marker,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
    except FileExistsError:
        return False
    try:
        os.write(descriptor, b"claimed\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    fsync_directory(work_state_dir)
    return True


def _decode_standalone_work_message(
    message: typing.Any,
) -> tuple[str, dict[str, typing.Any] | None]:
    """Return an input hash and optional verified ULFM result fence."""
    if isinstance(message, str):
        work_hash = _normalize_work_hash(message)
        if not work_hash:
            raise ValueError("standalone work hash is invalid")
        return work_hash, None
    try:
        envelope = verify_work_envelope(message)
    except UlfmRecoveryError as error:
        raise ValueError("standalone ULFM work envelope is invalid") from error
    payload = envelope["payload"]
    if set(payload) != {"input_hash"}:
        raise ValueError("standalone ULFM work payload shape changed")
    work_hash = _normalize_work_hash(payload["input_hash"])
    if not work_hash or envelope["fence"]["work_id"] != work_hash:
        raise ValueError("standalone ULFM work identity changed")
    return work_hash, envelope["fence"]


def _with_ulfm_result_fence(
    result: typing.Mapping[str, typing.Any],
    fence: typing.Mapping[str, typing.Any] | None,
) -> dict[str, typing.Any]:
    """Echo the exact dispatch fence without changing legacy result payloads."""
    normalized = dict(result)
    if fence is not None:
        normalized["ulfm_fence"] = dict(fence)
    return normalized


def _environment_integer(
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    """Read one strict bounded integer used by storage lifecycle policy."""
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw, 10)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{name} must be an integer") from error
    if not minimum <= value <= maximum:
        raise ValueError(
            f"{name} must be between {minimum} and {maximum}")
    return value


def _standalone_admission_budgets() -> dict[str, int]:
    """Read one internally consistent result/input admission contract."""
    result_max_objects = _environment_integer(
        "SYMCC_STANDALONE_RESULT_MAX_OBJECTS",
        _DEFAULT_RESULT_MAX_OBJECTS,
        1,
        _MAX_RESULT_MAX_OBJECTS,
    )
    result_max_bytes = _environment_integer(
        "SYMCC_STANDALONE_RESULT_MAX_BYTES",
        _DEFAULT_RESULT_MAX_BYTES,
        1,
        _MAX_RESULT_MAX_BYTES,
    )
    input_max_bytes = _environment_integer(
        "SYMCC_STANDALONE_INPUT_MAX_BYTES",
        _DEFAULT_INPUT_MAX_BYTES,
        1,
        _MAX_INPUT_MAX_BYTES,
    )
    if result_max_bytes > input_max_bytes:
        raise ValueError(
            "SYMCC_STANDALONE_RESULT_MAX_BYTES must not exceed "
            "SYMCC_STANDALONE_INPUT_MAX_BYTES"
        )
    return {
        "max_objects": result_max_objects,
        "max_bytes": result_max_bytes,
        "input_max_bytes": input_max_bytes,
    }


def _environment_strict_float(
    name: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    """Read one strict finite float used by storage lifecycle policy."""
    raw = os.environ.get(name, str(default))
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{name} must be a finite number") from error
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if not minimum <= value <= maximum:
        raise ValueError(
            f"{name} must be between {minimum} and {maximum}")
    return value


def _request_completed(request: typing.Any) -> bool:
    result = request.Test()
    if isinstance(result, tuple):
        result = result[0]
    return bool(result)


class _WorkerAvailabilityGate:
    """Hold READY until the preceding RESULT has retired active ownership."""

    def __init__(self, workers: typing.Iterable[int]) -> None:
        self._workers = frozenset(int(worker) for worker in workers)
        if not self._workers or any(worker < 1 for worker in self._workers):
            raise ValueError("worker ranks must be positive")
        self._ready: set[int] = set()
        self._quarantined: set[int] = set()

    def observe_ready(self, worker: int) -> str:
        worker = int(worker)
        if worker not in self._workers:
            return "unowned"
        if worker in self._ready:
            return "duplicate"
        self._ready.add(worker)
        if worker in self._quarantined:
            return "quarantined"
        return "ready"

    def quarantine(self, worker: int) -> bool:
        worker = int(worker)
        if worker not in self._workers:
            return False
        self._quarantined.add(worker)
        return True

    def claim(self, active_workers: typing.Mapping[int, typing.Any]) -> int | None:
        for worker in sorted(self._ready):
            if worker not in active_workers and worker not in self._quarantined:
                self._ready.remove(worker)
                return worker
        return None

    @property
    def ready(self) -> tuple[int, ...]:
        return tuple(sorted(self._ready))

    @property
    def usable(self) -> tuple[int, ...]:
        return tuple(sorted(self._workers - self._quarantined))


def _requeue_failed_assignment(
    pending_queue: deque[tuple[str, str]],
    assignment: tuple[str, str] | None,
    availability: _WorkerAvailabilityGate,
    worker: int,
    *,
    current_lease_token: str = "",
) -> str:
    """Preserve exact work ownership after a worker protocol failure."""
    availability.quarantine(worker)
    if assignment is None:
        return "unowned"
    work_hash, lease_token = assignment
    if lease_token and lease_token != current_lease_token:
        return "stale"
    pending_queue.appendleft((work_hash, lease_token))
    return "requeued" if availability.usable else "exhausted"


def _refresh_quiescence_frontier(
    refreshers: typing.Iterable[typing.Callable[[], typing.Any]],
    is_idle: typing.Callable[[], bool],
) -> bool:
    """Refresh every work source before accepting a local PREPARE boundary."""
    for refresh in refreshers:
        refresh()
    return bool(is_idle())


def _worker_result_payload(
    message: typing.Any,
    *,
    expected_input_hash: str = "",
    max_objects: int = _DEFAULT_RESULT_MAX_OBJECTS,
    max_bytes: int = _DEFAULT_RESULT_MAX_BYTES,
) -> tuple[tuple[str, ...], int, int] | None:
    """Validate the worker result fields consumed by scheduling and metrics."""
    if (isinstance(max_objects, bool) or not isinstance(max_objects, int)
            or max_objects < 1):
        raise ValueError("worker-result object limit must be positive")
    if (isinstance(max_bytes, bool) or not isinstance(max_bytes, int)
            or max_bytes < 1):
        raise ValueError("worker-result byte limit must be positive")
    if not isinstance(message, dict):
        return None
    protocol_error = message.get("protocol_error")
    if protocol_error is not None and protocol_error != "":
        return None
    if expected_input_hash:
        expected = _normalize_work_hash(expected_input_hash)
        observed = _normalize_work_hash(message.get("input_hash"))
        if not expected or observed != expected:
            return None
    raw_hashes = message.get("new_hashes")
    if not isinstance(raw_hashes, (list, tuple)):
        return None
    if len(raw_hashes) > max_objects:
        raise _ResultBudgetExceeded(
            "objects", len(raw_hashes), max_objects)
    hashes: list[str] = []
    for value in raw_hashes:
        if (not isinstance(value, str) or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)):
            return None
        hashes.append(value)
    staging_id = _normalize_staging_id(message.get("staging_id"))
    if hashes and not staging_id:
        return None
    raw_staging_id = message.get("staging_id")
    if not hashes and raw_staging_id is not None and raw_staging_id != "":
        return None
    generated = message.get("num_generated", len(hashes))
    if (isinstance(generated, bool) or not isinstance(generated, int)
            or generated != len(hashes)):
        return None
    if generated > max_objects:
        raise _ResultBudgetExceeded("objects", generated, max_objects)
    staged_bytes = message.get("staged_bytes")
    if (isinstance(staged_bytes, bool) or not isinstance(staged_bytes, int)
            or staged_bytes < 0):
        return None
    if staged_bytes > max_bytes:
        raise _ResultBudgetExceeded("bytes", staged_bytes, max_bytes)
    if not hashes and staged_bytes != 0:
        return None
    return tuple(hashes), generated, staged_bytes


def _worker_result_budget_violation(
    message: typing.Any,
    *,
    expected_input_hash: str,
    max_objects: int,
    max_bytes: int,
) -> _ResultBudgetExceeded | None:
    """Validate a worker's explicit, non-retryable admission failure."""
    if (not isinstance(message, dict)
            or message.get("protocol_error") != _RESULT_BUDGET_PROTOCOL_ERROR
            or _normalize_work_hash(message.get("input_hash"))
            != _normalize_work_hash(expected_input_hash)
            or message.get("new_hashes") not in ([], ())
            or message.get("num_generated") != 0
            or message.get("staged_bytes") != 0):
        return None
    raw_staging_id = message.get("staging_id")
    if (raw_staging_id not in (None, "")
            and not _normalize_staging_id(raw_staging_id)):
        return None
    detail = message.get("result_budget")
    if not isinstance(detail, dict) or set(detail) != {
            "resource", "observed", "limit"}:
        return None
    resource = detail.get("resource")
    observed = detail.get("observed")
    limit = detail.get("limit")
    expected_limit = (
        max_objects if resource == "objects" else
        max_bytes if resource == "bytes" else
        None
    )
    if (isinstance(observed, bool) or not isinstance(observed, int)
            or isinstance(limit, bool) or not isinstance(limit, int)
            or limit != expected_limit or observed <= limit):
        return None
    try:
        return _ResultBudgetExceeded(resource, observed, limit)
    except ValueError:
        return None


def _worker_input_budget_violation(
    message: typing.Any,
    *,
    expected_input_hash: str,
    max_bytes: int,
) -> _InputBudgetExceeded | None:
    """Validate a worker's explicit, non-retryable input-byte overflow."""
    if (not isinstance(message, dict)
            or message.get("protocol_error") != _INPUT_BUDGET_PROTOCOL_ERROR
            or _normalize_work_hash(message.get("input_hash"))
            != _normalize_work_hash(expected_input_hash)
            or message.get("new_hashes") not in ([], ())
            or message.get("num_generated") != 0
            or message.get("staged_bytes") != 0
            or message.get("staging_id") not in (None, "")):
        return None
    detail = message.get("input_budget")
    if not isinstance(detail, dict) or set(detail) != {"observed", "limit"}:
        return None
    observed = detail.get("observed")
    limit = detail.get("limit")
    if (isinstance(observed, bool) or not isinstance(observed, int)
            or isinstance(limit, bool) or not isinstance(limit, int)
            or limit != max_bytes or observed <= limit):
        return None
    try:
        return _InputBudgetExceeded(observed, limit)
    except ValueError:
        return None


def _idle_time_remaining(
    idle_started: float,
    timeout: float,
    now: float,
) -> float:
    """Return deadline-based idle time without round-count truncation."""
    try:
        started = float(idle_started)
        timeout_value = float(timeout)
        current = float(now)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not all(map(math.isfinite, (started, timeout_value, current))):
        return 0.0
    timeout_value = max(0.0, timeout_value)
    elapsed = max(0.0, current - started)
    return max(0.0, timeout_value - elapsed)


def _dispatch_window_open(
    wall_timeout: float,
    wall_started: float,
    execution_timeout: float,
    now: float,
) -> bool:
    """Return whether one bounded target execution still fits the campaign."""
    try:
        wall = float(wall_timeout)
        started = float(wall_started)
        execution = float(execution_timeout)
        current = float(now)
    except (TypeError, ValueError, OverflowError):
        return False
    if not all(map(math.isfinite, (wall, started, execution, current))):
        return False
    if wall <= 0.0:
        return True
    remaining = wall - max(0.0, current - started)
    return remaining > max(0.0, execution)


_WORK_HASH_LENGTH = 64


def _normalize_work_hash(value: typing.Any) -> str:
    if not isinstance(value, str) or len(value) != _WORK_HASH_LENGTH:
        return ""
    if any(char not in "0123456789abcdef" for char in value):
        return ""
    return value


_STANDALONE_COMMIT_SCHEMA = "symcc-standalone-result-commit-v1"


def _standalone_commit_manifest(
    worker_global_rank: typing.Any,
    staging_id: typing.Any,
    hashes: typing.Any,
    num_generated: typing.Any,
) -> dict[str, typing.Any] | None:
    if (isinstance(worker_global_rank, bool)
            or not isinstance(worker_global_rank, int)
            or worker_global_rank < 0
            or not isinstance(hashes, (list, tuple))
            or isinstance(num_generated, bool)
            or not isinstance(num_generated, int)
            or num_generated < 0):
        return None
    normalized_hashes = tuple(_normalize_work_hash(value) for value in hashes)
    if any(not value for value in normalized_hashes):
        return None
    unique_hashes = tuple(sorted(set(normalized_hashes)))
    if num_generated < len(normalized_hashes):
        return None
    normalized_stage = _normalize_staging_id(staging_id)
    if unique_hashes and not normalized_stage:
        return None
    if (not unique_hashes
            and staging_id is not None and staging_id != ""):
        return None
    return {
        "schema": _STANDALONE_COMMIT_SCHEMA,
        "worker_global_rank": worker_global_rank,
        "staging_id": normalized_stage,
        "hashes": list(unique_hashes),
        "num_generated": num_generated,
    }


def _normalize_standalone_commit_manifest(
    value: typing.Any,
) -> dict[str, typing.Any] | None:
    if (not isinstance(value, dict)
            or value.get("schema") != _STANDALONE_COMMIT_SCHEMA):
        return None
    normalized = _standalone_commit_manifest(
        value.get("worker_global_rank"),
        value.get("staging_id"),
        value.get("hashes"),
        value.get("num_generated"),
    )
    return normalized if normalized == value else None


def _ensure_work_state_metadata(
    root: str,
    *,
    epoch: str,
    shard_count: int,
) -> None:
    """Create or verify immutable layout metadata for epoch recovery."""
    expected = {
        "schema": "symcc-standalone-work-state-v1",
        "epoch": epoch,
        "shard_count": int(shard_count),
    }
    durable_makedirs(root)
    path = os.path.join(root, "state.json")
    temporary = (
        f"{path}.{os.getpid()}.{time.monotonic_ns()}."
        f"{os.urandom(8).hex()}.tmp"
    )
    try:
        with open(temporary, "xb") as stream:
            stream.write(json.dumps(
                expected, sort_keys=True, separators=(",", ":")
            ).encode("ascii") + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            durable_link(temporary, path)
        except FileExistsError:
            pass
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass
    try:
        with open(path, encoding="ascii") as stream:
            observed = json.load(stream)
    except (OSError, ValueError, TypeError) as error:
        raise ValueError("work epoch metadata is unreadable") from error
    if observed != expected:
        raise ValueError(
            "work epoch metadata does not match epoch/shard configuration")


_RUNTIME_LOCK_CONFIGURATION_MANIFEST = "renewal-configuration.json"
_RUNTIME_LOCK_CONFIGURATION_MANIFEST_LIMIT = 4096


def _read_bounded_regular_file(path: str, limit: int) -> bytes:
    """Read one bounded regular file without following a final symlink."""
    if type(limit) is not int or limit < 1:
        raise ValueError("invalid bounded-file size")
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise OSError("O_NOFOLLOW is unavailable")
    flags |= no_follow
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > limit
        ):
            raise ValueError("runtime lock configuration manifest is malformed")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > limit:
            raise ValueError("runtime lock configuration manifest is oversized")
        return content
    finally:
        os.close(descriptor)


def _runtime_lock_configuration_manifest_matches(
    path: str,
    content: bytes,
) -> bool:
    """Return false only when absent; reject malformed or different state."""
    try:
        observed = _read_bounded_regular_file(
            path,
            _RUNTIME_LOCK_CONFIGURATION_MANIFEST_LIMIT,
        )
    except FileNotFoundError:
        return False
    except (OSError, ValueError) as error:
        raise ValueError(
            "runtime lock configuration manifest is unreadable"
        ) from error
    if observed != content:
        raise ValueError(
            "runtime lock configuration manifest does not match consensus")
    return True


def _ensure_runtime_lock_configuration_manifest(
    root: str,
    controller: ClusterLockRenewalController,
) -> dict[str, typing.Any]:
    """Bind one live MPI renewal consensus to durable work-epoch state."""
    if not isinstance(controller, ClusterLockRenewalController):
        raise TypeError("invalid runtime lock renewal controller")
    if not controller.configuration_consensus_established:
        raise RuntimeError(
            "runtime lock configuration consensus is not established")
    configuration = controller.configuration_snapshot()
    expected = {
        "schema": "symcc-runtime-lock-configuration-manifest-v1",
        "epoch": controller.epoch,
        "fingerprint": controller.configuration_fingerprint,
        "configuration": configuration,
    }
    content = json.dumps(
        expected,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii") + b"\n"
    durable_makedirs(root)
    path = os.path.join(root, _RUNTIME_LOCK_CONFIGURATION_MANIFEST)
    if _runtime_lock_configuration_manifest_matches(path, content):
        return expected
    temporary = (
        f"{path}.{os.getpid()}.{time.monotonic_ns()}."
        f"{os.urandom(8).hex()}.tmp"
    )
    try:
        with open(temporary, "xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            durable_link(temporary, path)
        except FileExistsError:
            pass
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass
    if not _runtime_lock_configuration_manifest_matches(path, content):
        raise RuntimeError(
            "runtime lock configuration manifest publication disappeared")
    return expected


_ACTIVE_WORK_STATE_PREFIX = ".standalone-work-"
_RETIRED_WORK_STATE_PREFIX = ".retired-standalone-work-"
_RETIREMENT_ID_LENGTH = 32
_RETIRED_WORK_STATE_GC_LOCK = ".symcc-retired-work-state-gc.lock"
_RETIREMENT_PROBE_PREFIX = ".symcc-retirement-noreplace-probe-"


def _probe_retirement_noreplace(shared_dir: str) -> None:
    """Fail fast unless the persistent root enforces no-clobber rename."""
    shared = os.path.abspath(shared_dir)
    probe = os.path.join(
        shared,
        f"{_RETIREMENT_PROBE_PREFIX}{os.getpid()}-{time.monotonic_ns()}-"
        f"{os.urandom(16).hex()}",
    )
    created = False
    try:
        os.mkdir(probe)
        created = True
        fsync_directory(probe)
        fsync_directory(shared)
        active = os.path.join(probe, "active")
        retired = os.path.join(probe, "retired")
        durable_makedirs(active, exist_ok=False)
        durable_rename_noreplace(active, retired)
        durable_makedirs(active, exist_ok=False)
        try:
            durable_rename_noreplace(active, retired)
        except FileExistsError:
            pass
        else:
            raise OSError(
                errno.EOPNOTSUPP,
                "RENAME_NOREPLACE did not reject an existing destination",
                shared,
            )
    finally:
        if created:
            durable_rmtree(probe)


def _completed_work_state_epoch(work_state: str, shared: str) -> str:
    if os.path.dirname(work_state) != shared:
        raise ValueError("work state is not a direct child of the shared root")
    name = os.path.basename(work_state)
    epoch = (
        name[len(_ACTIVE_WORK_STATE_PREFIX):]
        if name.startswith(_ACTIVE_WORK_STATE_PREFIX)
        else ""
    )
    if (
        len(epoch) != _CONTROL_TOKEN_LENGTH
        or any(char not in "0123456789abcdef" for char in epoch)
    ):
        raise ValueError("invalid completed work-state identity")
    try:
        metadata = os.lstat(work_state)
    except OSError:
        raise
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("completed work state is not a real directory")
    return epoch


def _retired_work_state_name(epoch: str, retirement_id: str) -> str:
    if (
        len(epoch) != _CONTROL_TOKEN_LENGTH
        or any(char not in "0123456789abcdef" for char in epoch)
        or len(retirement_id) != _RETIREMENT_ID_LENGTH
        or any(char not in "0123456789abcdef" for char in retirement_id)
    ):
        raise ValueError("invalid retired work-state identity")
    return f"{_RETIRED_WORK_STATE_PREFIX}{epoch}-{retirement_id}"


def _parse_retired_work_state_name(name: str) -> tuple[str, str] | None:
    if not name.startswith(_RETIRED_WORK_STATE_PREFIX):
        return None
    suffix = name[len(_RETIRED_WORK_STATE_PREFIX):]
    if suffix.count("-") != 1:
        raise ValueError("malformed reserved retired work-state name")
    epoch, retirement_id = suffix.split("-", 1)
    if _retired_work_state_name(epoch, retirement_id) != name:
        raise ValueError("malformed reserved retired work-state name")
    return epoch, retirement_id


@dataclass(frozen=True)
class _RetiredWorkStateGcResult:
    reclaimed_roots: tuple[str, ...]
    partial_root: str | None
    removed_entries: int
    stop_reason: str
    scanned_entries: int = 0
    candidate_roots: int = 0


class _ReverseLexicalRetiredName:
    """Make heap[0] the lexically greatest retained retired-root name."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __lt__(self, other: "_ReverseLexicalRetiredName") -> bool:
        return self.name > other.name


def _select_retired_work_states(
    shared: str,
    limit: int,
) -> tuple[tuple[str, ...], int, int]:
    """Validate the complete namespace while retaining only lexical top-k."""
    retained: list[_ReverseLexicalRetiredName] = []
    scanned_entries = 0
    candidate_roots = 0
    with os.scandir(shared) as entries:
        for entry in entries:
            scanned_entries += 1
            parsed = _parse_retired_work_state_name(entry.name)
            if parsed is None:
                continue
            try:
                is_directory = entry.is_dir(follow_symlinks=False)
            except OSError as error:
                raise ValueError(
                    "retired work-state type is unreadable") from error
            if not is_directory:
                raise ValueError(
                    "retired work state is not a real directory")
            candidate_roots += 1
            if len(retained) < limit:
                heapq.heappush(
                    retained, _ReverseLexicalRetiredName(entry.name))
            elif entry.name < retained[0].name:
                heapq.heapreplace(
                    retained, _ReverseLexicalRetiredName(entry.name))
    return (
        tuple(sorted(candidate.name for candidate in retained)),
        scanned_entries,
        candidate_roots,
    )


def _reclaim_retired_work_states(
    shared_dir: str,
    *,
    limit: int,
    lock_timeout: float,
    entry_budget: int = 4096,
    time_budget: float = 0.05,
) -> _RetiredWorkStateGcResult:
    """Incrementally reclaim exact retired roots under three startup budgets."""
    if type(limit) is not int or limit < 0:
        raise ValueError("retired work-state GC limit must be non-negative")
    if not math.isfinite(lock_timeout) or lock_timeout <= 0.0:
        raise ValueError("retired work-state GC lock timeout must be positive")
    if type(entry_budget) is not int or entry_budget <= 0:
        raise ValueError(
            "retired work-state GC entry budget must be a positive integer")
    if not math.isfinite(time_budget) or time_budget <= 0.0:
        raise ValueError(
            "retired work-state GC time budget must be finite and positive")
    if limit == 0:
        return _RetiredWorkStateGcResult((), None, 0, "disabled")
    shared = os.path.abspath(shared_dir)
    lock_path = os.path.join(shared, _RETIRED_WORK_STATE_GC_LOCK)
    reclaimed: list[str] = []
    partial_root: str | None = None
    removed_entries = 0
    stop_reason = "empty"
    scanned_entries = 0
    candidate_roots = 0
    with bounded_advisory_lock(
        lock_path,
        timeout=lock_timeout,
        description="retired work-state garbage-collection lock",
    ):
        started = time.monotonic()
        deadline = started + time_budget
        selected, scanned_entries, candidate_roots = (
            _select_retired_work_states(shared, limit))
        for name in selected:
            now = time.monotonic()
            if removed_entries >= entry_budget:
                stop_reason = "entry-budget"
                break
            if removed_entries > 0 and now >= deadline:
                stop_reason = "time-budget"
                break
            step = durable_rmtree_step(
                os.path.join(shared, name),
                entry_limit=entry_budget - removed_entries,
                time_limit=max(1e-9, deadline - now),
            )
            removed_entries += step.removed_entries
            if not step.complete:
                partial_root = name
                stop_reason = step.stop_reason.replace("-limit", "-budget")
                break
            reclaimed.append(name)
        else:
            if candidate_roots > len(selected):
                stop_reason = "root-limit"
            elif selected:
                stop_reason = "complete"

    return _RetiredWorkStateGcResult(
        reclaimed_roots=tuple(reclaimed),
        partial_root=partial_root,
        removed_entries=removed_entries,
        stop_reason=stop_reason,
        scanned_entries=scanned_entries,
        candidate_roots=candidate_roots,
    )


def _cleanup_completed_work_state(
    work_state_dir: str,
    shared_dir: str,
    *,
    remove_shared_dir: bool,
) -> str | None:
    """Durably retire a completed epoch before reporting MPI success."""
    work_state = os.path.abspath(work_state_dir)
    shared = os.path.abspath(shared_dir)
    epoch = _completed_work_state_epoch(work_state, shared)
    if remove_shared_dir:
        durable_rmtree(work_state)
        durable_rmtree(shared)
        return None

    retirement_id = os.urandom(_RETIREMENT_ID_LENGTH // 2).hex()
    retired = os.path.join(
        shared, _retired_work_state_name(epoch, retirement_id))
    durable_rename_noreplace(work_state, retired)
    return retired


def _rendezvous_work_owner(
    work_hash: typing.Any,
    master_ranks: typing.Iterable[int],
) -> int:
    """Choose a stable owner while minimizing remapping across topology changes."""
    normalized = _normalize_work_hash(work_hash)
    masters = tuple(sorted({int(rank) for rank in master_ranks}))
    if not normalized or not masters or any(rank < 0 for rank in masters):
        raise ValueError("invalid rendezvous work coordinates")
    scored = []
    for rank in masters:
        material = f"symcc-work-owner-v1\0{normalized}\0{rank}".encode("ascii")
        score = int.from_bytes(hashlib.sha256(material).digest()[:16], "big")
        scored.append((score, -rank, rank))
    return max(scored)[2]


class _SharedWorkCoordinator:
    """Lease-fenced global ownership for content-addressed standalone work."""

    _SCHEMA = "symcc-standalone-work-v1"

    def __init__(
        self,
        root: str,
        *,
        rank: int,
        master_ranks: typing.Iterable[int],
        epoch: str,
        shard_count: int,
        lease_ttl: float,
        lock_ttl: float,
        lock_acquire_timeout: float,
        verify_filesystem: bool = False,
        filesystem_probe_timeout: float = 5.0,
        publication_root: str | None = None,
        filesystem_capabilities: SharedFilesystemCapabilities | None = None,
    ) -> None:
        self.rank = int(rank)
        self.master_ranks = tuple(sorted({int(item) for item in master_ranks}))
        if self.rank not in self.master_ranks:
            raise ValueError("work coordinator rank is not a master")
        token = _normalize_control_token(epoch)
        if not token:
            raise ValueError("invalid work coordinator epoch")
        self.epoch = token
        if filesystem_capabilities is not None:
            if not isinstance(
                    filesystem_capabilities, SharedFilesystemCapabilities):
                raise TypeError("invalid prequalified filesystem capabilities")
            expected_publication = os.path.realpath(publication_root or root)
            if (
                filesystem_capabilities.root != os.path.realpath(root)
                or filesystem_capabilities.publication_root
                != expected_publication
                or set(FULL_SHARED_FILESYSTEM_REQUIREMENTS.required_operations)
                - set(filesystem_capabilities.required_operations)
                or any(
                    getattr(filesystem_capabilities, operation) is not True
                    for operation in
                    FULL_SHARED_FILESYSTEM_REQUIREMENTS.required_operations
                )
            ):
                raise ValueError(
                    "prequalified filesystem capabilities do not satisfy "
                    "the standalone shared-state contract"
                )
            self.filesystem_capabilities = filesystem_capabilities
        else:
            self.filesystem_capabilities = (
                probe_shared_state_filesystem(
                    root,
                    timeout=filesystem_probe_timeout,
                    publication_root=publication_root,
                )
                if verify_filesystem else None
            )
        _ensure_work_state_metadata(
            root, epoch=token, shard_count=shard_count)
        self.owner = f"standalone:{token}:{self.rank}"
        self.table = FencedWorkLeaseTable(
            root,
            shard_count=shard_count,
            lease_ttl=lease_ttl,
            lock_ttl=lock_ttl,
            lock_acquire_timeout=lock_acquire_timeout,
        )
        self.tokens: dict[str, str] = {}
        self.heartbeat_batches = 0
        self.heartbeat_renewals = 0
        self.heartbeat_directory_syncs = 0
        self.heartbeat_failures = 0

    def designated_owner(self, work_hash: typing.Any) -> int:
        return _rendezvous_work_owner(work_hash, self.master_ranks)

    def _claim(
        self,
        work_hash: typing.Any,
        *,
        origin: str,
        lease_ttl: float | None = None,
    ) -> str | None:
        normalized = _normalize_work_hash(work_hash)
        if not normalized or normalized in self.tokens:
            return None
        normalized_origin = origin if origin in {"initial", "generated"} \
            else "generated"
        payload = {
            "schema": self._SCHEMA,
            "hash": normalized,
            "origin": normalized_origin,
        }
        token = self.table.claim(
            normalized,
            payload,
            owner=self.owner,
            lease_ttl=lease_ttl,
        )
        if token:
            self.tokens[normalized] = token
        return token

    def claim_owned(
        self,
        work_hash: typing.Any,
        *,
        origin: str,
    ) -> str | None:
        normalized = _normalize_work_hash(work_hash)
        if not normalized or self.designated_owner(normalized) != self.rank:
            return None
        return self._claim(normalized, origin=origin)

    def reclaim(
        self,
        work_hash: typing.Any,
        *,
        origin: str,
        lease_ttl: float | None = None,
    ) -> str | None:
        return self._claim(
            work_hash, origin=origin, lease_ttl=lease_ttl)

    def heartbeat_all(self) -> tuple[str, ...]:
        leases = dict(self.tokens)
        if not leases:
            return ()
        try:
            result = self.table.heartbeat_many(leases)
            if (
                not isinstance(result, LeaseHeartbeatBatch)
                or type(result.renewed) is not tuple
                or type(result.lost) is not tuple
                or type(result.directory_syncs) is not int
                or result.directory_syncs < 0
                or any(
                    _normalize_work_hash(work_hash) != work_hash
                    for work_hash in result.renewed + result.lost
                )
                or len(set(result.renewed)) != len(result.renewed)
                or len(set(result.lost)) != len(result.lost)
                or set(result.renewed) & set(result.lost)
                or set(result.renewed) | set(result.lost) != set(leases)
            ):
                raise TypeError("invalid work lease heartbeat batch result")
        except Exception:
            self.heartbeat_failures += 1
            raise
        self.heartbeat_batches += 1
        self.heartbeat_renewals += len(result.renewed)
        self.heartbeat_directory_syncs += result.directory_syncs
        for work_hash in result.lost:
            self.tokens.pop(work_hash, None)
        return result.lost

    def begin_commit(
        self,
        work_hash: typing.Any,
        token: typing.Any,
        commit: dict[str, typing.Any],
    ) -> bool:
        normalized = _normalize_work_hash(work_hash)
        token = str(token or "")
        return bool(
            normalized
            and self.tokens.get(normalized) == token
            and _normalize_standalone_commit_manifest(commit) is not None
            and self.table.begin_commit(
                normalized, token, commit=dict(commit))
        )

    def finish_commit(self, work_hash: typing.Any, token: typing.Any) -> str:
        normalized = _normalize_work_hash(work_hash)
        token = str(token or "")
        if not normalized or not token:
            return "stale"
        completed = self.table.complete_once(normalized, token)
        if completed in {"completed", "already"}:
            self.tokens.pop(normalized, None)
        return completed

    def replay_commit(
        self,
        work_hash: typing.Any,
        token: typing.Any,
        publish: typing.Callable[[dict[str, typing.Any]], None],
    ) -> str:
        """Serialize WAL replay, corpus publication, and completion."""
        normalized = _normalize_work_hash(work_hash)
        token = str(token or "")
        if not normalized or not token or not callable(publish):
            return "stale"

        def validate_and_publish(raw: dict[str, typing.Any]) -> None:
            manifest = _normalize_standalone_commit_manifest(raw)
            if manifest is None:
                raise ValueError(
                    f"invalid standalone commit manifest: {normalized}"
                )
            publish(manifest)

        completed = self.table.replay_commit_once(
            normalized,
            token,
            validate_and_publish,
        )
        if completed in {"completed", "already"}:
            self.tokens.pop(normalized, None)
        return completed

    def complete(self, work_hash: typing.Any, token: typing.Any) -> bool:
        normalized = _normalize_work_hash(work_hash)
        token = str(token or "")
        if (not normalized or not token
                or self.tokens.get(normalized) != token):
            return False
        return self.finish_commit(normalized, token) in {
            "completed", "already"}

    def abandon(self, work_hash: typing.Any, token: typing.Any) -> bool:
        normalized = _normalize_work_hash(work_hash)
        token = str(token or "")
        if (not normalized or not token
                or self.tokens.get(normalized) != token):
            return False
        abandoned = self.table.abandon(normalized, token)
        if abandoned:
            self.tokens.pop(normalized, None)
        return abandoned

    def recover_expired(
        self,
        *,
        limit: int = 4096,
        lease_ttl: float | None = None,
    ) -> tuple[tuple[str, str], ...]:
        candidates: list[tuple[str, str]] = []
        records = self.table.recover_expired_records(
            limit=limit, lease_ttl=lease_ttl)
        # Validate the whole scan before issuing a new fencing token.  A bad
        # record must not leave a partially recovered batch behind and then be
        # mistaken for an empty frontier by quiescence.
        for record_hash, payload in records:
            if (not isinstance(payload, dict)
                    or payload.get("schema") != self._SCHEMA
                    or _normalize_work_hash(payload.get("hash")) != record_hash
                    or payload.get("origin") not in {"initial", "generated"}):
                raise ValueError(
                    f"invalid standalone leased payload: {record_hash}")
            candidates.append((record_hash, str(payload["origin"])))

        recovered: list[tuple[str, str]] = []
        for work_hash, origin in candidates:
            if self.reclaim(
                    work_hash, origin=origin, lease_ttl=lease_ttl):
                recovered.append((work_hash, origin))
        return tuple(recovered)

    def recover_committing(
        self,
        *,
        limit: int = 4096,
    ) -> tuple[tuple[str, str, dict[str, typing.Any]], ...]:
        recovered: list[tuple[str, str, dict[str, typing.Any]]] = []
        for record_hash, record in self.table.snapshot_records():
            payload = record.get("payload")
            if (not isinstance(payload, dict)
                    or payload.get("schema") != self._SCHEMA
                    or _normalize_work_hash(payload.get("hash")) != record_hash
                    or payload.get("origin") not in {"initial", "generated"}):
                raise ValueError(
                    f"invalid standalone work payload: {record_hash}")
            if record.get("status") != "committing":
                continue
            token = str(record.get("token", ""))
            commit = record.get("commit")
            if not isinstance(commit, dict):
                raise ValueError(
                    f"committing record lacks durable manifest: {record_hash}")
            normalized_commit = _normalize_standalone_commit_manifest(commit)
            if normalized_commit is None:
                raise ValueError(
                    f"invalid standalone commit manifest: {record_hash}")
            recovered.append((record_hash, token, normalized_commit))
        if limit > 0:
            recovered = recovered[:limit]
        return tuple(recovered)


def _durable_completed_work_statistics(
    coordinator: _SharedWorkCoordinator,
) -> dict[str, typing.Any]:
    """Derive cross-generation counters from exactly-once completed WAL rows."""
    if not isinstance(coordinator, _SharedWorkCoordinator):
        raise TypeError("invalid standalone work coordinator")
    generated = 0
    analyzed = 0
    by_master: dict[int, int] = {}
    owner_prefix = f"standalone:{coordinator.epoch}:"
    for work_hash, record in coordinator.table.snapshot_records():
        if record.get("status") != "done":
            continue
        payload = record.get("payload")
        commit = _normalize_standalone_commit_manifest(record.get("commit"))
        if (
            not isinstance(payload, dict)
            or payload.get("schema") != coordinator._SCHEMA
            or payload.get("hash") != work_hash
            or _normalize_work_hash(work_hash) != work_hash
            or commit is None
        ):
            raise ValueError(
                f"completed standalone work record is malformed: {work_hash}"
            )
        owner = record.get("owner")
        if not isinstance(owner, str) or not owner.startswith(owner_prefix):
            raise ValueError(
                f"completed standalone work owner is malformed: {work_hash}"
            )
        try:
            owner_rank = int(owner[len(owner_prefix):], 10)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError(
                f"completed standalone work owner is malformed: {work_hash}"
            ) from error
        if owner_rank < 0:
            raise ValueError(
                f"completed standalone work owner is malformed: {work_hash}"
            )
        analyzed += 1
        generated += int(commit["num_generated"])
        by_master[owner_rank] = by_master.get(owner_rank, 0) + 1
    return {
        "generated": generated,
        "analyzed": analyzed,
        "by_master": dict(sorted(by_master.items())),
    }


_CONTROL_TOKEN_LENGTH = 64


def _normalize_control_token(value: typing.Any) -> str:
    if not isinstance(value, str) or len(value) != _CONTROL_TOKEN_LENGTH:
        return ""
    if any(char not in "0123456789abcdef" for char in value):
        return ""
    return value


def _select_work_epoch(
    configured: typing.Any,
    *,
    random_bytes: typing.Callable[[int], bytes] = os.urandom,
) -> tuple[str, bool]:
    if configured is None or configured == "":
        token = random_bytes(32).hex()
        if not _normalize_control_token(token):
            raise ValueError("random work epoch source returned invalid bytes")
        return token, False
    token = _normalize_control_token(configured)
    if not token:
        raise ValueError(
            "SYMCC_STANDALONE_WORK_EPOCH must be 64 lowercase hex digits")
    return token, True


def _master_status_payload(
    rank: typing.Any,
    sequence: typing.Any,
    idle: typing.Any,
) -> dict[str, typing.Any] | None:
    if (isinstance(rank, bool) or not isinstance(rank, int) or rank < 0
            or isinstance(sequence, bool) or not isinstance(sequence, int)
            or sequence < 1 or not isinstance(idle, bool)):
        return None
    return {
        "schema": "symcc-master-status-v1",
        "rank": rank,
        "sequence": sequence,
        "idle": idle,
    }


class _MasterQuiescenceGate:
    """Root-side stable-idle detection followed by an exact two-phase probe."""

    def __init__(self, peers: typing.Iterable[int]) -> None:
        self.peers = tuple(sorted({int(peer) for peer in peers}))
        if any(peer <= 0 for peer in self.peers):
            raise ValueError("quiescence peers must be positive master ranks")
        self.status: dict[int, tuple[int, bool, float]] = {}
        self.idle_since: float | None = None
        self.probe_token = ""
        self.approved: set[int] = set()
        self.decision_sent = False
        self.committed: set[int] = set()
        self.cancelled_token = ""

    def cancel_probe(self) -> None:
        if self.probe_token and not self.decision_sent:
            self.cancelled_token = self.probe_token
        self.probe_token = ""
        self.approved.clear()
        self.decision_sent = False
        self.committed.clear()

    def take_abort_message(self) -> dict[str, typing.Any] | None:
        token = self.cancelled_token
        self.cancelled_token = ""
        if not token:
            return None
        return {
            "schema": "symcc-master-quiescence-abort-v1",
            "probe_token": token,
        }

    def observe_status(
        self,
        source: int,
        message: typing.Any,
        *,
        now: float,
    ) -> str:
        source = int(source)
        if source not in self.peers or not isinstance(message, dict):
            return "unowned"
        payload = _master_status_payload(
            message.get("rank"), message.get("sequence"), message.get("idle"))
        if (payload is None or payload["rank"] != source
                or message.get("schema") != payload["schema"]):
            return "malformed"
        previous = self.status.get(source)
        if previous is not None and payload["sequence"] <= previous[0]:
            return "stale"
        self.status[source] = (
            payload["sequence"], payload["idle"], float(now))
        if not payload["idle"]:
            self.idle_since = None
            if self.decision_sent:
                return "commit-conflict"
            self.cancel_probe()
        return "current"

    def globally_idle(
        self,
        local_idle: bool,
        *,
        now: float,
        freshness: float,
    ) -> bool:
        if not local_idle:
            return False
        for peer in self.peers:
            status = self.status.get(peer)
            if status is None or not status[1] or now - status[2] > freshness:
                return False
        return True

    def should_probe(
        self,
        local_idle: bool,
        *,
        now: float,
        idle_window: float,
        freshness: float,
    ) -> bool:
        if self.decision_sent:
            return False
        if not self.globally_idle(local_idle, now=now, freshness=freshness):
            self.idle_since = None
            self.cancel_probe()
            return False
        if self.idle_since is None:
            self.idle_since = now
        return (
            not self.probe_token
            and now - self.idle_since >= max(0.0, float(idle_window))
        )

    def begin_probe(self) -> dict[str, typing.Any]:
        if self.probe_token:
            raise RuntimeError("quiescence probe already active")
        self.probe_token = os.urandom(32).hex()
        self.approved.clear()
        self.decision_sent = False
        self.committed.clear()
        return {
            "schema": "symcc-master-quiescence-probe-v1",
            "probe_token": self.probe_token,
        }

    def observe_reply(self, source: int, message: typing.Any) -> str:
        source = int(source)
        if source not in self.peers or not self.probe_token:
            return "unowned"
        if (not isinstance(message, dict)
                or message.get("schema") !=
                "symcc-master-quiescence-reply-v1"
                or isinstance(message.get("rank"), bool)
                or not isinstance(message.get("rank"), int)
                or message.get("rank") != source
                or _normalize_control_token(message.get("probe_token")) !=
                self.probe_token
                or not isinstance(message.get("idle"), bool)):
            return "malformed"
        if not message["idle"]:
            self.idle_since = None
            self.cancel_probe()
            return "busy"
        self.approved.add(source)
        return "current"

    def ready_to_commit(
        self,
        local_idle: bool,
        *,
        now: float,
        freshness: float,
    ) -> bool:
        return bool(
            self.probe_token
            and not self.decision_sent
            and self.approved == set(self.peers)
            and self.globally_idle(local_idle, now=now, freshness=freshness)
        )

    def commit_message(self) -> dict[str, typing.Any]:
        if (not self.probe_token or self.decision_sent
                or self.approved != set(self.peers)):
            raise RuntimeError("quiescence probe is incomplete")
        self.decision_sent = True
        return {
            "schema": "symcc-master-quiescence-commit-v1",
            "probe_token": self.probe_token,
        }

    def observe_commit_ack(self, source: int, message: typing.Any) -> str:
        source = int(source)
        if (source not in self.peers or not self.probe_token
                or not self.decision_sent):
            return "unowned"
        if (not isinstance(message, dict)
                or message.get("schema") !=
                "symcc-master-quiescence-ack-v1"
                or isinstance(message.get("rank"), bool)
                or not isinstance(message.get("rank"), int)
                or message.get("rank") != source
                or _normalize_control_token(message.get("probe_token")) !=
                self.probe_token
                or not isinstance(message.get("committed"), bool)):
            return "malformed"
        if not message["committed"]:
            return "rejected"
        self.committed.add(source)
        return "current"

    def commit_acknowledged(self) -> bool:
        return bool(
            self.probe_token
            and self.decision_sent
            and self.committed == set(self.peers)
        )


def _quiescence_control_token(message: typing.Any, schema: str) -> str:
    if not isinstance(message, dict) or message.get("schema") != schema:
        return ""
    return _normalize_control_token(message.get("probe_token"))


def _master_stats_payload(
    generated: typing.Any,
    interesting: typing.Any,
    analyzed: typing.Any,
    token: str,
) -> dict[str, typing.Any] | None:
    values = (generated, interesting, analyzed)
    if any(isinstance(value, bool) or not isinstance(value, int)
           or value < 0 for value in values):
        return None
    if (not isinstance(token, str) or len(token) != 64
            or any(char not in "0123456789abcdef" for char in token)):
        return None
    return {
        "schema": "symcc-master-stats-v1",
        "stats_token": token,
        "generated": generated,
        "interesting": interesting,
        "analyzed": analyzed,
    }


def _bounded_master_stats_exchange(
    global_comm: typing.Any,
    *,
    rank: int,
    is_root: bool,
    peer_masters: typing.Iterable[int],
    generated: int,
    interesting: int,
    analyzed: int,
    pending_sends: list[typing.Any],
    timeout: float,
    monotonic: typing.Callable[[], float] = time.monotonic,
    sleep: typing.Callable[[float], None] = time.sleep,
) -> dict[str, typing.Any]:
    """Exchange final multi-master statistics with exact ACK and a deadline."""
    peers = tuple(sorted({int(peer) for peer in peer_masters
                          if int(peer) != int(rank)}))
    started = monotonic()
    timeout = _bounded_mpi_timeout(timeout, timeout)
    deadline = started + timeout
    requests = list(pending_sends)
    pending_sends.clear()
    communication_errors = 0
    quarantined = 0
    local_stats = {
        "generated": int(generated),
        "interesting": int(interesting),
        "analyzed": int(analyzed),
    }

    if not peers:
        return {
            "clean": True,
            "generated": int(generated),
            "interesting": int(interesting),
            "analyzed": int(analyzed),
            "received": (),
            "pending": (),
            "quarantined": 0,
            "communication_errors": 0,
            "elapsed": 0.0,
            "by_master": {int(rank): local_stats},
        }

    if not is_root:
        token = os.urandom(32).hex()
        payload = _master_stats_payload(
            generated, interesting, analyzed, token)
        if payload is None:
            raise RuntimeError("invalid local master statistics")
        try:
            requests.append(global_comm.isend(
                payload, dest=0, tag=TAG_MASTER_STATS))
        except (AttributeError, MPI.Exception, OSError, RuntimeError):
            communication_errors += 1
        acknowledged = False
        first_poll = True
        while first_poll or monotonic() < deadline:
            first_poll = False
            try:
                for peer in peers:
                    while global_comm.iprobe(
                            source=peer, tag=TAG_MASTER_STATUS):
                        global_comm.recv(
                            source=peer, tag=TAG_MASTER_STATUS)
                for tag in (TAG_MASTER_PROBE, TAG_MASTER_QUIESCE):
                    while global_comm.iprobe(source=0, tag=tag):
                        global_comm.recv(source=0, tag=tag)
                while global_comm.iprobe(source=0, tag=TAG_MASTER_STATS_ACK):
                    ack = global_comm.recv(
                        source=0, tag=TAG_MASTER_STATS_ACK)
                    if (isinstance(ack, dict)
                            and ack.get("schema") ==
                            "symcc-master-stats-ack-v1"
                            and ack.get("stats_token") == token
                            and not isinstance(ack.get("rank"), bool)
                            and isinstance(ack.get("rank"), int)
                            and ack.get("rank") == int(rank)):
                        acknowledged = True
                    else:
                        quarantined += 1
            except (MPI.Exception, OSError, RuntimeError):
                communication_errors += 1

            incomplete: list[typing.Any] = []
            for request in requests:
                try:
                    if not _request_completed(request):
                        incomplete.append(request)
                except (MPI.Exception, OSError, RuntimeError):
                    communication_errors += 1
            requests = incomplete
            if acknowledged and not requests:
                break
            remaining = deadline - monotonic()
            if remaining <= 0.0:
                break
            sleep(min(0.01, remaining))
        return {
            "clean": (
                acknowledged and not requests
                and communication_errors == 0
            ),
            "generated": int(generated),
            "interesting": int(interesting),
            "analyzed": int(analyzed),
            "received": (),
            "pending": (() if acknowledged else (0,)),
            "quarantined": quarantined,
            "communication_errors": communication_errors,
            "elapsed": max(0.0, monotonic() - started),
            "by_master": {int(rank): local_stats},
        }

    expected = set(peers)
    received: dict[int, dict[str, typing.Any]] = {}
    ack_requests: list[typing.Any] = []
    acks_started = False
    first_poll = True
    while first_poll or monotonic() < deadline:
        first_poll = False
        for peer in peers:
            try:
                for tag in (
                    TAG_MASTER_STATUS,
                    TAG_MASTER_PROBE_REPLY,
                    TAG_MASTER_QUIESCE_ACK,
                ):
                    while global_comm.iprobe(source=peer, tag=tag):
                        global_comm.recv(source=peer, tag=tag)
                while global_comm.iprobe(source=peer, tag=TAG_MASTER_STATS):
                    message = global_comm.recv(
                        source=peer, tag=TAG_MASTER_STATS)
                    if not isinstance(message, dict):
                        quarantined += 1
                        continue
                    payload = _master_stats_payload(
                        message.get("generated"),
                        message.get("interesting"),
                        message.get("analyzed"),
                        message.get("stats_token"),
                    )
                    if (payload is None
                            or message.get("schema") !=
                            "symcc-master-stats-v1"):
                        quarantined += 1
                        continue
                    if peer in received:
                        quarantined += 1
                        continue
                    received[peer] = payload
            except (MPI.Exception, OSError, RuntimeError):
                communication_errors += 1

        incomplete = []
        for request in requests:
            try:
                if not _request_completed(request):
                    incomplete.append(request)
            except (MPI.Exception, OSError, RuntimeError):
                communication_errors += 1
        requests = incomplete

        if expected == set(received) and not requests and not acks_started:
            acks_started = True
            for peer in peers:
                try:
                    ack_requests.append(global_comm.isend(
                        {
                            "schema": "symcc-master-stats-ack-v1",
                            "rank": peer,
                            "stats_token": received[peer]["stats_token"],
                        },
                        dest=peer,
                        tag=TAG_MASTER_STATS_ACK,
                    ))
                except (AttributeError, MPI.Exception, OSError, RuntimeError):
                    communication_errors += 1

        incomplete_acks = []
        for request in ack_requests:
            try:
                if not _request_completed(request):
                    incomplete_acks.append(request)
            except (MPI.Exception, OSError, RuntimeError):
                communication_errors += 1
        ack_requests = incomplete_acks
        if acks_started and not ack_requests:
            break
        remaining = deadline - monotonic()
        if remaining <= 0.0:
            break
        sleep(min(0.01, remaining))

    clean = (
        acks_started
        and not ack_requests
        and expected == set(received)
        and communication_errors == 0
    )
    by_master = {int(rank): local_stats}
    by_master.update({
        peer: {
            "generated": int(payload["generated"]),
            "interesting": int(payload["interesting"]),
            "analyzed": int(payload["analyzed"]),
        }
        for peer, payload in received.items()
    })
    return {
        "clean": clean,
        "generated": int(generated) + sum(
            payload["generated"] for payload in received.values()),
        "interesting": int(interesting) + sum(
            payload["interesting"] for payload in received.values()),
        "analyzed": int(analyzed) + sum(
            payload["analyzed"] for payload in received.values()),
        "received": tuple(sorted(received)),
        "pending": tuple(sorted(expected - set(received))),
        "quarantined": quarantined,
        "communication_errors": communication_errors,
        "elapsed": max(0.0, monotonic() - started),
        "by_master": by_master,
    }


def _discover_result_files(
    output_dir: str,
    *,
    max_objects: int,
    max_bytes: int,
) -> tuple[str, ...]:
    """Discover a flat regular-file result set within hard admission bounds."""
    if (isinstance(max_objects, bool) or not isinstance(max_objects, int)
            or max_objects < 1):
        raise ValueError("result object limit must be positive")
    if (isinstance(max_bytes, bool) or not isinstance(max_bytes, int)
            or max_bytes < 1):
        raise ValueError("result byte limit must be positive")
    paths: list[str] = []
    total_bytes = 0
    try:
        with os.scandir(output_dir) as entries:
            for entry in entries:
                # Constraint hints accompany a testcase and are consumed by
                # the hybrid AFL bridge.  They are not executable corpus
                # objects and must never be scheduled as standalone inputs.
                if entry.name.endswith(".hints"):
                    continue
                observed_objects = len(paths) + 1
                if observed_objects > max_objects:
                    raise _ResultBudgetExceeded(
                        "objects", observed_objects, max_objects)
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except OSError as error:
                    raise ValueError(
                        f"cannot inspect result entry {entry.name!r}"
                    ) from error
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError(
                        f"result entry is not a regular file: {entry.name!r}")
                total_bytes += metadata.st_size
                if total_bytes > max_bytes:
                    raise _ResultBudgetExceeded(
                        "bytes", total_bytes, max_bytes)
                paths.append(entry.path)
    except FileNotFoundError:
        return ()
    return tuple(paths)


def _stream_copy_regular_snapshot(
    source: str,
    temporary: str,
    *,
    byte_limit: int,
    expected_hash: str = "",
    expected_identity: _RegularFileIdentity | None = None,
) -> tuple[str, int, _RegularFileIdentity]:
    """Copy and hash one stable path-bound regular inode in fixed chunks."""
    if (isinstance(byte_limit, bool) or not isinstance(byte_limit, int)
            or byte_limit < 0):
        raise ValueError("stream-copy byte limit must be non-negative")
    normalized_hash = _normalize_work_hash(expected_hash) if expected_hash else ""
    if expected_hash and not normalized_hash:
        raise ValueError("invalid expected stream-copy digest")
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise OSError(
            errno.EOPNOTSUPP,
            "no-follow regular-file streaming is unavailable",
            source,
        )
    source_flags = (
        os.O_RDONLY
        | no_follow
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    output_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    output_flags |= getattr(os, "O_CLOEXEC", 0)
    source_descriptor = -1
    output_descriptor = -1
    digest = hashlib.sha256()
    total = 0
    try:
        source_descriptor = os.open(source, source_flags)
        metadata_before = os.fstat(source_descriptor)
        if not stat.S_ISREG(metadata_before.st_mode):
            raise ValueError("stream-copy source is not a regular file")
        identity = _regular_file_identity(metadata_before)
        if expected_identity is not None and identity != expected_identity:
            raise _SourceSnapshotChanged(
                errno.EAGAIN, "stream-copy source changed before copy", source)
        if identity.size > byte_limit:
            raise _ResultBudgetExceeded("bytes", identity.size, byte_limit)
        output_descriptor = os.open(temporary, output_flags, 0o600)
        while chunk := os.read(
                source_descriptor, _RESULT_STREAM_CHUNK_BYTES):
            total += len(chunk)
            if total > byte_limit:
                raise _ResultBudgetExceeded("bytes", total, byte_limit)
            digest.update(chunk)
            _write_all_descriptor(output_descriptor, chunk)
        metadata_after = os.fstat(source_descriptor)
        path_metadata = os.stat(source, follow_symlinks=False)
        if (not stat.S_ISREG(path_metadata.st_mode)
                or _regular_file_identity(metadata_after) != identity
                or _regular_file_identity(path_metadata) != identity
                or total != identity.size):
            raise _SourceSnapshotChanged(
                errno.EAGAIN, "stream-copy source changed during copy", source)
        work_hash = digest.hexdigest()
        if normalized_hash and work_hash != normalized_hash:
            raise ValueError(
                f"stream-copy digest mismatch: expected={normalized_hash}, "
                f"observed={work_hash}"
            )
        os.fsync(output_descriptor)
        os.close(output_descriptor)
        output_descriptor = -1
        return work_hash, total, identity
    finally:
        for descriptor in (output_descriptor, source_descriptor):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _stream_stage_result_file(
    source: str,
    stage_dir: str,
    *,
    byte_limit: int,
) -> tuple[str, int]:
    """Copy one no-follow regular result into a content-addressed object."""
    if (isinstance(byte_limit, bool) or not isinstance(byte_limit, int)
            or byte_limit < 0):
        raise ValueError("result staging byte limit must be non-negative")
    temporary = os.path.join(
        stage_dir,
        f".result.{os.getpid()}.{time.monotonic_ns()}."
        f"{os.urandom(8).hex()}.tmp",
    )
    try:
        work_hash, total, _identity = _stream_copy_regular_snapshot(
            source,
            temporary,
            byte_limit=byte_limit,
        )
        destination = os.path.join(stage_dir, work_hash)
        durable_replace(temporary, destination)
        verified = _regular_file_sha256_size(destination)
        if verified != (work_hash, total):
            raise ValueError(
                f"staged output digest or size mismatch for {work_hash}")
        return work_hash, total
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def _input_file_snapshot(
    path: str,
    *,
    max_bytes: int,
) -> tuple[str, int, _RegularFileIdentity] | None:
    """Return a stable bounded input snapshot or an explicit input overflow."""
    try:
        return _regular_file_sha256_snapshot(path, byte_limit=max_bytes)
    except _ResultBudgetExceeded as error:
        raise _InputBudgetExceeded(error.observed, error.limit) from error


def _stream_publish_input_file(
    source: str,
    shared_dir: str,
    *,
    expected_hash: str,
    expected_identity: _RegularFileIdentity,
    max_bytes: int,
) -> tuple[str, int]:
    """Publish a previously hashed stable input without whole-file bytes."""
    normalized = _normalize_work_hash(expected_hash)
    if not normalized:
        raise ValueError("invalid expected input digest")
    publication_mode = _shared_corpus_file_mode()
    temporary = os.path.join(
        shared_dir,
        f".input.{os.getpid()}.{time.monotonic_ns()}."
        f"{os.urandom(8).hex()}.tmp",
    )
    try:
        try:
            work_hash, total, _identity = _stream_copy_regular_snapshot(
                source,
                temporary,
                byte_limit=max_bytes,
                expected_hash=normalized,
                expected_identity=expected_identity,
            )
        except _ResultBudgetExceeded as error:
            raise _InputBudgetExceeded(
                error.observed, error.limit) from error
        destination = os.path.join(shared_dir, work_hash)
        if os.path.lexists(destination):
            existing = _input_file_snapshot(
                destination, max_bytes=max_bytes)
            verified = existing[:2] if existing is not None else None
            if verified != (work_hash, total):
                raise ValueError(
                    f"existing input object is invalid for {work_hash}")
            _set_durable_regular_file_mode(destination, publication_mode)
        else:
            _set_durable_regular_file_mode(temporary, publication_mode)
            durable_replace(temporary, destination)
            published = _input_file_snapshot(
                destination, max_bytes=max_bytes)
            verified = published[:2] if published is not None else None
            if verified != (work_hash, total):
                raise ValueError(
                    f"published input digest or size mismatch for {work_hash}")
        return work_hash, total
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def _stream_copy_verified_input(
    source: str,
    destination: str,
    *,
    expected_hash: str,
    max_bytes: int,
) -> tuple[str, int]:
    """Copy one public input to a private worker path in one verified pass."""
    temporary = (
        f"{destination}.tmp.{os.getpid()}.{time.monotonic_ns()}."
        f"{os.urandom(8).hex()}"
    )
    try:
        try:
            work_hash, total, _identity = _stream_copy_regular_snapshot(
                source,
                temporary,
                byte_limit=max_bytes,
                expected_hash=expected_hash,
            )
        except _ResultBudgetExceeded as error:
            raise _InputBudgetExceeded(
                error.observed, error.limit) from error
        durable_replace(temporary, destination)
        return work_hash, total
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def _count_public_corpus_objects(
    shared_dir: str,
    external_hashes: typing.AbstractSet[str],
) -> _CorpusProvenanceCounts:
    """Stream exact public and present-external corpus cardinalities."""
    public = 0
    external = 0
    with os.scandir(shared_dir) as entries:
        for entry in entries:
            if not _normalize_work_hash(entry.name):
                continue
            metadata = entry.stat(follow_symlinks=False)
            if stat.S_ISREG(metadata.st_mode):
                public += 1
                if entry.name in external_hashes:
                    external += 1
    return _CorpusProvenanceCounts(public=public, external=external)


def _stage_worker_outputs(
    paths: typing.Iterable[str],
    work_state_dir: str,
    worker_global_rank: int,
    *,
    max_objects: int,
    max_bytes: int,
) -> tuple[tuple[str, ...], int, str]:
    """Stage one bounded worker result and return hashes, bytes, and identity."""
    if (isinstance(max_objects, bool) or not isinstance(max_objects, int)
            or max_objects < 1):
        raise ValueError("result object limit must be positive")
    if (isinstance(max_bytes, bool) or not isinstance(max_bytes, int)
            or max_bytes < 1):
        raise ValueError("result byte limit must be positive")
    bounded_paths = tuple(paths)
    if len(bounded_paths) > max_objects:
        raise _ResultBudgetExceeded(
            "objects", len(bounded_paths), max_objects)
    if not bounded_paths:
        return (), 0, ""
    staging_id = os.urandom(16).hex()
    stage_dir = _staging_directory(
        work_state_dir, worker_global_rank, staging_id)
    durable_makedirs(stage_dir, exist_ok=False)
    hashes: list[str] = []
    total_bytes = 0
    try:
        for source in bounded_paths:
            try:
                work_hash, size = _stream_stage_result_file(
                    source,
                    stage_dir,
                    byte_limit=max_bytes - total_bytes,
                )
            except _ResultBudgetExceeded as error:
                raise _ResultBudgetExceeded(
                    "bytes", total_bytes + error.observed, max_bytes
                ) from error
            hashes.append(work_hash)
            total_bytes += size
        return tuple(hashes), total_bytes, staging_id
    except _ResultBudgetExceeded as error:
        error.staging_id = staging_id
        try:
            _remove_staged_outputs(
                work_state_dir, worker_global_rank, staging_id)
        except OSError:
            # Preserve the admission failure and stage identity so the master
            # can retry cleanup instead of losing the actionable result.
            pass
        raise
    except BaseException:
        try:
            _remove_staged_outputs(
                work_state_dir, worker_global_rank, staging_id)
        except OSError:
            pass
        raise


def _simulation_mutation_plans(
    input_size: int,
    count: int,
    rng: typing.Any,
) -> tuple[tuple[tuple[int, int], ...], ...]:
    """Draw legacy-compatible mutation plans without copying input bytes."""
    plans = []
    for _ in range(count):
        mutation_count = rng.randint(1, min(3, input_size))
        plans.append(tuple(
            (rng.randint(0, input_size - 1), rng.randint(0, 255))
            for _ in range(mutation_count)
        ))
    return tuple(plans)


def _simulate_mutations(
    input_file: str,
    output_dir: str,
    num_mutations: int = 5,
    *,
    max_objects: int = _DEFAULT_RESULT_MAX_OBJECTS,
    max_bytes: int = _DEFAULT_RESULT_MAX_BYTES,
    _rng: typing.Any = random,
) -> list[str]:
    """Generate bounded-memory synthetic outputs for framework benchmarks.

    The random draw order and byte-update semantics match the former whole-file
    implementation.  Outputs are streamed in descriptor-bounded batches and
    remain private temporary files until every source pass has succeeded.
    """
    if (isinstance(num_mutations, bool)
            or not isinstance(num_mutations, int)
            or num_mutations < 0):
        raise ValueError("simulation mutation count must be non-negative")
    if (isinstance(max_objects, bool) or not isinstance(max_objects, int)
            or max_objects < 1):
        raise ValueError("simulation object limit must be positive")
    if (isinstance(max_bytes, bool) or not isinstance(max_bytes, int)
            or max_bytes < 1):
        raise ValueError("simulation byte limit must be positive")
    if num_mutations == 0:
        return []
    if num_mutations > max_objects:
        raise _ResultBudgetExceeded(
            "objects", num_mutations, max_objects)

    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        return []
    source_flags = (
        os.O_RDONLY
        | no_follow
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        source_descriptor = os.open(input_file, source_flags)
    except OSError:
        return []

    temporary_paths: list[str] = []
    published_paths: list[str] = []
    open_outputs: list[int] = []
    try:
        try:
            metadata_before = os.fstat(source_descriptor)
        except OSError:
            return []
        if not stat.S_ISREG(metadata_before.st_mode):
            return []
        identity = _regular_file_identity(metadata_before)
        if identity.size == 0:
            return []
        planned_bytes = identity.size * num_mutations
        if planned_bytes > max_bytes:
            raise _ResultBudgetExceeded(
                "bytes", planned_bytes, max_bytes)

        os.makedirs(output_dir, exist_ok=True)
        destinations = [
            os.path.join(output_dir, f"sim_{index:04d}")
            for index in range(num_mutations)
        ]
        for destination in destinations:
            if os.path.lexists(destination):
                raise FileExistsError(
                    errno.EEXIST,
                    "simulation output already exists",
                    destination,
                )

        output_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        output_flags |= getattr(os, "O_CLOEXEC", 0)
        for batch_start in range(
                0, num_mutations, _SIMULATION_OUTPUT_BATCH_SIZE):
            batch_stop = min(
                num_mutations,
                batch_start + _SIMULATION_OUTPUT_BATCH_SIZE,
            )
            plans = _simulation_mutation_plans(
                identity.size, batch_stop - batch_start, _rng)
            for index in range(batch_start, batch_stop):
                temporary = os.path.join(
                    output_dir,
                    f".sim.{index:04d}.{os.getpid()}."
                    f"{time.monotonic_ns()}.{os.urandom(8).hex()}.tmp",
                )
                descriptor = os.open(temporary, output_flags, 0o600)
                temporary_paths.append(temporary)
                open_outputs.append(descriptor)

            os.lseek(source_descriptor, 0, os.SEEK_SET)
            source_offset = 0
            while chunk := os.read(
                    source_descriptor, _RESULT_STREAM_CHUNK_BYTES):
                chunk_stop = source_offset + len(chunk)
                for descriptor, plan in zip(open_outputs, plans):
                    changes = tuple(
                        (position - source_offset, value)
                        for position, value in plan
                        if source_offset <= position < chunk_stop
                    )
                    if changes:
                        output = bytearray(chunk)
                        for position, value in changes:
                            output[position] = value
                        _write_all_descriptor(descriptor, output)
                    else:
                        _write_all_descriptor(descriptor, chunk)
                source_offset = chunk_stop
            if source_offset != identity.size:
                raise _SourceSnapshotChanged(
                    errno.EAGAIN,
                    "simulation source changed during streaming",
                    input_file,
                )
            while open_outputs:
                os.close(open_outputs.pop())

        metadata_after = os.fstat(source_descriptor)
        path_metadata = os.stat(input_file, follow_symlinks=False)
        if (not stat.S_ISREG(path_metadata.st_mode)
                or _regular_file_identity(metadata_after) != identity
                or _regular_file_identity(path_metadata) != identity):
            raise _SourceSnapshotChanged(
                errno.EAGAIN,
                "simulation source changed during streaming",
                input_file,
            )

        for temporary, destination in zip(temporary_paths, destinations):
            os.link(temporary, destination, follow_symlinks=False)
            published_paths.append(destination)
            os.unlink(temporary)
        return published_paths
    except BaseException:
        for path in reversed(published_paths):
            try:
                os.unlink(path)
            except OSError:
                pass
        raise
    finally:
        while open_outputs:
            try:
                os.close(open_outputs.pop())
            except OSError:
                pass
        for path in temporary_paths:
            try:
                os.unlink(path)
            except OSError:
                pass
        try:
            os.close(source_descriptor)
        except OSError:
            pass


def run_symcc(target_cmd: list[str], input_file: str, output_dir: str,
              timeout_sec: int, use_stdin: bool,
              base_env: "dict[str, str] | None" = None,
              simulate: bool = False,
              result_max_objects: int = _DEFAULT_RESULT_MAX_OBJECTS,
              result_max_bytes: int = _DEFAULT_RESULT_MAX_BYTES,
              ) -> "tuple[list[str], int, float]":
    """
    Run the SymCC-instrumented target on the given input.

    When simulate=True, runs the target normally but generates synthetic
    mutations if no SymCC output is produced (for gcc-compiled binaries).

    Returns:
        (list_of_new_testcase_paths, return_code, elapsed_seconds)
    """
    os.makedirs(output_dir, exist_ok=True)

    env = os.environ.copy() if base_env is None else dict(base_env)
    # 引擎抽象:据 SYMCC_ENGINE(默认 symcc)选 SymCC/SymSan 决定实际命令 + 环境 + 是否喂 stdin。
    _engine = get_engine()
    cmd, env, feed_stdin = _engine.wrap_run(
        target_cmd, input_file, output_dir, env, use_stdin, timeout_sec)

    start = time.monotonic()
    py_timeout = timeout_sec + 15  # Python-side backstop for hung processes
    try:
        if feed_stdin:
            with open(input_file, "rb") as inf:
                proc = subprocess.run(
                    cmd, stdin=inf, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, env=env,
                    timeout=py_timeout
                )
        else:
            proc = subprocess.run(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, env=env,
                timeout=py_timeout
            )
        retcode = proc.returncode
    except subprocess.TimeoutExpired:
        print(f"[Worker {MPI.COMM_WORLD.Get_rank()}] Python-side timeout "
              f"after {py_timeout}s", file=sys.stderr)
        retcode = -1
    except (OSError, subprocess.SubprocessError) as e:
        print(f"[Worker {MPI.COMM_WORLD.Get_rank()}] Error running SymCC: {e}",
              file=sys.stderr)
        retcode = -1

    elapsed = time.monotonic() - start

    new_tests = list(_discover_result_files(
        output_dir,
        max_objects=result_max_objects,
        max_bytes=result_max_bytes,
    ))

    # 模拟模式：如果目标二进制没有产生 SymCC 输出，生成随机变异
    if simulate and not new_tests:
        _simulate_mutations(
            input_file,
            output_dir,
            max_objects=result_max_objects,
            max_bytes=result_max_bytes,
        )
        new_tests = list(_discover_result_files(
            output_dir,
            max_objects=result_max_objects,
            max_bytes=result_max_bytes,
        ))

    return new_tests, retcode, elapsed


def master_loop(global_comm: "MPI.Intracomm", group_comm: "MPI.Intracomm",
                args: argparse.Namespace, peer_masters: list[int],
                is_root: bool, shared_dir: str, master_ranks: list[int],
                worker_groups: "dict[int, list[int]]", work_state_dir: str,
                work_epoch: str, resumed_epoch: bool,
                filesystem_capabilities: SharedFilesystemCapabilities | None = None,
                filesystem_qualification_comm: typing.Any | None = None,
                filesystem_qualification_timeout: float = 30.0,
                filesystem_requalification_interval: float = 0.0,
                filesystem_requalification_jitter: float = 0.0,
                ulfm_endpoint_hosts: typing.Mapping[int, str] | None = None,
                ulfm_session_id: str = "",
                ulfm_initial_master_rank: int | None = None,
                ulfm_initial_worker_ranks: typing.Sequence[int] | None = None,
                ulfm_transport_endpoint_ranks: typing.Mapping[int, int] | None = None,
                ulfm_recovery_comm: typing.Any | None = None,
                ulfm_generation: int = 0,
                campaign_started_at: float | None = None,
                campaign_clock: dict[str, float] | None = None,
                ) -> bool:
    """
    Master process main loop.

    Uses group_comm for worker communication (isolated per group via
    comm.Split) and global_comm for inter-master quiescence coordination.

    Workers stage test cases below the job-private state directory.  A master
    verifies and promotes them to shared_dir/{hash} only after fencing the
    exact parent result.  MPI messages carry identities and control records,
    not file contents.
    """
    rank = global_comm.Get_rank()
    num_workers = group_comm.Get_size() - 1  # subtract self
    result_max_objects = int(args.result_max_objects)
    result_max_bytes = int(args.result_max_bytes)
    input_max_bytes = int(args.input_max_bytes)

    if num_workers == 0:
        print(f"[Master {rank}] Error: no workers assigned.", file=sys.stderr)
        return False
    owned_global_workers = tuple(worker_groups.get(rank, ()))
    if len(owned_global_workers) != num_workers:
        print(
            f"[Master {rank}] Error: worker-group identity mismatch.",
            file=sys.stderr,
        )
        return False

    shutdown_grace_default = float(max(120, args.timeout * 4))
    shutdown_grace = _bounded_mpi_timeout(
        os.environ.get(
            "SYMCC_SHUTDOWN_GRACE_SEC",
            str(shutdown_grace_default),
        ),
        shutdown_grace_default,
    )

    multi_master = len(master_ranks) > 1
    work_lease_ttl = _environment_float(
        "SYMCC_STANDALONE_WORK_LEASE_TTL",
        float(max(120, args.timeout * 4)),
        1.0,
        86400.0,
    )
    work_lock_ttl = _environment_float(
        "SYMCC_STANDALONE_WORK_LOCK_TTL", 30.0, 1.0, 3600.0)
    work_lock_acquire_timeout = _environment_float(
        "SYMCC_STANDALONE_WORK_LOCK_ACQUIRE_TIMEOUT",
        min(30.0, max(1.0, shutdown_grace / 4.0)),
        0.001,
        3600.0,
    )
    work_scan_interval = _environment_float(
        "SYMCC_STANDALONE_WORK_SCAN_INTERVAL", 0.25, 0.01, 30.0)
    ulfm_failure_poll_interval = _environment_float(
        "SYMCC_ULFM_FAILURE_POLL_INTERVAL", 0.1, 0.01, 30.0
    )
    work_fs_probe = _environment_enabled("SYMCC_SHARED_STATE_FS_PROBE")
    work_fs_probe_timeout = _environment_float(
        "SYMCC_SHARED_STATE_FS_PROBE_TIMEOUT", 5.0, 0.001, 60.0)
    try:
        work_lease_shards = min(4096, max(1, int(os.environ.get(
            "SYMCC_STANDALONE_WORK_LEASE_SHARDS", "64"))))
    except (TypeError, ValueError, OverflowError):
        work_lease_shards = 64

    durable_makedirs(shared_dir)

    # The same WAL-like work table is used with one or many masters.  Besides
    # simplifying invariants, this makes a configured epoch genuinely
    # recoverable after a single-master process failure.
    try:
        work_coordinator = _SharedWorkCoordinator(
            work_state_dir,
            rank=rank,
            master_ranks=master_ranks,
            epoch=work_epoch,
            shard_count=work_lease_shards,
            lease_ttl=work_lease_ttl,
            lock_ttl=work_lock_ttl,
            lock_acquire_timeout=work_lock_acquire_timeout,
            verify_filesystem=work_fs_probe,
            filesystem_probe_timeout=work_fs_probe_timeout,
            publication_root=shared_dir,
            filesystem_capabilities=filesystem_capabilities,
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(
            f"[Master {rank}] Shared filesystem capability failure: {error}",
            file=sys.stderr,
            flush=True,
        )
        return False

    ulfm_coordinator: DurableUlfmCoordinator | None = None
    ulfm_worker_shards: dict[int, str] = {}
    ulfm_master_stable_rank = rank
    if ulfm_session_id:
        try:
            if ulfm_endpoint_hosts is None:
                raise UlfmRecoveryError("ULFM endpoint inventory is unavailable")
            initial_master_rank = (
                rank if ulfm_initial_master_rank is None else
                int(ulfm_initial_master_rank)
            )
            initial_worker_ranks = (
                owned_global_workers if ulfm_initial_worker_ranks is None else
                tuple(int(value) for value in ulfm_initial_worker_ranks)
            )
            if ulfm_transport_endpoint_ranks is not None:
                ulfm_master_stable_rank = int(
                    ulfm_transport_endpoint_ranks.get(0, rank)
                )
            ulfm_coordinator, ulfm_worker_shards = _initialize_ulfm_hot_path(
                run_id=f"standalone-{initial_master_rank}-{ulfm_session_id}",
                master_global_rank=initial_master_rank,
                worker_global_ranks=initial_worker_ranks,
                endpoint_hosts=ulfm_endpoint_hosts,
                store_root=_ulfm_hot_path_store_root(
                    work_state_dir,
                    initial_master_rank,
                    ulfm_generation,
                ),
                transport_endpoint_ranks=ulfm_transport_endpoint_ranks,
            )
        except (OSError, RuntimeError, ValueError) as error:
            print(
                f"[Master {rank}] ULFM hot-path setup failed: {error}",
                file=sys.stderr,
                flush=True,
            )
            return False

    analyzed_hashes: set[str] = set()
    pending_queue: deque[tuple[str, str]] = deque()
    active_workers: dict[int, tuple[str, str]] = {}
    active_ulfm_fences: dict[int, dict[str, typing.Any]] = {}
    availability = _WorkerAvailabilityGate(range(1, num_workers + 1))
    total_generated = 0
    total_interesting = 0
    analysis_observations = 0
    invalid_worker_results = 0
    stale_worker_results = 0
    recovered_work = 0
    replayed_commits = 0
    lost_work_leases = 0
    fatal_control_error = ""

    pending_sends: list[typing.Any] = []
    ulfm_failure_sentinels: dict[int, tuple[bytearray, typing.Any]] = {}
    if ulfm_recovery_comm is not None:
        ulfm_failure_sentinels = _start_ulfm_failure_sentinels(
            group_comm, range(1, num_workers + 1)
        )
    imported_files: dict[str, _RegularFileIdentity] = {}
    external_hashes: set[str] = set()
    observed_shared_hashes: set[str] = set()

    def set_control_error(message: str) -> None:
        nonlocal fatal_control_error
        if not fatal_control_error:
            fatal_control_error = str(message)

    def claim_hash(
        work_hash: typing.Any,
        *,
        origin: str,
        recovery: bool = False,
    ) -> bool:
        nonlocal total_interesting
        normalized = _normalize_work_hash(work_hash)
        if not normalized:
            return False
        if not recovery and normalized in analyzed_hashes:
            return False
        token = ""
        if work_coordinator is not None:
            try:
                token = (
                    work_coordinator.reclaim(normalized, origin=origin)
                    if recovery else
                    work_coordinator.claim_owned(normalized, origin=origin)
                ) or ""
            except OSError as error:
                set_control_error(f"work claim failed: {error}")
                return False
            if not token:
                return False
        analyzed_hashes.add(normalized)
        observed_shared_hashes.add(normalized)
        pending_queue.append((normalized, token))
        if origin == "generated" and not recovery:
            total_interesting += 1
        return True

    def import_inputs(src_dir: str) -> int:
        """Import seeds, but enqueue only the rendezvous owner of each hash."""
        count = 0
        if fatal_control_error or not os.path.isdir(src_dir):
            return count
        # os.scandir 免排序（去重按内容哈希，顺序无关）+ 先按名跳过已导入项再 stat：避免
        # 每秒对增长的输入目录（迭代 concolic 产物不断回灌）做 O(n log n) 的 listdir+sort。
        # 沿用 AflConfig._file_cache 已验证的扫描优化（其文档记录该模式曾致 34s/58s@15w）。
        try:
            with os.scandir(src_dir) as scan:
                for entry in scan:
                    fname = entry.name
                    try:
                        metadata = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if not stat.S_ISREG(metadata.st_mode):
                        continue
                    entry_identity = _regular_file_identity(metadata)
                    if imported_files.get(fname) == entry_identity:
                        continue
                    try:
                        snapshot = _input_file_snapshot(
                            entry.path, max_bytes=input_max_bytes)
                    except _InputBudgetExceeded as error:
                        set_control_error(str(error))
                        break
                    if snapshot is None:
                        # Do not remember an unstable/unreadable name: the
                        # next scan must be able to retry its admission.
                        continue
                    work_hash, _size, identity = snapshot
                    external_hashes.add(work_hash)
                    if (work_coordinator is not None
                            and work_coordinator.designated_owner(work_hash)
                            != rank):
                        imported_files[fname] = identity
                        continue
                    try:
                        _stream_publish_input_file(
                            entry.path,
                            shared_dir,
                            expected_hash=work_hash,
                            expected_identity=identity,
                            max_bytes=input_max_bytes,
                        )
                    except _SourceSnapshotChanged:
                        continue
                    except _InputBudgetExceeded as error:
                        set_control_error(str(error))
                        break
                    except (OSError, ValueError) as error:
                        set_control_error(
                            f"initial corpus publish failed: {error}")
                        break
                    imported_files[fname] = identity
                    if claim_hash(work_hash, origin="initial"):
                        count += 1
        except OSError:
            return count
        return count

    def scan_shared_corpus() -> int:
        """Discover files assigned to this master without broadcasting work."""
        if fatal_control_error or work_coordinator is None:
            return 0
        admitted = 0
        try:
            with os.scandir(shared_dir) as scan:
                for entry in scan:
                    work_hash = _normalize_work_hash(entry.name)
                    if (not work_hash or work_hash in observed_shared_hashes
                            or work_coordinator.designated_owner(work_hash)
                            != rank):
                        continue
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    try:
                        snapshot = _input_file_snapshot(
                            entry.path, max_bytes=input_max_bytes)
                    except _InputBudgetExceeded as error:
                        set_control_error(str(error))
                        break
                    observed_hash = snapshot[0] if snapshot is not None else ""
                    if observed_hash != work_hash:
                        set_control_error(
                            f"shared corpus digest mismatch for {work_hash}: "
                            f"observed={observed_hash or 'unreadable'}")
                        continue
                    origin = (
                        "initial" if work_hash in external_hashes
                        else "generated"
                    )
                    if claim_hash(work_hash, origin=origin):
                        admitted += 1
                    else:
                        observed_shared_hashes.add(work_hash)
        except OSError as error:
            set_control_error(f"shared corpus scan failed: {error}")
        return admitted

    def recover_expired_work(
        *,
        lease_ttl: float | None = None,
    ) -> int:
        nonlocal recovered_work
        if fatal_control_error or work_coordinator is None:
            return 0
        try:
            recovered = work_coordinator.recover_expired(
                limit=4096, lease_ttl=lease_ttl)
        except (OSError, ValueError) as error:
            set_control_error(f"work recovery failed: {error}")
            return 0
        admitted = 0
        for work_hash, origin in recovered:
            corpus_path = os.path.join(shared_dir, work_hash)
            try:
                snapshot = _input_file_snapshot(
                    corpus_path, max_bytes=input_max_bytes)
            except _InputBudgetExceeded as error:
                set_control_error(str(error))
                continue
            if snapshot is None:
                token = work_coordinator.tokens.get(work_hash, "")
                if token:
                    try:
                        released = work_coordinator.abandon(work_hash, token)
                    except OSError as error:
                        set_control_error(
                            f"missing work lease release failed: {error}")
                    else:
                        if not released:
                            set_control_error(
                                "missing work lease could not be released")
                continue
            observed_hash = snapshot[0]
            if observed_hash != work_hash:
                set_control_error(
                    f"recovered corpus digest mismatch for {work_hash}: "
                    f"observed={observed_hash or 'unreadable'}")
                continue
            token = work_coordinator.tokens.get(work_hash, "")
            if not token:
                continue
            analyzed_hashes.add(work_hash)
            observed_shared_hashes.add(work_hash)
            pending_queue.append((work_hash, token))
            recovered_work += 1
            admitted += 1
        return admitted

    def account_committed_result(
        new_hashes: typing.Iterable[str],
        num_generated: int,
    ) -> None:
        nonlocal total_generated, analysis_observations
        total_generated += num_generated
        for result_hash in new_hashes:
            if (work_coordinator is None
                    or work_coordinator.designated_owner(result_hash) == rank):
                claim_hash(result_hash, origin="generated")
        analysis_observations += 1

    def recover_committing_work() -> int:
        """Redo durable standalone corpus commits and finish them exactly once."""
        nonlocal replayed_commits
        if fatal_control_error or work_coordinator is None:
            return 0
        try:
            records = work_coordinator.recover_committing(limit=4096)
        except (OSError, ValueError) as error:
            set_control_error(f"commit manifest scan failed: {error}")
            return 0
        completed_count = 0
        for input_hash, lease_token, manifest in records:
            worker_global_rank = int(manifest["worker_global_rank"])
            staging_id = str(manifest["staging_id"])
            new_hashes = tuple(manifest["hashes"])
            num_generated = int(manifest["num_generated"])

            def publish_recovered(
                current_manifest: dict[str, typing.Any],
            ) -> None:
                if current_manifest != manifest:
                    raise ValueError(
                        f"committed output manifest changed for {input_hash}"
                    )
                if not _verify_replayable_outputs(
                    work_state_dir,
                    shared_dir,
                    worker_global_rank,
                    staging_id,
                    new_hashes,
                ):
                    raise ValueError(
                        "committed output manifest is not replayable for "
                        f"{input_hash}"
                    )
                _promote_staged_outputs(
                    work_state_dir,
                    shared_dir,
                    worker_global_rank,
                    staging_id,
                    new_hashes,
                )

            try:
                completion = work_coordinator.replay_commit(
                    input_hash,
                    lease_token,
                    publish_recovered,
                )
            except (OSError, ValueError) as error:
                set_control_error(f"committed output replay failed: {error}")
                continue
            if completion == "stale":
                set_control_error(
                    f"committed output token changed for {input_hash}")
                continue
            _remove_staged_outputs(
                work_state_dir, worker_global_rank, staging_id)
            if completion == "completed":
                account_committed_result(new_hashes, num_generated)
                replayed_commits += 1
                completed_count += 1
        return completed_count

    def refresh_quiescence_frontier() -> bool:
        """Take the local PREPARE boundary after every work source is fresh."""
        return _refresh_quiescence_frontier(
            (
                recover_committing_work,
                lambda: import_inputs(args.input_dir),
                scan_shared_corpus,
                recover_expired_work,
            ),
            lambda: (
                not fatal_control_error
                and not pending_queue
                and not active_workers
            ),
        )

    def consume_worker_result(
        worker_group_rank: int,
        result: typing.Any,
    ) -> bool:
        """Retire one owned dispatch and account only a valid result payload."""
        nonlocal invalid_worker_results, stale_worker_results
        worker_global_rank = (
            owned_global_workers[worker_group_rank - 1]
            if 1 <= worker_group_rank <= len(owned_global_workers) else -1
        )
        staging_id = (
            _normalize_staging_id(result.get("staging_id"))
            if isinstance(result, dict) else ""
        )
        expected_ulfm_fence = active_ulfm_fences.get(worker_group_rank)
        ulfm_retired = False
        if ulfm_coordinator is not None:
            raw_fence = result.get("ulfm_fence") if isinstance(result, dict) else None
            classification = (
                ulfm_coordinator.classify_result(raw_fence)
                if isinstance(raw_fence, typing.Mapping) else
                "malformed"
            )
            if (
                expected_ulfm_fence is None
                or classification != "current"
                or raw_fence != expected_ulfm_fence
            ):
                _remove_staged_outputs(
                    work_state_dir, worker_global_rank, staging_id)
                if classification == "stale":
                    stale_worker_results += 1
                else:
                    invalid_worker_results += 1
                    availability.quarantine(worker_group_rank)
                    set_control_error(
                        "malformed or unowned ULFM result fence from worker "
                        f"{worker_group_rank}"
                    )
                return False
        assignment = active_workers.pop(worker_group_rank, None)
        if assignment is None:
            _remove_staged_outputs(
                work_state_dir, worker_global_rank, staging_id)
            availability.quarantine(worker_group_rank)
            invalid_worker_results += 1
            active_ulfm_fences.pop(worker_group_rank, None)
            if ulfm_coordinator is not None and expected_ulfm_fence is not None:
                try:
                    ulfm_coordinator.cancel(
                        expected_ulfm_fence, "assignment-state-mismatch"
                    )
                except (OSError, RuntimeError, ValueError) as error:
                    set_control_error(
                        f"ULFM inconsistent assignment cancellation failed: {error}"
                    )
                else:
                    set_control_error(
                        "ULFM result fence had no matching active assignment"
                    )
            return False
        active_ulfm_fences.pop(worker_group_rank, None)
        input_hash, lease_token = assignment

        def retire_ulfm_failure(reason: str) -> None:
            if (
                ulfm_retired
                or ulfm_coordinator is None
                or expected_ulfm_fence is None
            ):
                return
            try:
                ulfm_coordinator.cancel(expected_ulfm_fence, reason)
            except (OSError, RuntimeError, ValueError) as error:
                set_control_error(f"ULFM work cancellation failed: {error}")

        def retire_ulfm_success(proof_sha256: str) -> bool:
            nonlocal ulfm_retired
            if ulfm_coordinator is None or expected_ulfm_fence is None:
                return True
            try:
                ulfm_coordinator.finish(expected_ulfm_fence, proof_sha256)
            except (OSError, RuntimeError, ValueError) as error:
                set_control_error(f"ULFM work completion failed: {error}")
                return False
            ulfm_retired = True
            return True
        input_violation = _worker_input_budget_violation(
            result,
            expected_input_hash=input_hash,
            max_bytes=input_max_bytes,
        )
        if input_violation is not None:
            _remove_staged_outputs(
                work_state_dir, worker_global_rank, staging_id)
            invalid_worker_results += 1
            set_control_error(str(input_violation))
            retire_ulfm_failure("input-budget-rejected")
            return False
        declared_violation = _worker_result_budget_violation(
            result,
            expected_input_hash=input_hash,
            max_objects=result_max_objects,
            max_bytes=result_max_bytes,
        )
        if declared_violation is not None:
            _remove_staged_outputs(
                work_state_dir, worker_global_rank, staging_id)
            invalid_worker_results += 1
            set_control_error(str(declared_violation))
            retire_ulfm_failure("result-budget-rejected")
            return False
        try:
            payload = _worker_result_payload(
                result,
                expected_input_hash=input_hash,
                max_objects=result_max_objects,
                max_bytes=result_max_bytes,
            )
            if payload is not None and not _verify_staged_outputs(
                    work_state_dir,
                    worker_global_rank,
                    staging_id,
                    payload[0],
                    declared_bytes=payload[2],
                    max_objects=result_max_objects,
                    max_bytes=result_max_bytes):
                payload = None
        except _ResultBudgetExceeded as error:
            _remove_staged_outputs(
                work_state_dir, worker_global_rank, staging_id)
            invalid_worker_results += 1
            set_control_error(str(error))
            retire_ulfm_failure("result-budget-overflow")
            return False
        if payload is None:
            _remove_staged_outputs(
                work_state_dir, worker_global_rank, staging_id)
            invalid_worker_results += 1
            try:
                corpus_snapshot = _input_file_snapshot(
                    os.path.join(shared_dir, input_hash),
                    max_bytes=input_max_bytes,
                )
            except _InputBudgetExceeded as error:
                set_control_error(str(error))
                retire_ulfm_failure("retry-input-budget-rejected")
                return False
            corpus_hash = (
                corpus_snapshot[0] if corpus_snapshot is not None else ""
            )
            if corpus_hash != input_hash:
                set_control_error(
                    f"active corpus digest mismatch for {input_hash}: "
                    f"observed={corpus_hash or 'unreadable'}")
                retire_ulfm_failure("active-corpus-mismatch")
                return False
            current_token = (
                work_coordinator.tokens.get(input_hash, "")
                if work_coordinator is not None else ""
            )
            recovery = _requeue_failed_assignment(
                pending_queue,
                assignment,
                availability,
                worker_group_rank,
                current_lease_token=current_token,
            )
            if recovery == "stale":
                stale_worker_results += 1
            elif recovery == "exhausted":
                set_control_error(
                    "all workers were quarantined while retrying exact work")
            retire_ulfm_failure("worker-result-rejected")
            return False
        new_hashes, num_generated, _staged_bytes = payload
        commit_manifest = _standalone_commit_manifest(
            worker_global_rank,
            staging_id,
            new_hashes,
            num_generated,
        )
        if commit_manifest is None:
            _remove_staged_outputs(
                work_state_dir, worker_global_rank, staging_id)
            set_control_error("validated result could not form a commit manifest")
            retire_ulfm_failure("commit-manifest-rejected")
            return False
        if work_coordinator is not None:
            try:
                current = work_coordinator.begin_commit(
                    input_hash, lease_token, commit_manifest)
            except OSError as error:
                set_control_error(f"work commit fence failed: {error}")
                retire_ulfm_failure("work-commit-fence-failed")
                return False
            if not current:
                _remove_staged_outputs(
                    work_state_dir, worker_global_rank, staging_id)
                stale_worker_results += 1
                retire_ulfm_failure("standalone-work-fence-stale")
                return True
        # The WAL commit manifest is now durable and sufficient to replay all
        # publication side effects.  Retire the ULFM lease here so a master
        # failure cannot leave a completed WAL record queued forever in the
        # independent recovery controller.
        if not retire_ulfm_success(ulfm_content_digest(commit_manifest)):
            return False
        committed_hashes = tuple(commit_manifest["hashes"])

        def publish_current(current_manifest: dict[str, typing.Any]) -> None:
            if current_manifest != commit_manifest:
                raise ValueError(
                    f"committed output manifest changed for {input_hash}"
                )
            _promote_staged_outputs(
                work_state_dir,
                shared_dir,
                worker_global_rank,
                staging_id,
                committed_hashes,
            )

        completion = "completed"
        try:
            if work_coordinator is None:
                publish_current(commit_manifest)
            else:
                completion = work_coordinator.replay_commit(
                    input_hash,
                    lease_token,
                    publish_current,
                )
        except (OSError, ValueError) as error:
            if work_coordinator is None:
                _remove_staged_outputs(
                    work_state_dir, worker_global_rank, staging_id)
            set_control_error(f"staged corpus promotion failed: {error}")
            retire_ulfm_failure("corpus-promotion-failed")
            return False
        if completion == "stale":
            set_control_error("current work lease could not complete")
            retire_ulfm_failure("standalone-work-completion-stale")
            return False
        _remove_staged_outputs(
            work_state_dir, worker_global_rank, staging_id)
        if completion == "completed":
            account_committed_result(committed_hashes, num_generated)
        return True

    now_at_start = time.monotonic()
    wall_start = (
        float(campaign_started_at)
        if (
            campaign_started_at is not None
            and not isinstance(campaign_started_at, bool)
            and isinstance(campaign_started_at, (int, float))
            and math.isfinite(float(campaign_started_at))
            and float(campaign_started_at) <= now_at_start
        ) else
        now_at_start
    )
    wall_timeout = args.wall_timeout
    idle_timeout = max(0.0, float(args.max_idle))
    idle_started: float | None = None
    last_idle_report = wall_start
    # 限流：主循环每 ~50ms 转一圈，若每圈都 sorted(os.listdir(input_dir)) 重扫整个
    # 输入目录（可能是很大的 AFL queue），是 O(n log n) 的热路径浪费。每 IMPORT_SCAN_
    # INTERVAL 秒才重扫一次即可（imported_files 已保证只处理新文件）。
    last_import_scan = wall_start
    IMPORT_SCAN_INTERVAL = 1.0
    last_shared_scan = 0.0
    last_ulfm_failure_poll = 0.0
    last_lease_heartbeat = wall_start
    lease_heartbeat_interval = max(
        0.1, min(30.0, work_lease_ttl / 3.0))
    status_interval = max(0.05, min(0.25, work_scan_interval / 2.0))
    status_freshness = max(1.0, status_interval * 10.0)
    last_status_send = 0.0
    status_sequence = 0
    last_local_idle: bool | None = None
    peer_last_seen = {peer: wall_start for peer in peer_masters}
    peer_status_sequence = {peer: 0 for peer in peer_masters}
    quiescence_gate = _MasterQuiescenceGate(peer_masters) \
        if is_root and multi_master else None
    accepted_probe_token = ""
    quarantined_control_messages = 0
    runtime_lock_renewal: ClusterLockRenewalController | None = None
    runtime_lock_metrics_reported = False
    if filesystem_qualification_comm is not None:
        try:
            if (
                not multi_master
                or work_coordinator.filesystem_capabilities is None
                or not work_coordinator.filesystem_capabilities.cluster_lock_verified
                or work_coordinator.filesystem_capabilities.probe_scope
                != "cross-host-mpi-lock-v2"
                or work_coordinator.filesystem_capabilities
                .cluster_lock_identity_checks != len(master_ranks)
                or filesystem_qualification_comm.Get_size()
                != len(master_ranks)
                or filesystem_qualification_comm.Get_rank()
                != master_ranks.index(rank)
            ):
                raise ValueError(
                    "runtime cluster lock communicator/capability mismatch")
            runtime_lock_renewal = ClusterLockRenewalController(
                epoch=work_epoch,
                interval=filesystem_requalification_interval,
                timeout=min(
                    float(filesystem_qualification_timeout),
                    work_lease_ttl / 3.0,
                ),
                completed_at=wall_start,
                jitter_fraction=filesystem_requalification_jitter,
                expected_master_ranks=master_ranks,
                expected_capability=work_coordinator.filesystem_capabilities,
            )
            if not runtime_lock_renewal.enabled:
                raise ValueError(
                    "runtime cluster lock renewal interval is disabled")
            renewal_config_fingerprint, renewal_config_error = (
                qualify_cluster_lock_renewal_configuration(
                    filesystem_qualification_comm,
                    runtime_lock_renewal,
                    timeout=filesystem_qualification_timeout,
                )
            )
            if renewal_config_error:
                raise ValueError(
                    "runtime cluster lock configuration consensus failed: "
                    f"{renewal_config_error}"
                )
            renewal_manifest = _ensure_runtime_lock_configuration_manifest(
                work_state_dir,
                runtime_lock_renewal,
            )
            if is_root:
                manifest_path = os.path.join(
                    work_state_dir,
                    _RUNTIME_LOCK_CONFIGURATION_MANIFEST,
                )
                print(
                    "[Master] Runtime lock renewal configuration consensus: "
                    f"{renewal_config_fingerprint}",
                    flush=True,
                )
                print(
                    "[Master] Durable runtime lock configuration: "
                    f"{renewal_manifest['fingerprint']} ({manifest_path})",
                    flush=True,
                )
        except (
            AttributeError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as error:
            set_control_error(f"runtime cluster lock setup failed: {error}")

    # Establish the control-plane configuration before any master performs
    # asymmetric seed import or recovery I/O.  A slow distributed-filesystem
    # client must not make an already-ready peer exhaust the configuration
    # exchange deadline before the slow peer has entered that exchange.
    if resumed_epoch:
        # The operator-selected epoch asserts that the previous MPI job is no
        # longer active.  Redo irreversible commits first, then take over all
        # remaining pre-commit leases once without waiting for their old TTL.
        recover_committing_work()
        recover_expired_work(lease_ttl=0.0)
    import_inputs(args.input_dir)
    if not resumed_epoch:
        recover_committing_work()
    if (
        filesystem_qualification_comm is not None
        and not fatal_control_error
        and not _bounded_mpi_barrier(
            filesystem_qualification_comm,
            filesystem_qualification_timeout,
        )
    ):
        set_control_error(
            "initial master data-plane synchronization timed out"
        )
    campaign_ready_at = time.monotonic()
    if (
        ulfm_generation == 0
        or (campaign_clock is not None and "started_at" not in campaign_clock)
    ):
        # Initial capability checks and seed publication are setup costs.  The
        # user-visible campaign budget starts only once every master can enter
        # the scheduling loop. A newly promoted master starts a local clock if
        # it did not participate as a master in the initial generation.
        wall_start = campaign_ready_at
        if campaign_clock is not None:
            campaign_clock["started_at"] = wall_start
            campaign_clock.pop("paused_at", None)
    elif campaign_clock is not None and "paused_at" in campaign_clock:
        paused_at = float(campaign_clock.pop("paused_at"))
        if math.isfinite(paused_at) and paused_at <= campaign_ready_at:
            wall_start += campaign_ready_at - paused_at
            campaign_clock["started_at"] = wall_start
    if runtime_lock_renewal is not None:
        runtime_lock_renewal.completed_at = time.monotonic()
    initial_seed_count = len(external_hashes)
    if is_root:
        total_masters = len(peer_masters) + 1
        print(f"[Master] Observed {initial_seed_count} initial inputs from "
              f"{args.input_dir}")
        print(f"[Master] Using {total_masters} master(s), "
              f"{num_workers} workers in this group")
        print(
            f"[Master] Durable work ownership: rendezvous + "
            f"{work_lease_shards} fenced shards, "
            f"lease={work_lease_ttl:g}s",
            flush=True,
        )
        if work_coordinator.filesystem_capabilities is not None:
            print(
                "[Master] Shared filesystem capabilities: "
                f"{work_coordinator.filesystem_capabilities.snapshot()}",
                flush=True,
            )
    # 进度汇报节流：每 worker 结果都 print 会让单核 master 在高 worker 数下 CPU 打满
    #（f-string 格式化 + stdout I/O 成为热路径）。改为周期性汇总，显著抬高单 master 可
    # 喂饱的 worker 上限。
    last_report_time = wall_start
    REPORT_INTERVAL = 2.0
    reported_generated = 0

    shutdown_requested = False

    def _signal_handler(signum: int, frame: object) -> None:
        nonlocal shutdown_requested
        shutdown_requested = True
        print(f"\n[Master {rank}] Received signal {signum}, shutting down...",
              file=sys.stderr, flush=True)

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    def retire_pending_sends() -> None:
        incomplete: list[typing.Any] = []
        for request in pending_sends:
            try:
                if not _request_completed(request):
                    incomplete.append(request)
            except (MPI.Exception, OSError, RuntimeError) as error:
                set_control_error(f"master control send failed: {error}")
        pending_sends[:] = incomplete

    def publish_status(local_idle: bool, *, force: bool = False) -> None:
        nonlocal status_sequence, last_status_send, last_local_idle
        if not multi_master:
            return
        now = time.monotonic()
        if (not force and local_idle == last_local_idle
                and now - last_status_send < status_interval):
            return
        status_sequence += 1
        payload = _master_status_payload(rank, status_sequence, local_idle)
        assert payload is not None
        for peer in peer_masters:
            try:
                pending_sends.append(global_comm.isend(
                    payload, dest=peer, tag=TAG_MASTER_STATUS))
            except (AttributeError, MPI.Exception, OSError, RuntimeError) as error:
                set_control_error(f"master status send failed: {error}")
        last_status_send = now
        last_local_idle = local_idle

    def drain_master_status(now: float) -> None:
        nonlocal quarantined_control_messages
        if not multi_master:
            return
        while global_comm.iprobe(
                source=MPI.ANY_SOURCE, tag=TAG_MASTER_STATUS):
            status = MPI.Status()
            message = global_comm.recv(
                source=MPI.ANY_SOURCE,
                tag=TAG_MASTER_STATUS,
                status=status,
            )
            source = status.Get_source()
            if quiescence_gate is not None:
                classification = quiescence_gate.observe_status(
                    source, message, now=now)
            else:
                payload = message if isinstance(message, dict) else {}
                normalized = _master_status_payload(
                    payload.get("rank"),
                    payload.get("sequence"),
                    payload.get("idle"),
                )
                if (normalized is None or normalized["rank"] != source
                        or payload.get("schema") != normalized["schema"]):
                    classification = "malformed"
                elif normalized["sequence"] <= peer_status_sequence.get(
                        source, 0):
                    classification = "stale"
                else:
                    peer_status_sequence[source] = normalized["sequence"]
                    classification = "current"
            if classification == "current":
                peer_last_seen[source] = now
            elif classification == "commit-conflict":
                peer_last_seen[source] = now
                set_control_error(
                    f"master {source} became busy after quiescence commit")
            else:
                quarantined_control_messages += 1

    def report_runtime_lock_metrics() -> None:
        nonlocal runtime_lock_metrics_reported
        if runtime_lock_renewal is None or runtime_lock_metrics_reported:
            return
        print(
            f"[Master {rank}] Runtime cluster lock renewal: "
            f"{runtime_lock_renewal.snapshot()}",
            flush=True,
        )
        runtime_lock_metrics_reported = True

    def execute_runtime_lock_renewal(
        generation: int,
        *,
        observed_at: float,
    ) -> bool:
        nonlocal last_lease_heartbeat, lost_work_leases
        assert runtime_lock_renewal is not None
        assert filesystem_qualification_comm is not None
        heartbeat = observe_work_lease_heartbeat(
            work_coordinator.heartbeat_all)
        lost_work_leases += heartbeat.lost_lease_count
        last_lease_heartbeat = observed_at
        observation = observe_cluster_lock_qualification_inputs(
            work_coordinator.filesystem_capabilities,
            processor_name_probe=MPI.Get_processor_name,
            local_error=(
                f"pre-renewal {heartbeat.error}" if heartbeat.error else ""
            ),
        )
        if heartbeat.error:
            set_control_error(observation.error)
        result = qualify_mpi_cluster_advisory_lock(
            filesystem_qualification_comm,
            observation.capability,
            root=work_state_dir,
            epoch=work_epoch,
            global_rank=rank,
            expected_master_ranks=master_ranks,
            processor_name=observation.processor_name,
            qualification_generation=generation,
            timeout=runtime_lock_renewal.timeout,
            local_error=observation.error,
        )
        successful, accounting_error = complete_cluster_lock_renewal(
            runtime_lock_renewal,
            generation,
            result,
            monotonic=time.monotonic,
        )
        if accounting_error:
            report_runtime_lock_metrics()
            set_control_error(
                "runtime cluster lock renewal accounting failed: "
                f"{accounting_error}"
            )
            return False
        if not successful:
            detail = result.error or (
                "cross-host lock evidence is no longer verified")
            report_runtime_lock_metrics()
            set_control_error(
                f"runtime cluster lock renewal {generation} failed: {detail}")
            return False
        assert result.capability is not None
        work_coordinator.filesystem_capabilities = result.capability
        if is_root:
            print(
                "[Master] Cluster lock proof transcript generation "
                f"{generation}: {result.proof_transcript}",
                flush=True,
            )
        return True

    def handle_runtime_lock_renewal(
        now: float,
        *,
        safe_to_start: bool,
        wall_remaining: float | None,
    ) -> bool:
        nonlocal quarantined_control_messages
        if runtime_lock_renewal is None or fatal_control_error:
            return False
        assert filesystem_qualification_comm is not None

        if is_root:
            if not runtime_lock_renewal.due(
                now,
                safe_to_start=safe_to_start,
                wall_remaining=wall_remaining,
            ):
                return False
            generation, sends, send_error = begin_cluster_lock_renewal(
                filesystem_qualification_comm,
                runtime_lock_renewal,
            )
            if generation == 0:
                set_control_error(
                    "runtime cluster lock renewal request failed: "
                    f"{send_error or 'unknown control error'}"
                )
                return False
            # Enter the bounded litmus even after a partial send failure. Peers
            # that received the request must not be stranded in the protocol.
            execute_runtime_lock_renewal(generation, observed_at=now)
            delivery = observe_cluster_lock_renewal_delivery(sends)
            delivery_failures = "; ".join(filter(None, (
                send_error,
                delivery.error,
                (
                    f"{delivery.incomplete_count} incomplete send(s)"
                    if delivery.incomplete_count else ""
                ),
            )))[:512]
            if delivery_failures:
                set_control_error(
                    "runtime cluster lock renewal request delivery failed: "
                    f"{delivery_failures}"
                )
            return True

        if not safe_to_start:
            return False
        generation, error, observed = poll_cluster_lock_renewal(
            filesystem_qualification_comm,
            runtime_lock_renewal,
        )
        if error and not observed:
            set_control_error(error)
            return False
        if not observed:
            return False
        if error:
            quarantined_control_messages += 1
            return False
        execute_runtime_lock_renewal(generation, observed_at=now)
        return True

    def handle_quiescence_control(local_idle: bool, now: float) -> bool:
        nonlocal accepted_probe_token, quarantined_control_messages
        if not multi_master:
            return False
        if is_root:
            assert quiescence_gate is not None
            while global_comm.iprobe(
                    source=MPI.ANY_SOURCE, tag=TAG_MASTER_QUIESCE_ACK):
                status = MPI.Status()
                message = global_comm.recv(
                    source=MPI.ANY_SOURCE,
                    tag=TAG_MASTER_QUIESCE_ACK,
                    status=status,
                )
                classification = quiescence_gate.observe_commit_ack(
                    status.Get_source(), message)
                if classification == "rejected":
                    set_control_error(
                        f"master {status.Get_source()} rejected quiescence "
                        "commit")
                elif classification != "current":
                    quarantined_control_messages += 1
            while global_comm.iprobe(
                    source=MPI.ANY_SOURCE, tag=TAG_MASTER_PROBE_REPLY):
                status = MPI.Status()
                message = global_comm.recv(
                    source=MPI.ANY_SOURCE,
                    tag=TAG_MASTER_PROBE_REPLY,
                    status=status,
                )
                classification = quiescence_gate.observe_reply(
                    status.Get_source(), message)
                if classification not in {"current", "busy"}:
                    quarantined_control_messages += 1
            abort = quiescence_gate.take_abort_message()
            if abort is not None:
                for peer in peer_masters:
                    try:
                        pending_sends.append(global_comm.isend(
                            abort, dest=peer, tag=TAG_MASTER_QUIESCE))
                    except (AttributeError, MPI.Exception, OSError,
                            RuntimeError) as error:
                        set_control_error(
                            f"master quiescence abort failed: {error}")
            if quiescence_gate.should_probe(
                    local_idle,
                    now=now,
                    idle_window=idle_timeout,
                    freshness=status_freshness):
                local_idle = refresh_quiescence_frontier()
                if local_idle:
                    probe = quiescence_gate.begin_probe()
                    for peer in peer_masters:
                        try:
                            pending_sends.append(global_comm.isend(
                                probe, dest=peer, tag=TAG_MASTER_PROBE))
                        except (AttributeError, MPI.Exception, OSError,
                                RuntimeError) as error:
                            set_control_error(
                                f"master quiescence probe failed: {error}")
                else:
                    # The root's final refresh found work. Publish BUSY now so
                    # peers do not keep an idle status across the next cycle.
                    quiescence_gate.should_probe(
                        False,
                        now=now,
                        idle_window=idle_timeout,
                        freshness=status_freshness,
                    )
                    publish_status(False, force=True)
            if quiescence_gate.ready_to_commit(
                    local_idle, now=now, freshness=status_freshness):
                commit = quiescence_gate.commit_message()
                for peer in peer_masters:
                    try:
                        pending_sends.append(global_comm.isend(
                            commit, dest=peer, tag=TAG_MASTER_QUIESCE))
                    except (AttributeError, MPI.Exception, OSError,
                            RuntimeError) as error:
                        set_control_error(
                            f"master quiescence commit failed: {error}")
                elapsed = max(
                    0.0,
                    now - (quiescence_gate.idle_since or now),
                )
                print(
                    f"[Master] Global quiescence commit proposed after "
                    f"{elapsed:.3f}s (limit {idle_timeout:g}s).",
                    flush=True,
                )
            if quiescence_gate.commit_acknowledged():
                print(
                    "[Master] Global quiescence committed by all masters.",
                    flush=True,
                )
                return not fatal_control_error
            return False

        while global_comm.iprobe(source=0, tag=TAG_MASTER_PROBE):
            message = global_comm.recv(source=0, tag=TAG_MASTER_PROBE)
            token = _quiescence_control_token(
                message, "symcc-master-quiescence-probe-v1")
            if not token:
                quarantined_control_messages += 1
                continue
            if accepted_probe_token:
                if token != accepted_probe_token:
                    quarantined_control_messages += 1
                    continue
                local_idle = not pending_queue and not active_workers
            else:
                # Re-scan before voting so a file published near the idle
                # boundary turns the vote into BUSY rather than being stranded
                # after commit. A YES vote freezes discovery until the exact
                # COMMIT/ABORT decision arrives.
                local_idle = refresh_quiescence_frontier()
                accepted_probe_token = token if local_idle else ""
            reply = {
                "schema": "symcc-master-quiescence-reply-v1",
                "rank": rank,
                "probe_token": token,
                "idle": local_idle,
            }
            try:
                pending_sends.append(global_comm.isend(
                    reply, dest=0, tag=TAG_MASTER_PROBE_REPLY))
            except (AttributeError, MPI.Exception, OSError,
                    RuntimeError) as error:
                set_control_error(f"master probe reply failed: {error}")
        if not local_idle and not accepted_probe_token:
            accepted_probe_token = ""
        while global_comm.iprobe(source=0, tag=TAG_MASTER_QUIESCE):
            message = global_comm.recv(source=0, tag=TAG_MASTER_QUIESCE)
            abort_token = _quiescence_control_token(
                message, "symcc-master-quiescence-abort-v1")
            if abort_token:
                if abort_token == accepted_probe_token:
                    accepted_probe_token = ""
                # ABORT is idempotent: peers that voted BUSY never entered the
                # prepared state, and a delayed prior decision is harmless.
                continue
            token = _quiescence_control_token(
                message, "symcc-master-quiescence-commit-v1")
            committed = bool(
                token and token == accepted_probe_token and local_idle)
            if not token:
                quarantined_control_messages += 1
                continue
            ack = {
                "schema": "symcc-master-quiescence-ack-v1",
                "rank": rank,
                "probe_token": token,
                "committed": committed,
            }
            try:
                pending_sends.append(global_comm.isend(
                    ack, dest=0, tag=TAG_MASTER_QUIESCE_ACK))
            except (AttributeError, MPI.Exception, OSError,
                    RuntimeError) as error:
                set_control_error(f"master commit ACK failed: {error}")
                committed = False
            if committed:
                return True
            quarantined_control_messages += 1
        return False

    while not shutdown_requested:
        # Check wall-clock time limit
        if wall_timeout > 0 and (time.monotonic() - wall_start) >= wall_timeout:
            if is_root:
                print(f"[Master] Wall-clock timeout ({wall_timeout}s) reached. "
                      f"Shutting down.")
            break

        retire_pending_sends()
        now = time.monotonic()
        drain_master_status(now)

        if fatal_control_error:
            break

        # Try to import new inputs (from external source like AFL) — 限流重扫
        probe_prepared = bool(
            accepted_probe_token
            or (quiescence_gate is not None and quiescence_gate.probe_token)
        )
        if (not probe_prepared
                and now - last_import_scan >= IMPORT_SCAN_INTERVAL):
            last_import_scan = now
            import_inputs(args.input_dir)
        if (not probe_prepared and work_coordinator is not None
                and now - last_shared_scan >= work_scan_interval):
            last_shared_scan = now
            recover_committing_work()
            scan_shared_corpus()
            recover_expired_work()
        if (work_coordinator is not None
                and now - last_lease_heartbeat >= lease_heartbeat_interval):
            last_lease_heartbeat = now
            heartbeat = observe_work_lease_heartbeat(
                work_coordinator.heartbeat_all)
            if heartbeat.error:
                set_control_error(heartbeat.error)
            lost_work_leases += heartbeat.lost_lease_count

        # 本轮是否有实际工作（派发/收集）；无工作才 sleep，避免忙等且降低派发延迟
        wall_remaining = (
            max(0.0, wall_timeout - (now - wall_start))
            if wall_timeout > 0 else None
        )
        did_work = handle_runtime_lock_renewal(
            now,
            safe_to_start=not probe_prepared,
            wall_remaining=wall_remaining,
        )
        if fatal_control_error:
            break
        if did_work:
            now = time.monotonic()

        if (
            ulfm_coordinator is not None
            and now - last_ulfm_failure_poll >= ulfm_failure_poll_interval
        ):
            last_ulfm_failure_poll = now
            failure_comm = (
                group_comm if ulfm_recovery_comm is None else ulfm_recovery_comm
            )
            if ulfm_failure_sentinels:
                _poll_ulfm_failure_sentinels(
                    ulfm_failure_sentinels,
                    group_comm=group_comm,
                    recovery_comm=failure_comm,
                )
            else:
                failed_member_count = _ulfm_failed_member_count(failure_comm)
                if failed_member_count:
                    try:
                        failure_comm.Revoke()
                    except Exception as error:
                        if not is_ulfm_failure(error, MPI):
                            raise
                    raise _UlfmProcessFailureDetected(
                        f"ULFM detector observed {failed_member_count} "
                        "failed member(s)"
                    )

        # READY and RESULT use different tags, so MPI selective receives may
        # expose READY first. Park it until RESULT retires the active dispatch.
        while group_comm.iprobe(source=MPI.ANY_SOURCE, tag=TAG_READY):
            status = MPI.Status()
            group_comm.recv(source=MPI.ANY_SOURCE, tag=TAG_READY,
                            status=status)
            availability.observe_ready(status.Get_source())
            did_work = True

        # Collect results from workers
        while group_comm.iprobe(source=MPI.ANY_SOURCE, tag=TAG_RESULT):
            status = MPI.Status()
            result = group_comm.recv(source=MPI.ANY_SOURCE, tag=TAG_RESULT,
                                     status=status)
            worker_group_rank = status.Get_source()

            consume_worker_result(worker_group_rank, result)
            did_work = True
            # 不再每结果 print（热路径去除 f-string 格式化 + stdout I/O）；见下方周期汇总

        # Distribute only after joining READY with the preceding RESULT.
        while (pending_queue and _dispatch_window_open(
                wall_timeout, wall_start, args.timeout, time.monotonic())):
            input_hash, lease_token = pending_queue[0]
            if (work_coordinator is not None
                    and work_coordinator.tokens.get(input_hash) != lease_token):
                pending_queue.popleft()
                continue
            worker_group_rank = availability.claim(active_workers)
            if worker_group_rank is None:
                break
            pending_queue.popleft()
            work_message: typing.Any = input_hash
            ulfm_fence: dict[str, typing.Any] | None = None
            if ulfm_coordinator is not None:
                try:
                    shard_id = ulfm_worker_shards[worker_group_rank]
                    ulfm_lease = ulfm_coordinator.dispatch(shard_id, input_hash)
                    work_message = build_work_envelope(
                        {"input_hash": input_hash}, ulfm_lease
                    )
                    ulfm_fence = build_work_fence(ulfm_lease)
                except (KeyError, OSError, RuntimeError, ValueError) as error:
                    pending_queue.appendleft((input_hash, lease_token))
                    set_control_error(f"ULFM work dispatch failed: {error}")
                    break
            # Legacy mode sends only the hash. ULFM mode sends the same hash in
            # a generation-fenced envelope that the worker must echo verbatim.
            group_comm.send(work_message, dest=worker_group_rank, tag=TAG_WORK)
            active_workers[worker_group_rank] = (input_hash, lease_token)
            if ulfm_fence is not None:
                active_ulfm_fences[worker_group_rank] = ulfm_fence
                if _claim_ulfm_test_failure(
                    work_state_dir,
                    ulfm_master_stable_rank,
                    ulfm_generation,
                ):
                    print(
                        f"[Master endpoint {ulfm_master_stable_rank}] "
                        "Injecting ULFM process failure at generation "
                        f"{ulfm_generation}",
                        file=sys.stderr,
                        flush=True,
                    )
                    os._exit(86)
            did_work = True

        # 周期性进度汇总（替代每结果 print），仅 root，热路径外
        if is_root and time.monotonic() - last_report_time >= REPORT_INTERVAL:
            _dt = time.monotonic() - last_report_time
            _rate = (total_generated - reported_generated) / _dt if _dt > 0 else 0
            print(f"[Master] {total_generated} generated, {total_interesting} "
                  f"interesting, {len(analyzed_hashes)} analyzed, "
                  f"{len(active_workers)} busy ({_rate:.0f} tc/s)", flush=True)
            last_report_time = time.monotonic()
            reported_generated = total_generated

        local_idle = not pending_queue and not active_workers
        if multi_master:
            publish_status(local_idle)
            now = time.monotonic()
            drain_master_status(now)
            if handle_quiescence_control(local_idle, now):
                break
            for peer, seen in peer_last_seen.items():
                if now - seen > shutdown_grace:
                    set_control_error(
                        f"master {peer} status silent for {now - seen:.3f}s")
                    break
            if fatal_control_error:
                break
            if not did_work:
                time.sleep(min(0.05, status_interval))
            continue

        # Single-master termination remains a local monotonic deadline, but
        # takes the same durable-manifest/corpus/lease refresh boundary first.
        if local_idle:
            if not refresh_quiescence_frontier():
                idle_started = None
                continue

            now = time.monotonic()
            if idle_started is None:
                idle_started = now
                last_idle_report = now
                print(f"[Master {rank}] Waiting for inputs... "
                      f"({total_generated} generated, "
                      f"{total_interesting} interesting, "
                      f"{len(analyzed_hashes)} analyzed)")
            remaining = _idle_time_remaining(
                idle_started, idle_timeout, now)
            if remaining <= 0.0:
                if is_root:
                    elapsed = max(0.0, now - idle_started)
                    print(f"[Master] No more inputs after {elapsed:.3f}s "
                          f"(limit {idle_timeout:g}s). Shutting down.")
                break

            if now - last_idle_report >= 30.0:
                last_idle_report = now
                print(f"[Master {rank}] Waiting for inputs... "
                      f"({total_generated} generated, "
                      f"{total_interesting} interesting, "
                      f"{len(analyzed_hashes)} analyzed)")
            time.sleep(min(5.0, remaining))
            continue
        else:
            idle_started = None

        # 仅在本轮无派发/收集时才 sleep：有工作时立即进入下一轮（降低派发延迟、
        # 减小每轮批量→降低单核 master 峰值负载），空闲时让出 CPU。
        if not did_work:
            time.sleep(0.05)

    if fatal_control_error:
        print(
            f"[Master {rank}] Global work control failure: "
            f"{fatal_control_error}",
            file=sys.stderr,
            flush=True,
        )
    report_runtime_lock_metrics()

    if ulfm_failure_sentinels:
        _cancel_ulfm_failure_sentinels(ulfm_failure_sentinels)
        ulfm_failure_sentinels.clear()

    # --- Bounded, acknowledged shutdown of this master's worker group ---
    group_size = group_comm.Get_size()
    shutdown = _cooperative_shutdown_workers(
        group_comm,
        range(1, group_size),
        initial_ready=availability.ready,
        grace=shutdown_grace,
        result_callback=consume_worker_result,
    )
    print(
        f"[Master {rank}] Worker shutdown: "
        f"acked={len(shutdown['acknowledged'])}/{num_workers} "
        f"pending={list(shutdown['pending'])} "
        f"drained_results={shutdown['drained_results']} "
        f"result_errors={list(shutdown['result_errors'])} "
        f"quarantined={shutdown['quarantined_acks']} "
        f"communication_errors={list(shutdown['communication_errors'])} "
        f"elapsed={shutdown['elapsed']:.3f}s",
        flush=True,
    )
    if not shutdown["clean"]:
        return False
    if invalid_worker_results:
        print(
            f"[Master {rank}] Rejected {invalid_worker_results} invalid or "
            "unowned worker result(s).",
            file=sys.stderr,
            flush=True,
        )
        return False
    if fatal_control_error:
        print(
            f"[Master {rank}] Control failure during worker shutdown: "
            f"{fatal_control_error}",
            file=sys.stderr,
            flush=True,
        )
        return False
    if (stale_worker_results or recovered_work or replayed_commits
            or lost_work_leases
            or quarantined_control_messages):
        print(
            f"[Master {rank}] Work-control diagnostics: "
            f"stale_results={stale_worker_results} "
            f"recovered={recovered_work} "
            f"replayed_commits={replayed_commits} "
            f"lost_leases={lost_work_leases} "
            f"quarantined_control={quarantined_control_messages}",
            flush=True,
        )
    if work_coordinator is not None and (
            work_coordinator.heartbeat_batches
            or work_coordinator.heartbeat_failures):
        avoided = max(
            0,
            work_coordinator.heartbeat_renewals
            - work_coordinator.heartbeat_directory_syncs,
        )
        print(
            f"[Master {rank}] Lease heartbeat group commit: "
            f"batches={work_coordinator.heartbeat_batches} "
            f"renewals={work_coordinator.heartbeat_renewals} "
            f"directory_syncs={work_coordinator.heartbeat_directory_syncs} "
            f"directory_syncs_avoided={avoided} "
            f"failures={work_coordinator.heartbeat_failures}",
            flush=True,
        )

    # --- Bounded, exactly acknowledged statistics exchange across masters ---
    stats_exchange = _bounded_master_stats_exchange(
        global_comm,
        rank=rank,
        is_root=is_root,
        peer_masters=peer_masters,
        generated=total_generated,
        interesting=total_interesting,
        analyzed=analysis_observations,
        pending_sends=pending_sends,
        timeout=shutdown_grace,
    )
    if not stats_exchange["clean"]:
        print(
            f"[Master {rank}] Stats exchange incomplete: "
            f"received={list(stats_exchange['received'])} "
            f"pending={list(stats_exchange['pending'])} "
            f"quarantined={stats_exchange['quarantined']} "
            f"communication_errors={stats_exchange['communication_errors']}",
            file=sys.stderr,
            flush=True,
        )
        return False
    total_generated = int(stats_exchange["generated"])
    total_interesting = int(stats_exchange["interesting"])
    total_analyzed_observations = int(stats_exchange["analyzed"])

    # --- Print final summary (root only) ---
    if is_root:
        # Count the canonical no-follow regular namespace without materializing
        # names. Intersect external observations with that same public domain:
        # an input replaced before stage-B publication must not hide a child.
        try:
            corpus_counts = _count_public_corpus_objects(
                shared_dir, external_hashes)
        except OSError as error:
            print(
                f"[Master] Final corpus accounting failed: {error}",
                file=sys.stderr,
                flush=True,
            )
            return False

        external_input_count = corpus_counts.external
        actual_unique = corpus_counts.generated

        durable_by_master: dict[int, int] | None = None
        if work_coordinator is not None:
            try:
                durable_statistics = _durable_completed_work_statistics(
                    work_coordinator
                )
            except (OSError, TypeError, ValueError) as error:
                print(
                    "[Master] Durable cross-generation statistics failed: "
                    f"{error}",
                    file=sys.stderr,
                    flush=True,
                )
                return False
            total_generated = int(durable_statistics["generated"])
            total_analyzed_observations = int(
                durable_statistics["analyzed"]
            )
            durable_by_master = dict(durable_statistics["by_master"])

        total_masters = len(peer_masters) + 1
        total_workers_all = sum(
            len(worker_groups[m]) for m in master_ranks
        ) if peer_masters else num_workers

        wall_elapsed = time.monotonic() - wall_start
        throughput = total_generated / wall_elapsed if wall_elapsed > 0 else 0

        print("\n[Master] === Final Statistics ===")
        print(f"[Master] Total analysis observations:{total_analyzed_observations:>8}")
        print(f"[Master] Total test cases generated:  {total_generated}")
        print(f"[Master] External input objects:      {external_input_count}")
        print(f"[Master] New interesting test cases:  {actual_unique}")
        print(f"[Master] Throughput:                  {throughput:.1f} tc/s")
        print(f"[Master] Masters used:               {total_masters}")
        print(f"[Master] Workers used:               {total_workers_all}")
        by_master: typing.Any = (
            durable_by_master
            if durable_by_master is not None
            else stats_exchange.get("by_master", {})
        )
        if isinstance(by_master, dict):
            distribution = ", ".join(
                (
                    f"{master}={int(values)}"
                    if isinstance(values, int)
                    else f"{master}={int(values.get('analyzed', 0))}"
                )
                for master, values in sorted(by_master.items())
                if isinstance(values, (dict, int))
            )
            if distribution:
                print(f"[Master] Analysis by master:         {distribution}")

    return True


def worker_loop(group_comm: "MPI.Intracomm", args: argparse.Namespace,
                shared_dir: str, work_state_dir: str,
                stable_global_rank: int | None = None,
                ulfm_hot_path: bool = False,
                recovery_comm: typing.Any | None = None,
                ulfm_generation: int = 0) -> None:
    """
    Worker process main loop.

    Reads input from shared_dir/{hash}, runs SymCC, writes outputs to a hidden
    per-result staging directory, and sends only hashes plus the staging
    identity to the master.  The master owns public corpus promotion.
    """
    group_rank = group_comm.Get_rank()
    global_rank = (
        MPI.COMM_WORLD.Get_rank()
        if stable_global_rank is None else int(stable_global_rank)
    )
    MASTER = 0  # master is always rank 0 in group_comm

    target_cmd = args.target
    use_stdin = "@@" not in target_cmd
    timeout_sec = args.timeout

    worker_dir = tempfile.mkdtemp(prefix=f"symcc_worker_{global_rank}_")

    # Build env dict once and reuse
    worker_env = os.environ.copy()
    worker_env["SYMCC_ENABLE_LINEARIZATION"] = "1"

    shutdown_token = ""
    while True:
        # Signal readiness to our master (in group_comm)
        group_comm.send(group_rank, dest=MASTER, tag=TAG_READY)

        # Wait for work or stop
        msg, status = _receive_worker_control(
            group_comm,
            recovery_comm,
            master=MASTER,
        )

        if status.Get_tag() == TAG_STOP:
            shutdown_token = _shutdown_stop_token(msg, group_rank)
            if not shutdown_token:
                print(
                    f"[Worker {global_rank}] Ignoring malformed shutdown "
                    "message",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            break

        if status.Get_tag() != TAG_WORK:
            continue

        try:
            input_hash, work_fence = _decode_standalone_work_message(msg)
        except ValueError as error:
            print(
                f"[Worker {global_rank}] Invalid work envelope: {error}",
                file=sys.stderr,
                flush=True,
            )
            group_comm.send(
                {
                    "new_hashes": [],
                    "retcode": -1,
                    "elapsed": 0,
                    "num_generated": 0,
                    "staged_bytes": 0,
                    "input_hash": "",
                    "staging_id": "",
                    "protocol_error": "work-envelope",
                },
                dest=MASTER,
                tag=TAG_RESULT,
            )
            continue

        if ulfm_hot_path and _claim_ulfm_test_failure(
            work_state_dir, global_rank, ulfm_generation
        ):
            print(
                f"[Worker {global_rank}] Injecting ULFM process failure at "
                f"generation {ulfm_generation}",
                file=sys.stderr,
                flush=True,
            )
            os._exit(86)

        # Read input from shared dir
        shared_path = os.path.join(shared_dir, input_hash)
        input_file = os.path.join(worker_dir, "current_input")
        try:
            copied_input_hash, _copied_input_size = (
                _stream_copy_verified_input(
                    shared_path,
                    input_file,
                    expected_hash=input_hash,
                    max_bytes=args.input_max_bytes,
                )
            )
        except _InputBudgetExceeded as error:
            print(f"[Worker {global_rank}] {error}", file=sys.stderr)
            group_comm.send(_with_ulfm_result_fence({
                "new_hashes": [], "retcode": -1,
                "elapsed": 0, "num_generated": 0,
                "staged_bytes": 0,
                "input_hash": input_hash,
                "staging_id": "",
                "protocol_error": _INPUT_BUDGET_PROTOCOL_ERROR,
                "input_budget": error.payload(),
            }, work_fence), dest=MASTER, tag=TAG_RESULT)
            continue
        except (IOError, OSError, ValueError) as e:
            print(f"[Worker {global_rank}] Cannot read {input_hash}: {e}",
                  file=sys.stderr)
            group_comm.send(_with_ulfm_result_fence({
                "new_hashes": [], "retcode": -1,
                "elapsed": 0, "num_generated": 0,
                "staged_bytes": 0,
                "input_hash": "",
                "staging_id": "",
                "protocol_error": "input-copy-error",
            }, work_fence), dest=MASTER, tag=TAG_RESULT)
            continue

        run_output = os.path.join(worker_dir, f"run_{time.monotonic_ns()}")
        staging_id = ""
        retcode = -1
        elapsed = 0.0

        try:
            new_tests, retcode, elapsed = run_symcc(
                target_cmd, input_file, run_output, timeout_sec, use_stdin,
                base_env=worker_env,
                simulate=args.simulate,
                result_max_objects=args.result_max_objects,
                result_max_bytes=args.result_max_bytes,
            )

            # Stage child objects outside the public corpus. The master
            # promotes them only after the exact parent lease enters commit.
            try:
                new_hashes, staged_bytes, staging_id = _stage_worker_outputs(
                    new_tests,
                    work_state_dir,
                    global_rank,
                    max_objects=args.result_max_objects,
                    max_bytes=args.result_max_bytes,
                )
            except _ResultBudgetExceeded:
                raise
            except (IOError, OSError, TypeError, ValueError) as error:
                print(
                    f"[Worker {global_rank}] Cannot stage output: {error}",
                    file=sys.stderr,
                    flush=True,
                )
                result = {
                    "new_hashes": [],
                    "retcode": retcode,
                    "elapsed": elapsed,
                    "num_generated": 0,
                    "staged_bytes": 0,
                    "input_hash": copied_input_hash,
                    "staging_id": "",
                    "protocol_error": "corpus-publication",
                }
            else:
                result = {
                    "new_hashes": list(new_hashes),
                    "retcode": retcode,
                    "elapsed": elapsed,
                    "num_generated": len(new_tests),
                    "staged_bytes": staged_bytes,
                    "input_hash": copied_input_hash,
                    "staging_id": staging_id,
                    "protocol_error": "",
                }

        except _ResultBudgetExceeded as error:
            staging_id = error.staging_id or staging_id
            try:
                _remove_staged_outputs(
                    work_state_dir, global_rank, staging_id)
            except OSError:
                pass
            print(
                f"[Worker {global_rank}] {error}",
                file=sys.stderr,
                flush=True,
            )
            result = {
                "new_hashes": [],
                "retcode": retcode,
                "elapsed": elapsed,
                "num_generated": 0,
                "staged_bytes": 0,
                "input_hash": copied_input_hash,
                "staging_id": staging_id,
                "protocol_error": _RESULT_BUDGET_PROTOCOL_ERROR,
                "result_budget": error.payload(),
            }

        except (OSError, subprocess.SubprocessError, ValueError,
                RuntimeError) as e:
            # worker 弹性边界：I/O/子进程/解析错误不应拖垮整个 MPI 作业，回传错误结果继续
            print(f"[Worker {global_rank}] Error: {e}", file=sys.stderr)
            result = {
                "new_hashes": [], "retcode": -1,
                "elapsed": 0, "num_generated": 0,
                "staged_bytes": 0,
                "input_hash": copied_input_hash,
                "staging_id": staging_id,
                "protocol_error": "execution-error",
            }

        # Clean up run artifacts
        shutil.rmtree(run_output, ignore_errors=True)

        # Send only hashes back (~64B each, not file content)
        group_comm.send(
            _with_ulfm_result_fence(result, work_fence),
            dest=MASTER,
            tag=TAG_RESULT,
        )

    # Final cleanup
    shutil.rmtree(worker_dir, ignore_errors=True)
    _send_shutdown_ack(group_comm, group_rank, shutdown_token)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="MPI-parallel concolic execution with SymCC",
        usage="mpirun -np <N> python3 %(prog)s -i INPUT_DIR [options] "
              "-- TARGET [ARGS...]",
    )
    parser.add_argument(
        "-i", "--input-dir", required=True,
        help="Directory containing initial seed inputs",
    )
    parser.add_argument(
        "-o", "--output-dir", default=None,
        help="Directory to store all generated test cases",
    )
    parser.add_argument(
        "-f", "--failed-dir", default=None,
        help="Directory to store failing test cases",
    )
    parser.add_argument(
        "-t", "--timeout", type=int, default=90,
        help="Timeout in seconds per SymCC execution (default: 90)",
    )
    parser.add_argument(
        "--max-idle", type=int, default=60,
        help="Seconds to wait without new inputs before stopping (default: 60)",
    )
    parser.add_argument(
        "--wall-timeout", type=int, default=0,
        help="Total wall-clock time limit in seconds (0=unlimited, default: 0)",
    )
    parser.add_argument(
        "--workers-per-master", type=int, default=90,
        help="Target workers per master for auto-scaling (default: 90; 实测单个"
             "master 可近线性喂饱 ~96 worker）。np=192 → 3 masters（各 ~63 worker）。",
    )
    parser.add_argument(
        "--simulate", action="store_true",
        help="Simulation mode: generate random mutations when target produces "
             "no SymCC output (for gcc-compiled binaries)",
    )
    parser.add_argument(
        "target", nargs=argparse.REMAINDER,
        help="Target command (after '--')",
    )

    args = parser.parse_args()

    # Strip leading '--' from target
    if args.target and args.target[0] == "--":
        args.target = args.target[1:]

    if not args.target:
        parser.error("No target command specified. Use: -- TARGET [ARGS...]")

    return args


def _qualify_ulfm_master_generation_filesystem(
    master_comm: typing.Any,
    *,
    work_state_dir: str,
    shared_dir: str,
    work_epoch: str,
    global_rank: int,
    master_ranks: typing.Sequence[int],
    generation: int,
    cluster_probe_timeout: float,
    cluster_requalification_interval: float,
) -> tuple[SharedFilesystemCapabilities | None, bool, str]:
    """Qualify a rebuilt master generation and say whether to retain its comm."""
    if not _environment_enabled("SYMCC_SHARED_STATE_FS_PROBE"):
        return None, False, ""
    local_probe_timeout = _environment_float(
        "SYMCC_SHARED_STATE_FS_PROBE_TIMEOUT", 5.0, 0.001, 60.0
    )
    observation = observe_cluster_lock_qualification_inputs(
        capability_probe=lambda: probe_shared_state_filesystem(
            work_state_dir,
            timeout=local_probe_timeout,
            publication_root=shared_dir,
            requirements=FULL_SHARED_FILESYSTEM_REQUIREMENTS,
        ),
        processor_name_probe=MPI.Get_processor_name,
    )
    qualification = qualify_mpi_cluster_advisory_lock(
        master_comm,
        observation.capability,
        root=work_state_dir,
        epoch=work_epoch,
        global_rank=global_rank,
        expected_master_ranks=master_ranks,
        processor_name=observation.processor_name,
        qualification_generation=generation,
        timeout=cluster_probe_timeout,
        local_error=observation.error,
    )
    if not qualification.clean:
        raise UlfmRuntimeError(
            "shared filesystem generation qualification failed: "
            f"{qualification.error}"
        )
    return (
        qualification.capability,
        bool(
            qualification.verified
            and cluster_requalification_interval > 0.0
        ),
        qualification.proof_transcript,
    )


def main() -> None:
    comm = MPI.COMM_WORLD
    rank = comm.Get_rank()
    size = comm.Get_size()

    args = parse_args()
    _startup_trace(rank, "arguments-ready")

    ulfm_session_id = ""
    ulfm_endpoint_hosts: dict[int, str] | None = None
    if _environment_enabled("SYMCC_ULFM_HOT_PATH", "0"):
        local_policy = _ulfm_standalone_policy(size, max(1, size - 1))
        local_capability = probe_ulfm_runtime(
            MPI.COMM_SELF, policy=local_policy
        )
        local_qualification = {
            "rank": rank,
            "host": str(MPI.Get_processor_name()),
            "policy_sha256": local_policy.sha256,
            "capability": local_capability,
        }
        _startup_trace(rank, "ulfm-capability-allgather-enter")
        qualifications = comm.allgather(local_qualification)
        _startup_trace(rank, "ulfm-capability-allgather-exit")
        valid = (
            len(qualifications) == size
            and all(
                isinstance(item, dict)
                and type(item.get("rank")) is int
                and isinstance(item.get("host"), str)
                and item["host"]
                and isinstance(item.get("capability"), dict)
                and item["capability"].get("available") is True
                for item in qualifications
            )
            and {item["rank"] for item in qualifications} == set(range(size))
            and {
                item.get("policy_sha256") for item in qualifications
            } == {local_policy.sha256}
        )
        if not valid:
            if rank == 0:
                failures = [
                    {
                        "rank": item.get("rank"),
                        "error": (
                            "policy-mismatch"
                            if (
                                isinstance(item, dict)
                                and item.get("policy_sha256")
                                != local_policy.sha256
                            ) else (
                                item.get("capability", {}).get(
                                    "error", "malformed"
                                )
                                if isinstance(item, dict) else
                                "malformed"
                            )
                        ),
                    }
                    for item in qualifications
                    if (
                        not isinstance(item, dict)
                        or type(item.get("rank")) is not int
                        or not isinstance(item.get("capability"), dict)
                        or item["capability"].get("available") is not True
                        or item.get("policy_sha256") != local_policy.sha256
                    )
                ]
                print(
                    "[Master] SYMCC_ULFM_HOT_PATH requires a semantically "
                    f"qualified ULFM runtime on every rank: {failures}",
                    file=sys.stderr,
                    flush=True,
                )
            comm.Abort(74)
            return
        ulfm_endpoint_hosts = {
            int(item["rank"]): str(item["host"]) for item in qualifications
        }
        session_seed = os.urandom(32).hex() if rank == 0 else None
        _startup_trace(rank, "ulfm-session-bcast-enter")
        ulfm_session_id = str(comm.bcast(session_seed, root=0))
        _startup_trace(rank, "ulfm-session-bcast-exit")

    # Rank 0 is the sole configuration authority so every master and worker
    # enforces exactly the same result and input admission contract.
    result_budget_status = None
    if rank == 0:
        try:
            result_budget_status = {
                "ok": True,
                **_standalone_admission_budgets(),
            }
        except ValueError as error:
            result_budget_status = {"ok": False, "error": str(error)}
    _startup_trace(rank, "result-budget-bcast-enter")
    result_budget_status = comm.bcast(result_budget_status, root=0)
    _startup_trace(rank, "result-budget-bcast-exit")
    if (not isinstance(result_budget_status, dict)
            or not result_budget_status.get("ok")):
        if rank == 0:
            detail = (
                result_budget_status.get(
                    "error", "invalid standalone admission budget")
                if isinstance(result_budget_status, dict) else
                "invalid standalone admission budget consensus"
            )
            print(
                f"[Master] Standalone admission budget failed: {detail}",
                file=sys.stderr,
                flush=True,
            )
        comm.Abort(68)
        return
    args.result_max_objects = int(result_budget_status["max_objects"])
    args.result_max_bytes = int(result_budget_status["max_bytes"])
    args.input_max_bytes = int(result_budget_status["input_max_bytes"])

    warm_spare_status: dict[str, typing.Any] | None = None
    if rank == 0:
        try:
            warm_spares = _environment_integer(
                "SYMCC_ULFM_WARM_SPARES",
                0,
                0,
                max(0, size - 3),
            )
            if warm_spares and not ulfm_session_id:
                raise ValueError(
                    "SYMCC_ULFM_WARM_SPARES requires SYMCC_ULFM_HOT_PATH=1"
                )
            warm_spare_status = {"ok": True, "count": warm_spares}
        except ValueError as error:
            warm_spare_status = {"ok": False, "error": str(error)}
    _startup_trace(rank, "warm-spare-bcast-enter")
    warm_spare_status = comm.bcast(warm_spare_status, root=0)
    _startup_trace(rank, "warm-spare-bcast-exit")
    if (
        not isinstance(warm_spare_status, dict)
        or not warm_spare_status.get("ok")
    ):
        if rank == 0:
            detail = (
                warm_spare_status.get("error", "invalid warm-spare policy")
                if isinstance(warm_spare_status, dict)
                else "invalid warm-spare policy consensus"
            )
            print(f"[Master] {detail}", file=sys.stderr, flush=True)
        comm.Abort(75)
        return
    warm_spares = int(warm_spare_status["count"])
    initial_active_size = size - warm_spares

    # --- Auto-compute role assignment ---
    master_ranks, worker_groups = compute_roles(
        initial_active_size, args.workers_per_master
    )
    master_set = set(master_ranks)
    if ulfm_session_id and size < 3:
        if rank == 0:
            print(
                "[Master] SYMCC_ULFM_HOT_PATH requires at least one master "
                "and two workers.",
                file=sys.stderr,
                flush=True,
            )
        comm.Abort(75)
        return
    managed_ulfm = bool(
        ulfm_session_id and (len(master_ranks) > 1 or warm_spares > 0)
    )

    # Find each process's group master
    my_master_rank = None
    if rank in master_set:
        my_master_rank = rank
    else:
        for m, workers in worker_groups.items():
            if rank in workers:
                my_master_rank = m
                break

    # Create isolated group communicator: each master + its workers.
    # Within group_comm, the master has rank 0 (lowest global rank in group).
    # This prevents cross-group message interference.
    _startup_trace(rank, "initial-group-split-enter")
    group_comm = comm.Split(
        my_master_rank if my_master_rank is not None else MPI.UNDEFINED,
        rank,
    )
    _startup_trace(rank, "initial-group-split-exit")
    # A second communicator isolates the bounded filesystem qualification
    # control plane from READY/RESULT traffic. MPI_UNDEFINED excludes workers.
    _startup_trace(rank, "initial-master-split-enter")
    master_comm = comm.Split(
        0 if rank in master_set else MPI.UNDEFINED,
        rank,
    )
    _startup_trace(rank, "initial-master-split-exit")

    # Determine shared directory (all ranks must agree on the path)
    if args.output_dir:
        shared_dir = os.path.abspath(args.output_dir)
    else:
        # Create temp dir on rank 0 and broadcast to all
        if rank == 0:
            shared_dir = tempfile.mkdtemp(prefix="symcc_shared_")
        else:
            shared_dir = None
        shared_dir = comm.bcast(shared_dir, root=0)

    durable_makedirs(shared_dir)

    # A configured epoch reopens an interrupted WAL-like result-publication
    # state.  Otherwise one random epoch isolates this MPI job.  Rank 0 is the
    # sole authority and broadcasts both validation and the selected identity.
    epoch_selection = None
    if rank == 0:
        try:
            work_epoch, resumed_epoch = _select_work_epoch(
                os.environ.get("SYMCC_STANDALONE_WORK_EPOCH"))
            epoch_selection = {
                "ok": True,
                "epoch": work_epoch,
                "resumed": resumed_epoch,
            }
        except ValueError as error:
            epoch_selection = {
                "ok": False,
                "epoch": "",
                "resumed": False,
                "error": str(error),
            }
    _startup_trace(rank, "epoch-bcast-enter")
    epoch_selection = comm.bcast(epoch_selection, root=0)
    _startup_trace(rank, "epoch-bcast-exit")
    if not isinstance(epoch_selection, dict) or not epoch_selection.get("ok"):
        if rank == 0:
            detail = (
                epoch_selection.get("error", "invalid work epoch")
                if isinstance(epoch_selection, dict) else
                "invalid work epoch selection"
            )
            print(f"[Master] {detail}", file=sys.stderr, flush=True)
        comm.Abort(64)
        return
    work_epoch = str(epoch_selection["epoch"])
    resumed_epoch = bool(epoch_selection["resumed"])
    work_state_dir = os.path.join(
        shared_dir, f".standalone-work-{work_epoch}")

    prequalified_filesystem: SharedFilesystemCapabilities | None = None
    runtime_master_comm: typing.Any | None = None
    cluster_probe_timeout = _environment_float(
        "SYMCC_SHARED_STATE_CLUSTER_PROBE_TIMEOUT",
        30.0,
        0.001,
        3600.0,
    )
    cluster_requalification_interval = _environment_float(
        "SYMCC_SHARED_STATE_CLUSTER_REQUALIFY_INTERVAL",
        60.0,
        0.0,
        86400.0,
    )
    cluster_requalification_jitter = _environment_float(
        "SYMCC_SHARED_STATE_CLUSTER_REQUALIFY_JITTER",
        0.1,
        0.0,
        0.5,
    )
    if rank in master_set and not managed_ulfm:
        shared_fs_probe = _environment_enabled("SYMCC_SHARED_STATE_FS_PROBE")
        if shared_fs_probe:
            local_probe_timeout = _environment_float(
                "SYMCC_SHARED_STATE_FS_PROBE_TIMEOUT", 5.0, 0.001, 60.0)
            observation = observe_cluster_lock_qualification_inputs(
                capability_probe=lambda: probe_shared_state_filesystem(
                    work_state_dir,
                    timeout=local_probe_timeout,
                    publication_root=shared_dir,
                    requirements=FULL_SHARED_FILESYSTEM_REQUIREMENTS,
                ),
                processor_name_probe=MPI.Get_processor_name,
            )
            qualification = qualify_mpi_cluster_advisory_lock(
                master_comm,
                observation.capability,
                root=work_state_dir,
                epoch=work_epoch,
                global_rank=rank,
                expected_master_ranks=master_ranks,
                processor_name=observation.processor_name,
                timeout=cluster_probe_timeout,
                local_error=observation.error,
            )
            if not qualification.clean:
                if rank == 0:
                    print(
                        "[Master] Shared filesystem cluster qualification "
                        f"failed: {qualification.error}",
                        file=sys.stderr,
                        flush=True,
                    )
                comm.Abort(65)
                return
            prequalified_filesystem = qualification.capability
            if rank == 0 and qualification.verified:
                print(
                    "[Master] Cluster lock proof transcript generation 0: "
                    f"{qualification.proof_transcript}",
                    flush=True,
                )
            if (
                qualification.verified
                and cluster_requalification_interval > 0.0
            ):
                runtime_master_comm = master_comm
        if runtime_master_comm is None:
            master_comm.Free()
    elif rank in master_set:
        # Multi-master ULFM rebuilds this control plane after every shrink.
        # Keeping the startup communicator would make requalification depend
        # on a membership that can no longer reach consensus.
        master_comm.Free()

    # Persistent output roots retain completed epochs under a reserved name so
    # recursive deletion leaves the completion critical path. Reclaim bounded
    # root and directory-entry work on startup, serialized across MPI jobs.
    gc_status = None
    if rank == 0:
        try:
            gc_limit = _environment_integer(
                "SYMCC_RETIRED_WORK_STATE_GC_LIMIT", 1, 0, 1_000_000)
            gc_entry_budget = _environment_integer(
                "SYMCC_RETIRED_WORK_STATE_GC_ENTRY_BUDGET",
                4096,
                1,
                10_000_000,
            )
            gc_time_budget = _environment_strict_float(
                "SYMCC_RETIRED_WORK_STATE_GC_TIME_BUDGET_SECONDS",
                0.05,
                0.001,
                3600.0,
            )
            gc_result = (
                _reclaim_retired_work_states(
                    shared_dir,
                    limit=gc_limit,
                    lock_timeout=min(30.0, cluster_probe_timeout),
                    entry_budget=gc_entry_budget,
                    time_budget=gc_time_budget,
                )
                if args.output_dir else
                _RetiredWorkStateGcResult((), None, 0, "temporary-output")
            )
            gc_status = {
                "ok": True,
                "reclaimed": gc_result.reclaimed_roots,
                "partial": gc_result.partial_root,
                "removed_entries": gc_result.removed_entries,
                "stop_reason": gc_result.stop_reason,
                "scanned_entries": gc_result.scanned_entries,
                "candidate_roots": gc_result.candidate_roots,
                "root_limit": gc_limit,
                "entry_budget": gc_entry_budget,
                "time_budget_seconds": gc_time_budget,
            }
        except (OSError, TimeoutError, TypeError, ValueError) as error:
            gc_status = {
                "ok": False,
                "error": str(error),
            }
    _startup_trace(rank, "retired-gc-bcast-enter")
    gc_status = comm.bcast(gc_status, root=0)
    _startup_trace(rank, "retired-gc-bcast-exit")
    if not isinstance(gc_status, dict) or not gc_status.get("ok"):
        if rank == 0:
            detail = (
                gc_status.get("error", "retired work-state GC failed")
                if isinstance(gc_status, dict) else
                "invalid retired work-state GC result"
            )
            print(
                f"[Master] Retired work-state GC failed: {detail}",
                file=sys.stderr,
                flush=True,
            )
        comm.Abort(66)
        return

    retirement_primitive_status = None
    if rank == 0:
        try:
            if args.output_dir:
                _probe_retirement_noreplace(shared_dir)
            retirement_primitive_status = {"ok": True}
        except (OSError, TypeError, ValueError) as error:
            retirement_primitive_status = {
                "ok": False,
                "error": str(error),
            }
    _startup_trace(rank, "retirement-probe-bcast-enter")
    retirement_primitive_status = comm.bcast(
        retirement_primitive_status, root=0)
    _startup_trace(rank, "retirement-probe-bcast-exit")
    if (
        not isinstance(retirement_primitive_status, dict)
        or not retirement_primitive_status.get("ok")
    ):
        if rank == 0:
            detail = (
                retirement_primitive_status.get(
                    "error", "retirement primitive probe failed")
                if isinstance(retirement_primitive_status, dict) else
                "invalid retirement primitive probe result"
            )
            print(
                f"[Master] Retirement primitive probe failed: {detail}",
                file=sys.stderr,
                flush=True,
            )
        comm.Abort(67)
        return

    # Print configuration (root only)
    if rank == 0:
        print("SymCC MPI Parallel Concolic Execution")
        print(f"  Processes:     {size}")
        num_masters = len(master_ranks)
        if num_masters == 1:
            print(f"  Mode:          single-master "
                  f"({initial_active_size - 1} workers)")
        else:
            print(f"  Mode:          multi-master "
                  f"({num_masters} masters, auto-scaled at "
                  f"{args.workers_per_master} workers/master)")
            for m in master_ranks:
                print(f"    Master {m}: {len(worker_groups[m])} workers")
        if warm_spares:
            print(
                f"  Warm spares:   {warm_spares} prelaunched "
                f"({initial_active_size} active processes)"
            )
        print(f"  Shared dir:    {shared_dir}")
        print(f"  Input dir:     {args.input_dir}")
        print(f"  Target:        {' '.join(args.target)}")
        print(f"  Timeout:       {args.timeout}s per execution")
        print(
            "  Result budget: "
            f"{args.result_max_objects} objects, "
            f"{args.result_max_bytes} bytes per parent"
        )
        print(f"  Input budget:  {args.input_max_bytes} bytes per object")
        epoch_mode = "configured recovery" if resumed_epoch else "new"
        print(f"  Work epoch:    {work_epoch} ({epoch_mode})")
        print(
            "  Retired GC:    "
            f"{len(gc_status['reclaimed'])} root(s) reclaimed, "
            f"{gc_status['removed_entries']} entry(s) removed, "
            f"stop={gc_status['stop_reason']}"
        )
        print(
            "  Retired budget:"
            f" roots={gc_status['root_limit']},"
            f" entries={gc_status['entry_budget']},"
            f" time={gc_status['time_budget_seconds']:g}s"
        )
        print(
            "  Retired scan:  "
            f" entries={gc_status['scanned_entries']},"
            f" candidates={gc_status['candidate_roots']},"
            " selected="
            f"{min(gc_status['candidate_roots'], gc_status['root_limit'])}"
        )
        if gc_status["partial"]:
            print(f"  Retired partial: {gc_status['partial']}")
        if args.output_dir:
            print("  Retirement:    renameat2(RENAME_NOREPLACE) qualified")
        if args.wall_timeout > 0:
            print(f"  Wall timeout:  {args.wall_timeout}s total")
        print()

    # --- Run ---
    lifecycle_clean = True
    campaign_comm = group_comm
    if managed_ulfm:
        if ulfm_endpoint_hosts is None:
            raise UlfmRuntimeError("ULFM endpoint inventory disappeared")

        # All ranks now recover one membership communicator.  Per-master
        # communicators below are disposable generation-local views.
        if group_comm != MPI.COMM_NULL:
            group_comm.Free()
        campaign_comm = comm
        stable_rank = rank
        current_resumed_epoch = resumed_epoch
        current_generation = 0
        active_budget = initial_active_size
        campaign_clock: dict[str, float] = {}
        current_transport_endpoints = {
            transport_rank: transport_rank for transport_rank in range(size)
        }
        membership_run_id = f"standalone-global-{ulfm_session_id}"
        membership_store_root = os.path.join(
            work_state_dir, "ulfm-query-store", "global-membership"
        )

        membership_status: dict[str, typing.Any] | None = None
        if rank == 0:
            try:
                _initialize_ulfm_membership(
                    run_id=membership_run_id,
                    endpoint_hosts=ulfm_endpoint_hosts,
                    store_root=membership_store_root,
                    transport_endpoint_ranks=current_transport_endpoints,
                )
                membership_status = {"ok": True}
            except Exception as error:
                membership_status = {
                    "ok": False,
                    "error": f"{type(error).__name__}: {error}",
                }
        _startup_trace(rank, "global-membership-bcast-enter")
        membership_status = campaign_comm.bcast(membership_status, root=0)
        _startup_trace(rank, "global-membership-bcast-exit")
        if (
            not isinstance(membership_status, dict)
            or not membership_status.get("ok")
        ):
            if rank == 0:
                detail = (
                    membership_status.get("error", "invalid result")
                    if isinstance(membership_status, dict)
                    else "invalid result"
                )
                print(
                    "[Master] ULFM global membership setup failed: "
                    f"{detail}",
                    file=sys.stderr,
                    flush=True,
                )
            campaign_comm.Abort(76)
            return

        while True:
            generation_group_comm: typing.Any | None = None
            generation_runtime_master_comm: typing.Any | None = None
            try:
                campaign_comm.Set_errhandler(MPI.ERRORS_RETURN)
                current_rank = campaign_comm.Get_rank()
                current_size = campaign_comm.Get_size()
                if current_size < 2:
                    raise UlfmRuntimeError(
                        "ULFM campaign has no surviving worker"
                    )
                _startup_trace(
                    stable_rank,
                    f"generation-{current_generation}-membership-allgather-enter",
                )
                gathered_stable_ranks = campaign_comm.allgather(stable_rank)
                _startup_trace(
                    stable_rank,
                    f"generation-{current_generation}-membership-allgather-exit",
                )
                if (
                    len(gathered_stable_ranks) != current_size
                    or len(set(gathered_stable_ranks)) != current_size
                    or any(
                        type(value) is not int
                        or value not in ulfm_endpoint_hosts
                        for value in gathered_stable_ranks
                    )
                ):
                    raise UlfmRuntimeError(
                        "repaired global endpoint inventory is invalid"
                    )
                current_transport_endpoints = {
                    transport_rank: int(endpoint_rank)
                    for transport_rank, endpoint_rank in enumerate(
                        gathered_stable_ranks
                    )
                }
                current_active_size = min(active_budget, current_size)
                active_transport_endpoints = {
                    transport_rank: current_transport_endpoints[transport_rank]
                    for transport_rank in range(current_active_size)
                }
                (
                    current_master_ranks,
                    current_transport_groups,
                    current_stable_groups,
                ) = _ulfm_generation_layout(
                    active_transport_endpoints,
                    args.workers_per_master,
                )
                current_master_set = set(current_master_ranks)
                current_active = current_rank < current_active_size
                if current_rank == 0:
                    generation_manifest = _ulfm_generation_manifest(
                        generation=current_generation,
                        active_budget=active_budget,
                        transport_endpoint_ranks=current_transport_endpoints,
                        master_ranks=current_master_ranks,
                        transport_worker_groups=current_transport_groups,
                    )
                    manifest_path = _record_ulfm_generation_manifest(
                        membership_store_root, generation_manifest
                    )
                    promoted = [
                        endpoint
                        for endpoint in generation_manifest["active_endpoints"]
                        if endpoint >= active_budget
                    ]
                    print(
                        "[Master] ULFM generation layout: "
                        f"generation={current_generation} "
                        f"active={len(generation_manifest['active_endpoints'])} "
                        f"standby={len(generation_manifest['standby_endpoints'])} "
                        f"promoted={promoted} evidence={manifest_path}",
                        flush=True,
                    )

                current_group_master: int | None = None
                if current_rank in current_master_set:
                    current_group_master = current_rank
                elif current_active:
                    current_group_master = next(
                        master
                        for master, workers in current_transport_groups.items()
                        if current_rank in workers
                    )

                _startup_trace(
                    stable_rank,
                    f"generation-{current_generation}-group-split-enter",
                )
                generation_group_comm = campaign_comm.Split(
                    (
                        current_group_master
                        if current_group_master is not None
                        else MPI.UNDEFINED
                    ),
                    current_rank,
                )
                _startup_trace(
                    stable_rank,
                    f"generation-{current_generation}-group-split-exit",
                )
                _startup_trace(
                    stable_rank,
                    f"generation-{current_generation}-master-split-enter",
                )
                generation_master_comm = campaign_comm.Split(
                    0 if current_rank in current_master_set else MPI.UNDEFINED,
                    current_rank,
                )
                _startup_trace(
                    stable_rank,
                    f"generation-{current_generation}-master-split-exit",
                )
                generation_capability: (
                    SharedFilesystemCapabilities | None
                ) = None
                if current_rank in current_master_set:
                    (
                        generation_capability,
                        retain_master_comm,
                        proof_transcript,
                    ) = _qualify_ulfm_master_generation_filesystem(
                        generation_master_comm,
                        work_state_dir=work_state_dir,
                        shared_dir=shared_dir,
                        work_epoch=work_epoch,
                        global_rank=current_rank,
                        master_ranks=current_master_ranks,
                        generation=current_generation,
                        cluster_probe_timeout=cluster_probe_timeout,
                        cluster_requalification_interval=(
                            cluster_requalification_interval
                        ),
                    )
                    if current_rank == 0 and proof_transcript:
                        print(
                            "[Master] Cluster lock proof transcript generation "
                            f"{current_generation}: {proof_transcript}",
                            flush=True,
                        )
                    if retain_master_comm:
                        generation_runtime_master_comm = generation_master_comm
                    else:
                        generation_master_comm.Free()

                if current_rank in current_master_set:
                    assert current_group_master is not None
                    stable_master = current_transport_endpoints[
                        current_group_master
                    ]
                    stable_workers = current_stable_groups[
                        current_group_master
                    ]
                    group_transport_endpoints = {
                        local_rank: current_transport_endpoints[transport_rank]
                        for local_rank, transport_rank in enumerate(
                            (
                                current_group_master,
                                *current_transport_groups[
                                    current_group_master
                                ],
                            )
                        )
                    }
                    peer_masters = [
                        master
                        for master in current_master_ranks
                        if master != current_rank
                    ]
                    lifecycle_clean = master_loop(
                        campaign_comm,
                        generation_group_comm,
                        args,
                        peer_masters,
                        current_rank == 0,
                        shared_dir,
                        current_master_ranks,
                        current_stable_groups,
                        work_state_dir,
                        work_epoch,
                        current_resumed_epoch,
                        generation_capability,
                        filesystem_qualification_comm=(
                            generation_runtime_master_comm
                        ),
                        filesystem_qualification_timeout=(
                            cluster_probe_timeout
                        ),
                        filesystem_requalification_interval=(
                            cluster_requalification_interval
                        ),
                        filesystem_requalification_jitter=(
                            cluster_requalification_jitter
                        ),
                        ulfm_endpoint_hosts=ulfm_endpoint_hosts,
                        ulfm_session_id=(
                            f"{ulfm_session_id}-generation-"
                            f"{current_generation}-master-{stable_master}"
                        ),
                        ulfm_initial_master_rank=stable_master,
                        ulfm_initial_worker_ranks=stable_workers,
                        ulfm_transport_endpoint_ranks=(
                            group_transport_endpoints
                        ),
                        ulfm_recovery_comm=campaign_comm,
                        ulfm_generation=current_generation,
                        campaign_started_at=campaign_clock.get("started_at"),
                        campaign_clock=campaign_clock,
                    )
                    if not lifecycle_clean:
                        raise UlfmRuntimeError(
                            "repaired master could not complete its lifecycle"
                        )
                    if current_rank == 0:
                        stop = {
                            "schema": "symcc-ulfm-standby-stop-v1",
                            "generation": current_generation,
                        }
                        for standby_rank in range(
                            current_active_size, current_size
                        ):
                            campaign_comm.send(
                                stop,
                                dest=standby_rank,
                                tag=TAG_ULFM_STANDBY_STOP,
                            )
                elif current_active:
                    worker_loop(
                        generation_group_comm,
                        args,
                        shared_dir,
                        work_state_dir,
                        stable_global_rank=stable_rank,
                        ulfm_hot_path=True,
                        recovery_comm=campaign_comm,
                        ulfm_generation=current_generation,
                    )
                else:
                    _wait_ulfm_standby(
                        campaign_comm,
                        generation=current_generation,
                    )

                if generation_group_comm != MPI.COMM_NULL:
                    generation_group_comm.Free()
                generation_group_comm = None
                if generation_runtime_master_comm is not None:
                    generation_runtime_master_comm.Free()
                    generation_runtime_master_comm = None
                break
            except Exception as error:
                if not (
                    is_ulfm_failure(error, MPI)
                    or isinstance(error, _UlfmProcessFailureDetected)
                ):
                    print(
                        f"[ULFM endpoint {stable_rank}] unrecoverable "
                        f"multi-master error: {type(error).__name__}: {error}",
                        file=sys.stderr,
                        flush=True,
                    )
                    try:
                        campaign_comm.Abort(76)
                    finally:
                        os._exit(76)
                campaign_clock.setdefault("paused_at", time.monotonic())
                try:
                    repaired_comm, receipt = _repair_ulfm_membership(
                        campaign_comm,
                        run_id=membership_run_id,
                        local_endpoint_rank=stable_rank,
                        endpoint_hosts=ulfm_endpoint_hosts,
                        store_root=membership_store_root,
                        transport_endpoint_ranks=current_transport_endpoints,
                    )
                except Exception as recovery_error:
                    print(
                        f"[ULFM endpoint {stable_rank}] global communicator "
                        f"recovery failed: {type(recovery_error).__name__}: "
                        f"{recovery_error}",
                        file=sys.stderr,
                        flush=True,
                    )
                    try:
                        campaign_comm.Abort(77)
                    finally:
                        os._exit(77)
                campaign_comm = repaired_comm
                current_resumed_epoch = True
                current_generation = int(receipt["target_generation"])
                if campaign_comm.Get_rank() == 0:
                    print(
                        "[Master] ULFM global recovery committed: "
                        f"generation={current_generation} "
                        f"failed={receipt['failed_endpoints']} "
                        f"requeued={len(receipt['requeued_work'])}",
                        flush=True,
                    )
    elif ulfm_session_id:
        if ulfm_endpoint_hosts is None:
            raise UlfmRuntimeError("ULFM endpoint inventory disappeared")
        stable_rank = rank
        initial_worker_ranks = tuple(range(1, size))
        current_resumed_epoch = resumed_epoch
        campaign_clock = {}
        current_transport_endpoints = {
            transport_rank: transport_rank for transport_rank in range(size)
        }
        run_id = f"standalone-0-{ulfm_session_id}"
        ulfm_store_root = os.path.join(
            work_state_dir, "ulfm-query-store", "master-0"
        )
        while True:
            try:
                campaign_comm.Set_errhandler(MPI.ERRORS_RETURN)
                current_rank = campaign_comm.Get_rank()
                current_size = campaign_comm.Get_size()
                if current_size < 2:
                    raise UlfmRuntimeError(
                        "ULFM campaign has no surviving worker"
                    )
                gathered_stable_ranks = campaign_comm.allgather(stable_rank)
                if (
                    len(gathered_stable_ranks) != current_size
                    or len(set(gathered_stable_ranks)) != current_size
                    or any(
                        type(value) is not int
                        or value not in ulfm_endpoint_hosts
                        for value in gathered_stable_ranks
                    )
                ):
                    raise UlfmRuntimeError(
                        "repaired communicator endpoint inventory is invalid"
                    )
                current_transport_endpoints = {
                    transport_rank: int(endpoint_rank)
                    for transport_rank, endpoint_rank in enumerate(
                        gathered_stable_ranks
                    )
                }
                current_workers = [
                    current_transport_endpoints[transport_rank]
                    for transport_rank in range(1, current_size)
                ]
                current_worker_groups = {0: current_workers}
                if current_rank == 0:
                    lifecycle_clean = master_loop(
                        campaign_comm,
                        campaign_comm,
                        args,
                        [],
                        True,
                        shared_dir,
                        [0],
                        current_worker_groups,
                        work_state_dir,
                        work_epoch,
                        current_resumed_epoch,
                        prequalified_filesystem,
                        filesystem_qualification_comm=runtime_master_comm,
                        filesystem_qualification_timeout=cluster_probe_timeout,
                        filesystem_requalification_interval=(
                            cluster_requalification_interval),
                        filesystem_requalification_jitter=(
                            cluster_requalification_jitter),
                        ulfm_endpoint_hosts=ulfm_endpoint_hosts,
                        ulfm_session_id=ulfm_session_id,
                        ulfm_initial_master_rank=0,
                        ulfm_initial_worker_ranks=initial_worker_ranks,
                        ulfm_transport_endpoint_ranks=(
                            current_transport_endpoints),
                        campaign_started_at=campaign_clock.get("started_at"),
                        campaign_clock=campaign_clock,
                    )
                    if not lifecycle_clean:
                        raise UlfmRuntimeError(
                            "repaired master could not complete its lifecycle"
                        )
                else:
                    worker_loop(
                        campaign_comm,
                        args,
                        shared_dir,
                        work_state_dir,
                        stable_global_rank=stable_rank,
                        ulfm_hot_path=True,
                    )
                break
            except Exception as error:
                if not (
                    is_ulfm_failure(error, MPI)
                    or isinstance(error, _UlfmProcessFailureDetected)
                ):
                    print(
                        f"[ULFM endpoint {stable_rank}] unrecoverable campaign "
                        f"error: {type(error).__name__}: {error}",
                        file=sys.stderr,
                        flush=True,
                    )
                    try:
                        campaign_comm.Abort(76)
                    finally:
                        os._exit(76)
                campaign_clock.setdefault("paused_at", time.monotonic())
                try:
                    repaired_comm, receipt = _repair_ulfm_hot_path(
                        campaign_comm,
                        run_id=run_id,
                        local_endpoint_rank=stable_rank,
                        initial_master_rank=0,
                        initial_worker_ranks=initial_worker_ranks,
                        endpoint_hosts=ulfm_endpoint_hosts,
                        store_root=ulfm_store_root,
                        transport_endpoint_ranks=current_transport_endpoints,
                    )
                except Exception as recovery_error:
                    print(
                        f"[ULFM endpoint {stable_rank}] communicator recovery "
                        f"failed: {type(recovery_error).__name__}: "
                        f"{recovery_error}",
                        file=sys.stderr,
                        flush=True,
                    )
                    try:
                        campaign_comm.Abort(77)
                    finally:
                        os._exit(77)
                campaign_comm = repaired_comm
                current_resumed_epoch = True
                if campaign_comm.Get_rank() == 0:
                    print(
                        "[Master] ULFM recovery committed: "
                        f"generation={receipt['target_generation']} "
                        f"failed={receipt['failed_endpoints']} "
                        f"requeued={len(receipt['requeued_work'])}",
                        flush=True,
                    )
    elif rank in master_set:
        is_root = (rank == 0)
        peer_masters = [m for m in master_ranks if m != rank]
        lifecycle_clean = master_loop(
            comm,
            group_comm,
            args,
            peer_masters,
            is_root,
            shared_dir,
            master_ranks,
            worker_groups,
            work_state_dir,
            work_epoch,
            resumed_epoch,
            prequalified_filesystem,
            filesystem_qualification_comm=runtime_master_comm,
            filesystem_qualification_timeout=cluster_probe_timeout,
            filesystem_requalification_interval=(
                cluster_requalification_interval),
            filesystem_requalification_jitter=(
                cluster_requalification_jitter),
        )
    else:
        worker_loop(group_comm, args, shared_dir, work_state_dir)

    if ulfm_session_id:
        current_rank = campaign_comm.Get_rank()
        if current_rank == 0 and runtime_master_comm is not None:
            try:
                runtime_master_comm.Free()
            except Exception:
                pass
        finalize_grace = _bounded_mpi_timeout(
            os.environ.get("SYMCC_FINALIZE_GRACE_SEC", "30"), 30.0
        )
        if not lifecycle_clean or not _bounded_mpi_barrier(
            campaign_comm, finalize_grace
        ):
            if current_rank == 0:
                print(
                    "[Master] Incomplete repaired MPI lifecycle; aborting job.",
                    file=sys.stderr,
                    flush=True,
                )
            campaign_comm.Abort(78)
            return
        if current_rank == 0:
            try:
                _cleanup_completed_work_state(
                    work_state_dir,
                    shared_dir,
                    remove_shared_dir=not bool(args.output_dir),
                )
            except (OSError, TypeError, ValueError) as error:
                print(
                    f"[Master] Durable completed-epoch retirement failed: {error}",
                    file=sys.stderr,
                    flush=True,
                )
                campaign_comm.Abort(79)
                return
        if not _bounded_mpi_barrier(campaign_comm, finalize_grace):
            if current_rank == 0:
                print(
                    "[Master] Post-cleanup repaired MPI barrier expired.",
                    file=sys.stderr,
                    flush=True,
                )
            campaign_comm.Abort(80)
            return
        try:
            campaign_comm.Free()
        except Exception:
            pass
        MPI.Finalize()
        return

    # --- Cleanup ---
    master_cleanup_ready = True
    if rank in master_set and runtime_master_comm is not None:
        master_cleanup_grace = min(
            5.0,
            _bounded_mpi_timeout(
                os.environ.get("SYMCC_FINALIZE_GRACE_SEC", "30"),
                30.0,
            ),
        )
        master_cleanup_ready = _bounded_mpi_barrier(
            runtime_master_comm,
            master_cleanup_grace,
        )
        if not master_cleanup_ready:
            lifecycle_clean = False
            print(
                f"[Master {rank}] Master cleanup rendezvous expired.",
                file=sys.stderr,
                flush=True,
            )
    if rank in master_set and not lifecycle_clean:
        print(
            f"[Master {rank}] Incomplete MPI lifecycle after bounded "
            "master rendezvous; aborting job.",
            file=sys.stderr,
            flush=True,
        )
        comm.Abort(70)
        return

    if rank in master_set and runtime_master_comm is not None:
        runtime_master_comm.Free()

    group_comm.Free()
    finalize_grace = _bounded_mpi_timeout(
        os.environ.get("SYMCC_FINALIZE_GRACE_SEC", "30"),
        30.0,
    )
    if not _bounded_mpi_barrier(comm, finalize_grace):
        if rank == 0:
            print(
                "[Master] Pre-cleanup MPI barrier deadline expired; "
                "aborting job.",
                file=sys.stderr,
                flush=True,
            )
        comm.Abort(71)
        return

    if rank == 0:
        try:
            _cleanup_completed_work_state(
                work_state_dir,
                shared_dir,
                remove_shared_dir=not bool(args.output_dir),
            )
        except (OSError, TypeError, ValueError) as error:
            print(
                f"[Master] Durable completed-epoch retirement failed: {error}",
                file=sys.stderr,
                flush=True,
            )
            comm.Abort(72)
            return

    # No rank can report a successful finalize before root has durably retired
    # the active epoch name. A root failure aborts ranks waiting in Ibarrier.
    if not _bounded_mpi_barrier(comm, finalize_grace):
        if rank == 0:
            print(
                "[Master] Post-cleanup MPI barrier deadline expired; "
                "aborting job.",
                file=sys.stderr,
                flush=True,
            )
        comm.Abort(73)
        return

    MPI.Finalize()


if __name__ == "__main__":
    main()
