"""Transport primitives for distributed hybrid symbolic execution.

The coordinator sends immutable inputs by digest and AFL coverage as versioned
sparse deltas.  Workers no longer need to dereference coordinator-local paths
or treat the AFL bitmap as a QSYM-internal pruning map.
"""

from __future__ import annotations

from array import array
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
import ctypes
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from typing import Any, Callable, Iterable, Mapping


_AT_FDCWD = -100
_RENAME_NOREPLACE = 1

try:
    _LIBC = ctypes.CDLL(None, use_errno=True)
    _RENAMEAT2 = _LIBC.renameat2
except (AttributeError, OSError):
    _RENAMEAT2 = None
else:
    _RENAMEAT2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    _RENAMEAT2.restype = ctypes.c_int


def fsync_directory(path: str) -> None:
    """Persist directory-entry updates or raise when durability is unknown."""
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path or ".", flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def durable_makedirs(path: str, *, exist_ok: bool = True) -> None:
    """Create a directory tree and persist every newly published component."""
    absolute = os.path.abspath(path)
    missing: list[str] = []
    cursor = absolute
    while not os.path.exists(cursor):
        missing.append(cursor)
        parent = os.path.dirname(cursor)
        if parent == cursor:
            break
        cursor = parent
    if not os.path.isdir(cursor):
        raise NotADirectoryError(cursor)
    if not missing:
        if not exist_ok:
            raise FileExistsError(absolute)
        return

    for directory in reversed(missing):
        try:
            os.mkdir(directory)
        except FileExistsError:
            if directory == absolute and not exist_ok:
                raise
            if not os.path.isdir(directory):
                raise NotADirectoryError(directory)
        fsync_directory(directory)
        fsync_directory(os.path.dirname(directory) or ".")


def durable_replace(
    source: str,
    destination: str,
    *,
    directory_fd: int | None = None,
) -> None:
    """Atomically replace a path and persist both sides of a rename."""
    if directory_fd is not None:
        if (
            os.path.dirname(source)
            or os.path.dirname(destination)
            or source in {"", ".", ".."}
            or destination in {"", ".", ".."}
        ):
            raise ValueError("descriptor-relative replace requires leaf names")
        os.replace(
            source,
            destination,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
        return
    source_parent = os.path.abspath(os.path.dirname(source) or ".")
    destination_parent = os.path.abspath(os.path.dirname(destination) or ".")
    os.replace(source, destination)
    # The public name must be durable before acknowledging removal from a
    # staging directory. This ordering makes cross-directory replay idempotent.
    fsync_directory(destination_parent)
    if source_parent != destination_parent:
        fsync_directory(source_parent)


def durable_rename_noreplace(source: str, destination: str) -> None:
    """Atomically rename without clobbering and persist both pathnames.

    Linux ``renameat2(RENAME_NOREPLACE)`` is required. Falling back to a
    check followed by ``rename`` would reopen the destination race that this
    helper is intended to close.
    """
    if _RENAMEAT2 is None:
        raise OSError(
            errno.EOPNOTSUPP,
            "renameat2(RENAME_NOREPLACE) is unavailable",
            destination,
        )
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    if b"\0" in source_bytes or b"\0" in destination_bytes:
        raise ValueError("embedded null byte in rename pathname")

    ctypes.set_errno(0)
    result = _RENAMEAT2(
        _AT_FDCWD,
        source_bytes,
        _AT_FDCWD,
        destination_bytes,
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error_number = ctypes.get_errno() or errno.EIO
        raise OSError(
            error_number,
            os.strerror(error_number),
            destination,
        )

    source_parent = os.path.abspath(os.path.dirname(source) or ".")
    destination_parent = os.path.abspath(os.path.dirname(destination) or ".")
    fsync_directory(destination_parent)
    if source_parent != destination_parent:
        fsync_directory(source_parent)


def durable_link(source: str, destination: str) -> None:
    """Publish a hard link and persist its destination directory entry."""
    os.link(source, destination)
    fsync_directory(os.path.dirname(destination) or ".")


def durable_unlink(path: str) -> None:
    """Remove a path and persist the deletion before returning."""
    os.unlink(path)
    fsync_directory(os.path.dirname(path) or ".")


def durable_rmtree(path: str) -> None:
    """Remove a directory tree and persist disappearance of its root name."""
    absolute = os.path.abspath(path)
    parent = os.path.dirname(absolute)
    if absolute == parent:
        raise ValueError("refusing to remove a filesystem root")
    shutil.rmtree(absolute)
    fsync_directory(parent)


@dataclass(frozen=True)
class DurableRmtreeStepResult:
    """Durable progress made by one bounded directory-tree deletion step."""

    removed_entries: int
    complete: bool
    stop_reason: str


@dataclass
class _RmtreeDirectoryFrame:
    descriptor: int
    iterator: Any
    name: str
    dirty: bool = False


def durable_rmtree_step(
    path: str,
    *,
    entry_limit: int,
    time_limit: float,
    _clock: Any = time.monotonic,
) -> DurableRmtreeStepResult:
    """Durably remove a bounded part of a tree without following symlinks.

    ``entry_limit`` is a hard bound on successful unlink/rmdir mutations. The
    time limit is cooperative: it is checked between filesystem operations,
    and one mutation is allowed before it can stop a step so repeated calls
    cannot livelock on a deep tree. Individual filesystem calls are not
    preemptible and can therefore exceed the requested duration.
    """
    if type(entry_limit) is not int or entry_limit <= 0:
        raise ValueError("rmtree step entry limit must be a positive integer")
    try:
        duration = float(time_limit)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(
            "rmtree step time limit must be finite and positive"
        ) from error
    if not math.isfinite(duration) or duration <= 0.0:
        raise ValueError("rmtree step time limit must be finite and positive")
    if not callable(_clock):
        raise TypeError("rmtree step clock must be callable")

    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise OSError(
            errno.EOPNOTSUPP,
            "descriptor-relative no-follow directory traversal is unavailable",
            path,
        )

    absolute = os.path.abspath(path)
    lexical_parent = os.path.dirname(absolute)
    if absolute == lexical_parent:
        raise ValueError("refusing to remove a filesystem root")
    root_name = os.path.basename(absolute)
    parent_path = os.path.realpath(lexical_parent)
    flags = os.O_RDONLY | directory | nofollow
    flags |= getattr(os, "O_CLOEXEC", 0)

    started = float(_clock())
    if not math.isfinite(started):
        raise ValueError("rmtree step clock returned a non-finite value")
    deadline = started + duration
    if not math.isfinite(deadline):
        raise ValueError("rmtree step deadline is not finite")
    parent_descriptor = os.open(parent_path, flags)
    frames: list[_RmtreeDirectoryFrame] = []
    parent_dirty = False
    removed_entries = 0
    complete = False
    stop_reason = "entry-limit"

    try:
        root_descriptor = os.open(
            root_name,
            flags,
            dir_fd=parent_descriptor,
        )
        try:
            root_iterator = os.scandir(root_descriptor)
        except BaseException:
            os.close(root_descriptor)
            raise
        frames.append(
            _RmtreeDirectoryFrame(
                descriptor=root_descriptor,
                iterator=root_iterator,
                name=root_name,
            )
        )

        while frames:
            if removed_entries >= entry_limit:
                stop_reason = "entry-limit"
                break
            now = float(_clock())
            if not math.isfinite(now):
                raise ValueError("rmtree step clock returned a non-finite value")
            if removed_entries > 0 and now >= deadline:
                stop_reason = "time-limit"
                break

            frame = frames[-1]
            try:
                entry = next(frame.iterator)
            except StopIteration:
                now = float(_clock())
                if not math.isfinite(now):
                    raise ValueError("rmtree step clock returned a non-finite value")
                if removed_entries > 0 and now >= deadline:
                    stop_reason = "time-limit"
                    break

                removal_parent = (
                    parent_descriptor if len(frames) == 1 else frames[-2].descriptor
                )
                try:
                    os.rmdir(frame.name, dir_fd=removal_parent)
                except OSError as error:
                    if error.errno != errno.ENOTEMPTY:
                        raise
                    # Directory iteration while deleting entries can legally
                    # miss an entry. Reopen the same descriptor and continue.
                    frame.iterator.close()
                    frame.iterator = os.scandir(frame.descriptor)
                    continue

                if len(frames) == 1:
                    removed_entries += 1
                    parent_dirty = True
                    complete = True
                    stop_reason = "complete"
                else:
                    parent_frame = frames[-2]
                    removed_entries += 1
                    parent_frame.dirty = True
                frame.iterator.close()
                os.close(frame.descriptor)
                frames.pop()
                if complete:
                    break
                continue

            if entry.is_dir(follow_symlinks=False):
                child_descriptor = os.open(
                    entry.name,
                    flags,
                    dir_fd=frame.descriptor,
                )
                try:
                    child_iterator = os.scandir(child_descriptor)
                except BaseException:
                    os.close(child_descriptor)
                    raise
                frames.append(
                    _RmtreeDirectoryFrame(
                        descriptor=child_descriptor,
                        iterator=child_iterator,
                        name=entry.name,
                    )
                )
                continue

            now = float(_clock())
            if not math.isfinite(now):
                raise ValueError("rmtree step clock returned a non-finite value")
            if removed_entries > 0 and now >= deadline:
                stop_reason = "time-limit"
                break
            os.unlink(entry.name, dir_fd=frame.descriptor)
            removed_entries += 1
            frame.dirty = True
    finally:
        # Sync deepest surviving directories first. A directory removed during
        # this step needs only its surviving parent to make the unlink durable.
        cleanup_error: BaseException | None = None
        for frame in reversed(frames):
            if frame.dirty:
                try:
                    os.fsync(frame.descriptor)
                except BaseException as error:
                    cleanup_error = cleanup_error or error
        if parent_dirty:
            try:
                os.fsync(parent_descriptor)
            except BaseException as error:
                cleanup_error = cleanup_error or error
        for frame in reversed(frames):
            try:
                frame.iterator.close()
            except BaseException as error:
                cleanup_error = cleanup_error or error
            try:
                os.close(frame.descriptor)
            except BaseException as error:
                cleanup_error = cleanup_error or error
        try:
            os.close(parent_descriptor)
        except BaseException as error:
            cleanup_error = cleanup_error or error
        if cleanup_error is not None:
            raise cleanup_error

    return DurableRmtreeStepResult(
        removed_entries=removed_entries,
        complete=complete,
        stop_reason=stop_reason,
    )


def _finite_duration(value: Any, minimum: float, name: str) -> float:
    try:
        duration = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{name} must be a finite number") from error
    if not math.isfinite(duration):
        raise ValueError(f"{name} must be a finite number")
    return max(minimum, duration)


def _finite_timestamp(value: Any, name: str) -> float:
    try:
        timestamp = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{name} must be finite and non-negative") from error
    if not math.isfinite(timestamp) or timestamp < 0.0:
        raise ValueError(f"{name} must be finite and non-negative")
    return timestamp


def _json_object_without_duplicates(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object member {key!r}")
        value[key] = item
    return value


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r} is not supported")


def _atomic_publish_bytes(path: str, content: bytes, description: str) -> None:
    """Durably replace a file from a private, non-following temporary inode."""
    temporary = (
        f"{path}.{os.getpid()}.{time.time_ns()}.{os.urandom(8).hex()}.tmp"
    )
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError(f"O_NOFOLLOW is required for {description} writes")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0) | nofollow,
        0o600,
    )
    published = False
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError(f"short {description} write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        durable_replace(temporary, path)
        published = True
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not published:
            try:
                os.unlink(temporary)
            except OSError:
                pass


class SharedFilesystemCapabilityError(RuntimeError):
    """The shared-state root cannot satisfy a required storage contract."""


_FILESYSTEM_OPERATION_NAMES = (
    "file_fsync",
    "directory_fsync",
    "same_directory_replace",
    "cross_directory_replace",
    "publication_replace",
    "hard_link",
    "durable_unlink",
    "advisory_lock_exclusion",
    "advisory_lock_release",
)


@dataclass(frozen=True)
class SharedFilesystemRequirementProfile:
    """Exact filesystem operations required by one shared-state protocol."""

    name: str
    file_fsync: bool = True
    directory_fsync: bool = True
    same_directory_replace: bool = True
    cross_directory_replace: bool = True
    publication_replace: bool = True
    hard_link: bool = True
    durable_unlink: bool = True
    advisory_lock_exclusion: bool = True
    advisory_lock_release: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9-]{0,63}", self.name
        ):
            raise ValueError("invalid shared filesystem requirement profile")
        for operation in _FILESYSTEM_OPERATION_NAMES:
            if type(getattr(self, operation)) is not bool:
                raise TypeError(
                    f"shared filesystem requirement {operation} must be bool"
                )
        if not self.file_fsync or not self.directory_fsync:
            raise ValueError(
                "shared state profiles must require file and directory fsync"
            )
        if self.advisory_lock_exclusion != self.advisory_lock_release:
            raise ValueError(
                "advisory lock exclusion and release must be qualified together"
            )

    @property
    def required_operations(self) -> tuple[str, ...]:
        return tuple(
            name for name in _FILESYSTEM_OPERATION_NAMES if bool(getattr(self, name))
        )

    @property
    def unverified_operations(self) -> tuple[str, ...]:
        required = set(self.required_operations)
        return tuple(
            name for name in _FILESYSTEM_OPERATION_NAMES if name not in required
        )


FULL_SHARED_FILESYSTEM_REQUIREMENTS = SharedFilesystemRequirementProfile(
    "full-shared-state-v1"
)
LEASE_SHARED_FILESYSTEM_REQUIREMENTS = SharedFilesystemRequirementProfile(
    "lease-table-v1",
    cross_directory_replace=False,
    publication_replace=False,
    hard_link=False,
)
COVERAGE_SHARED_FILESYSTEM_REQUIREMENTS = SharedFilesystemRequirementProfile(
    "coverage-gossip-v1",
    cross_directory_replace=False,
    publication_replace=False,
    hard_link=False,
    durable_unlink=False,
)


def merge_shared_filesystem_requirements(
    name: str,
    *profiles: SharedFilesystemRequirementProfile,
) -> SharedFilesystemRequirementProfile:
    """Build the exact operation union for protocols sharing one root."""
    if not profiles:
        raise ValueError("at least one shared filesystem profile is required")
    for profile in profiles:
        if not isinstance(profile, SharedFilesystemRequirementProfile):
            raise TypeError("shared filesystem profiles must be requirement profiles")
    return SharedFilesystemRequirementProfile(
        name,
        **{
            operation: any(getattr(profile, operation) for profile in profiles)
            for operation in _FILESYSTEM_OPERATION_NAMES
        },
    )


@dataclass(frozen=True)
class SharedFilesystemCapabilities:
    """Observed host-process capabilities for one shared-state root."""

    root: str
    publication_root: str
    device: int
    filesystem_id: int
    publication_device: int
    publication_filesystem_id: int
    filesystem_type: str
    mount_point: str
    mount_source: str
    distributed_filesystem: bool
    requirement_profile: str = "full-shared-state-v1"
    required_operations: tuple[str, ...] = _FILESYSTEM_OPERATION_NAMES
    cluster_lock_verified: bool = False
    probe_scope: str = "same-host-subprocess-v1"
    cluster_lock_members: tuple[tuple[int, str], ...] = ()
    cluster_lock_representatives: tuple[int, ...] = ()
    cluster_lock_rounds: int = 0
    cluster_lock_contention_checks: int = 0
    cluster_lock_release_checks: int = 0
    cluster_lock_identity_checks: int = 0
    file_fsync: bool | None = True
    directory_fsync: bool | None = True
    same_directory_replace: bool | None = True
    cross_directory_replace: bool | None = True
    publication_replace: bool | None = True
    hard_link: bool | None = True
    durable_unlink: bool | None = True
    advisory_lock_exclusion: bool | None = True
    advisory_lock_release: bool | None = True

    def snapshot(self) -> dict[str, Any]:
        operations = {name: getattr(self, name) for name in _FILESYSTEM_OPERATION_NAMES}
        partial = any(value is None for value in operations.values())
        cluster_identity_closed = self.probe_scope == "cross-host-mpi-lock-v2"
        snapshot = {
            "schema": (
                "symcc-shared-filesystem-capabilities-v3"
                if cluster_identity_closed
                else "symcc-shared-filesystem-capabilities-v2"
                if partial
                else "symcc-shared-filesystem-capabilities-v1"
            ),
            "root": self.root,
            "publication_root": self.publication_root,
            "device": self.device,
            "filesystem_id": self.filesystem_id,
            "publication_device": self.publication_device,
            "publication_filesystem_id": self.publication_filesystem_id,
            "filesystem_type": self.filesystem_type,
            "mount_point": self.mount_point,
            "mount_source": self.mount_source,
            "distributed_filesystem": self.distributed_filesystem,
            "cluster_lock_verified": self.cluster_lock_verified,
            "probe_scope": self.probe_scope,
            "cluster_lock_members": [
                {"rank": rank, "processor": processor}
                for rank, processor in self.cluster_lock_members
            ],
            "cluster_lock_representatives": list(self.cluster_lock_representatives),
            "cluster_lock_rounds": self.cluster_lock_rounds,
            "cluster_lock_contention_checks": (self.cluster_lock_contention_checks),
            "cluster_lock_release_checks": self.cluster_lock_release_checks,
        }
        if cluster_identity_closed:
            snapshot["cluster_lock_identity_checks"] = self.cluster_lock_identity_checks
        if partial:
            required = set(self.required_operations)
            snapshot.update(
                {
                    "requirement_profile": self.requirement_profile,
                    "required_operations": list(self.required_operations),
                    "unverified_operations": [
                        name
                        for name in _FILESYSTEM_OPERATION_NAMES
                        if name not in required
                    ],
                }
            )
        snapshot.update(operations)
        return snapshot


def qualify_shared_filesystem_cluster_lock(
    capability: SharedFilesystemCapabilities,
    *,
    members: Iterable[tuple[int, str]],
    representatives: Iterable[int],
    rounds: int,
    contention_checks: int,
    release_checks: int,
    identity_checks: int,
) -> SharedFilesystemCapabilities:
    """Return a capability upgraded by a complete cross-host lock litmus.

    This function validates evidence shape only. The MPI protocol that produces
    the evidence lives in ``mpi_filesystem_qualification``; keeping the upgrade
    gate here prevents callers from constructing an incoherent capability
    snapshot accidentally.
    """
    if not isinstance(capability, SharedFilesystemCapabilities):
        raise TypeError("cluster lock qualification requires filesystem capabilities")
    if (
        capability.advisory_lock_exclusion is not True
        or capability.advisory_lock_release is not True
    ):
        raise ValueError(
            "cluster lock qualification requires local lock exclusion and release"
        )

    normalized_members: list[tuple[int, str]] = []
    seen_ranks: set[int] = set()
    for raw_rank, raw_processor in members:
        if type(raw_rank) is not int or raw_rank < 0 or raw_rank in seen_ranks:
            raise ValueError("invalid cluster lock member rank")
        if (
            not isinstance(raw_processor, str)
            or not raw_processor
            or len(raw_processor) > 255
            or "\x00" in raw_processor
        ):
            raise ValueError("invalid cluster lock processor identity")
        seen_ranks.add(raw_rank)
        normalized_members.append((raw_rank, raw_processor))
    normalized_members.sort()
    if len(normalized_members) < 2:
        raise ValueError("cluster lock qualification requires multiple masters")

    processors = {processor for _, processor in normalized_members}
    if len(processors) < 2:
        raise ValueError("cluster lock qualification requires multiple processors")

    normalized_representatives = tuple(representatives)
    if (
        any(type(rank) is not int for rank in normalized_representatives)
        or len(set(normalized_representatives)) != len(normalized_representatives)
        or any(rank not in seen_ranks for rank in normalized_representatives)
    ):
        raise ValueError("invalid cluster lock representatives")
    member_processors = dict(normalized_members)
    if len(normalized_representatives) != len(processors) or len(
        {member_processors[rank] for rank in normalized_representatives}
    ) != len(processors):
        raise ValueError(
            "cluster lock representatives must cover every processor exactly once"
        )

    expected_rounds = len(normalized_representatives)
    expected_contention = expected_rounds * (len(normalized_members) - 1)
    if type(rounds) is not int or rounds != expected_rounds:
        raise ValueError("cluster lock round count is incomplete")
    if type(contention_checks) is not int or contention_checks != expected_contention:
        raise ValueError("cluster lock contention evidence is incomplete")
    if type(release_checks) is not int or release_checks != expected_rounds:
        raise ValueError("cluster lock release evidence is incomplete")
    if type(identity_checks) is not int or identity_checks != len(normalized_members):
        raise ValueError("cluster lock namespace identity evidence is incomplete")

    return replace(
        capability,
        cluster_lock_verified=True,
        probe_scope="cross-host-mpi-lock-v2",
        cluster_lock_members=tuple(normalized_members),
        cluster_lock_representatives=normalized_representatives,
        cluster_lock_rounds=rounds,
        cluster_lock_contention_checks=contention_checks,
        cluster_lock_release_checks=release_checks,
        cluster_lock_identity_checks=identity_checks,
    )


_LOCK_PROBE_CHILD = r"""
import errno
import fcntl
import os
import sys

descriptor = os.open(sys.argv[1], os.O_RDWR)
result = 0
try:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EAGAIN}:
            result = 73
        else:
            print(f"flock errno={error.errno}", file=sys.stderr)
            result = 74
finally:
    os.close(descriptor)
raise SystemExit(result)
"""


def _unescape_mountinfo_path(value: str) -> str:
    return re.sub(
        r"\\([0-7]{3})",
        lambda match: chr(int(match.group(1), 8)),
        value,
    )


def _mountinfo_for_path(path: str) -> tuple[str, str, str]:
    """Return the deepest Linux mount point, filesystem type, and source."""
    resolved = os.path.realpath(path)
    best: tuple[int, str, str, str] | None = None
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as stream:
            lines = tuple(stream)
    except OSError:
        return "", "unknown", "unknown"
    for raw in lines:
        try:
            left, right = raw.rstrip("\n").split(" - ", 1)
            left_fields = left.split()
            right_fields = right.split()
            if len(left_fields) < 6 or len(right_fields) < 2:
                continue
            mount_point = _unescape_mountinfo_path(left_fields[4])
            filesystem_type = right_fields[0]
            source = _unescape_mountinfo_path(right_fields[1])
        except (IndexError, ValueError):
            continue
        prefix = mount_point.rstrip(os.sep) + os.sep
        if resolved != mount_point and not resolved.startswith(prefix):
            continue
        candidate = (len(mount_point), mount_point, filesystem_type, source)
        if best is None or candidate[0] > best[0]:
            best = candidate
    if best is None:
        return "", "unknown", "unknown"
    return best[1], best[2], best[3]


def _is_distributed_filesystem(filesystem_type: str) -> bool:
    normalized = filesystem_type.lower()
    return normalized in {
        "9p",
        "afs",
        "ceph",
        "cifs",
        "gfs2",
        "glusterfs",
        "gpfs",
        "lustre",
        "nfs",
        "nfs4",
        "ocfs2",
        "smb2",
        "smb3",
    } or normalized.startswith(("fuse.ceph", "fuse.glusterfs", "fuse.sshfs"))


def _write_probe_file(path: str, content: bytes) -> None:
    with open(path, "xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _remove_probe_tree(path: str) -> None:
    if not os.path.lexists(path):
        return
    for directory, subdirectories, files in os.walk(path, topdown=False):
        for name in files:
            os.unlink(os.path.join(directory, name))
        for name in subdirectories:
            os.rmdir(os.path.join(directory, name))
    os.rmdir(path)
    fsync_directory(os.path.dirname(path) or ".")


def _run_lock_probe_child(
    path: str, timeout: float
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [sys.executable, "-I", "-c", _LOCK_PROBE_CHILD, path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise SharedFilesystemCapabilityError(
            "cross-process advisory-lock probe timed out"
        ) from error


def probe_shared_state_filesystem(
    root: str,
    *,
    timeout: float = 5.0,
    publication_root: str | None = None,
    requirements: SharedFilesystemRequirementProfile = (
        FULL_SHARED_FILESYSTEM_REQUIREMENTS
    ),
) -> SharedFilesystemCapabilities:
    """Fail fast unless ``root`` supports the shared-state storage contract.

    The subprocess checks exclusion and descriptor-close release between two
    processes on this host. It does not prove that a remote NFS/CIFS/Lustre
    client participates in the same lock domain; cross-host deployments still
    require a coordinated cluster probe.
    """
    if not isinstance(requirements, SharedFilesystemRequirementProfile):
        raise TypeError("shared filesystem requirements must be a requirement profile")
    timeout = min(
        60.0, _finite_duration(timeout, 0.001, "shared filesystem probe timeout")
    )
    root = os.path.abspath(root)
    publication_root = os.path.abspath(publication_root or root)
    probe = os.path.join(
        root,
        f".symcc-fs-probe-{os.getpid()}-{time.monotonic_ns()}-{os.urandom(8).hex()}",
    )
    publication_probe = os.path.join(
        publication_root,
        f".symcc-fs-publication-probe-{os.getpid()}-"
        f"{time.monotonic_ns()}-{os.urandom(8).hex()}",
    )
    stage = "create probe root"
    failure: BaseException | None = None
    result: SharedFilesystemCapabilities | None = None
    try:
        durable_makedirs(root)
        durable_makedirs(publication_root)
        durable_makedirs(probe, exist_ok=False)
        durable_makedirs(publication_probe, exist_ok=False)
        left = os.path.join(probe, "left")
        right = os.path.join(probe, "right")
        durable_makedirs(left, exist_ok=False)
        durable_makedirs(right, exist_ok=False)

        stage = "file fsync"
        current = os.path.join(left, "current")
        _write_probe_file(current, b"old-state\n")
        fsync_directory(left)
        if requirements.same_directory_replace:
            stage = "file fsync and same-directory replace"
            replacement = os.path.join(left, "replacement.tmp")
            _write_probe_file(replacement, b"new-state\n")
            durable_replace(replacement, current)
            with open(current, "rb") as stream:
                if stream.read() != b"new-state\n":
                    raise OSError(errno.EIO, "same-directory replace lost data")

        if requirements.cross_directory_replace:
            stage = "cross-directory replace"
            cross_source = os.path.join(left, "cross.tmp")
            cross_destination = os.path.join(right, "cross")
            _write_probe_file(cross_source, b"cross-directory\n")
            durable_replace(cross_source, cross_destination)
            with open(cross_destination, "rb") as stream:
                if stream.read() != b"cross-directory\n":
                    raise OSError(errno.EIO, "cross-directory replace lost data")

        publication_destination = os.path.join(publication_probe, "published")
        if requirements.publication_replace:
            stage = "configured publication-boundary replace"
            publication_source = os.path.join(left, "publication.tmp")
            _write_probe_file(publication_source, b"published-object\n")
            durable_replace(publication_source, publication_destination)
            with open(publication_destination, "rb") as stream:
                if stream.read() != b"published-object\n":
                    raise OSError(errno.EIO, "publication replace lost data")

        linked = os.path.join(right, "linked")
        if requirements.hard_link:
            stage = "hard link"
            durable_link(current, linked)
            current_stat = os.stat(current, follow_symlinks=False)
            linked_stat = os.stat(linked, follow_symlinks=False)
            if current_stat.st_ino != linked_stat.st_ino:
                raise OSError(errno.EIO, "hard link did not preserve inode")

        if requirements.durable_unlink:
            stage = "durable unlink"
            unlink_target = (
                publication_destination
                if requirements.publication_replace
                else linked
                if requirements.hard_link
                else os.path.join(right, "unlink-target")
            )
            if not os.path.exists(unlink_target):
                _write_probe_file(unlink_target, b"durable-unlink\n")
                fsync_directory(os.path.dirname(unlink_target) or ".")
            durable_unlink(unlink_target)

        if requirements.advisory_lock_exclusion:
            stage = "cross-process advisory-lock exclusion"
            lock_path = os.path.join(probe, "record.lock")
            lock_flags = os.O_RDWR | os.O_CREAT
            lock_flags |= getattr(os, "O_CLOEXEC", 0)
            lock_flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(lock_path, lock_flags, 0o600)
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise OSError(
                        errno.EINVAL,
                        "probe lock path is not regular",
                        lock_path,
                    )
                os.fsync(descriptor)
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                blocked = _run_lock_probe_child(lock_path, timeout)
                if blocked.returncode != 73:
                    detail = blocked.stderr.strip() or str(blocked.returncode)
                    raise OSError(
                        errno.ENOLCK,
                        f"advisory lock did not exclude a child process: {detail}",
                    )
            finally:
                os.close(descriptor)

            stage = "cross-process advisory-lock release"
            released = _run_lock_probe_child(lock_path, timeout)
            if released.returncode != 0:
                detail = released.stderr.strip() or str(released.returncode)
                raise OSError(
                    errno.ENOLCK,
                    f"closed advisory lock was not released: {detail}",
                )

        resolved_root = os.path.realpath(root)
        resolved_publication_root = os.path.realpath(publication_root)
        stat_result = os.stat(resolved_root, follow_symlinks=False)
        statvfs_result = os.statvfs(resolved_root)
        publication_stat = os.stat(resolved_publication_root, follow_symlinks=False)
        publication_statvfs = os.statvfs(resolved_publication_root)
        mount_point, filesystem_type, mount_source = _mountinfo_for_path(root)
        result = SharedFilesystemCapabilities(
            root=resolved_root,
            publication_root=resolved_publication_root,
            device=int(stat_result.st_dev),
            filesystem_id=int(getattr(statvfs_result, "f_fsid", 0)),
            publication_device=int(publication_stat.st_dev),
            publication_filesystem_id=int(getattr(publication_statvfs, "f_fsid", 0)),
            filesystem_type=filesystem_type,
            mount_point=mount_point,
            mount_source=mount_source,
            distributed_filesystem=_is_distributed_filesystem(filesystem_type),
            requirement_profile=requirements.name,
            required_operations=requirements.required_operations,
            **{
                name: True if getattr(requirements, name) else None
                for name in _FILESYSTEM_OPERATION_NAMES
            },
        )
    except (OSError, ValueError, SharedFilesystemCapabilityError) as error:
        failure = error
    finally:
        try:
            _remove_probe_tree(probe)
        except OSError as error:
            if failure is None:
                stage = "probe cleanup"
                failure = error
        try:
            _remove_probe_tree(publication_probe)
        except OSError as error:
            if failure is None:
                stage = "publication probe cleanup"
                failure = error

    if failure is not None:
        if isinstance(failure, SharedFilesystemCapabilityError):
            raise failure
        raise SharedFilesystemCapabilityError(
            f"shared filesystem capability probe failed during {stage}: {failure}"
        ) from failure
    assert result is not None
    return result


@contextmanager
def _bounded_advisory_lock(
    path: str,
    *,
    timeout: float,
    description: str,
    age_hint: float | None = None,
    shared: bool = False,
):
    """Acquire a crash-released kernel lock without wall-clock stealing.

    The stable lock file is deliberately retained: unlinking a lock pathname
    can let a new opener lock a different inode while an old holder still owns
    the unlinked inode. The advisory lock itself is tied to this descriptor and
    is released by close, exception unwinding, or process exit.
    """
    durable_makedirs(os.path.dirname(path) or ".")
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise OSError(
            errno.EOPNOTSUPP,
            "O_NOFOLLOW is required for advisory lock identity",
            path,
        )
    flags |= no_follow
    descriptor = os.open(path, flags, 0o600)
    acquired = False
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(errno.EINVAL, "advisory lock path is not regular", path)
        deadline = time.monotonic() + timeout
        while True:
            try:
                lock_mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
                fcntl.flock(descriptor, lock_mode | fcntl.LOCK_NB)
                acquired = True
                break
            except InterruptedError:
                continue
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    hint = (
                        f"; configured age hint={age_hint:.3f}s"
                        if age_hint is not None
                        else ""
                    )
                    raise TimeoutError(
                        f"timed out acquiring {description}{hint}; "
                        "kernel advisory locks are never time-stolen"
                    ) from error
                time.sleep(min(0.002, remaining))
        yield
    finally:
        if acquired:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                # close() is the authoritative release operation.
                pass
        os.close(descriptor)


@contextmanager
def bounded_advisory_lock(
    path: str,
    *,
    timeout: float,
    description: str,
    shared: bool = False,
):
    """Expose a bounded shared/exclusive lock for shared-state protocols."""
    if not isinstance(shared, bool):
        raise TypeError("advisory lock shared mode must be Boolean")
    with _bounded_advisory_lock(
        path,
        timeout=timeout,
        description=description,
        shared=shared,
    ):
        yield


class _DirectorySyncBatch:
    """Defer directory-entry barriers without deferring file data.

    Replacements are still immediately visible and their files must already be
    fsynced by the caller. Cross-directory publication must use
    ``durable_replace`` so its destination-before-source ordering stays local
    to that rename and cannot be obscured by batch de-duplication.
    """

    def __init__(self) -> None:
        self._pending: dict[str, None] = {}

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def replace(self, source: str, destination: str) -> None:
        source_parent = os.path.abspath(os.path.dirname(source) or ".")
        destination_parent = os.path.abspath(os.path.dirname(destination) or ".")
        if source_parent != destination_parent:
            raise ValueError(
                "directory sync batches require same-directory replacement"
            )
        os.replace(source, destination)
        self._pending.setdefault(destination_parent, None)

    def unlink(self, path: str) -> None:
        parent = os.path.abspath(os.path.dirname(path) or ".")
        os.unlink(path)
        self._pending.setdefault(parent, None)

    def flush(self) -> int:
        synced = 0
        while self._pending:
            directory = next(iter(self._pending))
            fsync_directory(directory)
            del self._pending[directory]
            synced += 1
        return synced


@dataclass(frozen=True)
class LeaseHeartbeatBatch:
    """Outcome of one failure-atomic lease-heartbeat batch."""

    renewed: tuple[str, ...]
    lost: tuple[str, ...]
    directory_syncs: int


_STABLE_FILE_CHUNK_BYTES = 1024 * 1024
_INPUT_STORE_IDENTITY_CACHE_CAP = 300_000
_INPUT_STORE_PUBLICATION_VERIFY_ATTEMPTS = 32


@dataclass(frozen=True)
class StableRegularFileIdentity:
    """Metadata identity used to close a path/open-file snapshot."""

    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def from_stat(cls, metadata: os.stat_result) -> "StableRegularFileIdentity":
        return cls(
            int(metadata.st_dev),
            int(metadata.st_ino),
            int(metadata.st_size),
            int(metadata.st_mtime_ns),
            int(metadata.st_ctime_ns),
        )


@dataclass(frozen=True)
class StableRegularFileSnapshot:
    sha256: str
    identity: StableRegularFileIdentity
    content: bytes | None = None


@dataclass(frozen=True)
class ContentAddressedObjectObservation:
    """One canonical CAS leaf observed through an anchored shard descriptor."""

    object_id: str
    identity: StableRegularFileIdentity


@dataclass(frozen=True)
class ContentAddressedObjectInventory:
    """Bounded physical CAS inventory; incomplete scans are never exhaustive."""

    objects: tuple[ContentAddressedObjectObservation, ...]
    scanned_entries: int
    noncanonical_entries: int
    complete: bool


@dataclass(frozen=True)
class _ContentAddressedObjectDirectory:
    descriptor: int
    leaf: str
    public_path: str


def stable_regular_file_snapshot(
    path: str,
    *,
    max_bytes: int,
    retain_content: bool = False,
    directory_fd: int | None = None,
) -> StableRegularFileSnapshot:
    """Hash one bounded regular inode and prove its final path identity."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("stable file limit must be a positive integer")
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise OSError(
            errno.EOPNOTSUPP,
            "O_NOFOLLOW is required for stable file snapshots",
            path,
        )
    flags = (
        os.O_RDONLY
        | no_follow
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(path, flags, dir_fd=directory_fd)
    try:
        metadata_before = os.fstat(descriptor)
        if not stat.S_ISREG(metadata_before.st_mode):
            raise OSError(errno.EINVAL, "snapshot path is not regular", path)
        identity = StableRegularFileIdentity.from_stat(metadata_before)
        if identity.size > max_bytes:
            raise ValueError(f"input object exceeds {max_bytes} byte transport limit")

        digest = hashlib.sha256()
        retained = bytearray() if retain_content else None
        observed = 0
        while True:
            try:
                chunk = os.read(
                    descriptor,
                    min(_STABLE_FILE_CHUNK_BYTES, max_bytes - observed + 1),
                )
            except InterruptedError:
                continue
            if not chunk:
                break
            observed += len(chunk)
            if observed > max_bytes:
                raise ValueError(
                    f"input object exceeds {max_bytes} byte transport limit"
                )
            digest.update(chunk)
            if retained is not None:
                retained.extend(chunk)

        metadata_after = os.fstat(descriptor)
        path_metadata = os.stat(
            path,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if (
            observed != identity.size
            or not stat.S_ISREG(path_metadata.st_mode)
            or StableRegularFileIdentity.from_stat(metadata_after) != identity
            or StableRegularFileIdentity.from_stat(path_metadata) != identity
        ):
            raise OSError(
                getattr(errno, "ESTALE", errno.EIO),
                "stable file identity changed while reading",
                path,
            )
        return StableRegularFileSnapshot(
            sha256=digest.hexdigest(),
            identity=identity,
            content=bytes(retained) if retained is not None else None,
        )
    finally:
        os.close(descriptor)


class ContentAddressedInputStore:
    """Atomic SHA-256 object store used on both coordinator and workers."""

    def __init__(
        self,
        root: str,
        max_object_bytes: int = 16 * 1024 * 1024,
        *,
        object_suffix: str = "",
        full_digest_leaf: bool = False,
    ):
        if (
            not isinstance(object_suffix, str)
            or os.path.sep in object_suffix
            or (os.path.altsep is not None and os.path.altsep in object_suffix)
            or "\0" in object_suffix
        ):
            raise ValueError("invalid content-addressed object suffix")
        if not isinstance(full_digest_leaf, bool):
            raise ValueError("full_digest_leaf must be Boolean")
        self._root_path = os.path.abspath(os.fspath(root))
        self.root = self._root_path
        self.max_object_bytes = max(1, max_object_bytes)
        self.object_suffix = object_suffix
        self.full_digest_leaf = full_digest_leaf
        self._verified_identities: dict[str, StableRegularFileIdentity] = {}
        root_descriptor = self._open_root_directory(create=True)
        try:
            self._verify_root_directory(root_descriptor)
        finally:
            os.close(root_descriptor)

    @staticmethod
    def _open_directory(
        path: str,
        *,
        directory_fd: int | None = None,
    ) -> int:
        no_follow = getattr(os, "O_NOFOLLOW", None)
        directory = getattr(os, "O_DIRECTORY", None)
        if no_follow is None or directory is None:
            raise OSError(
                errno.EOPNOTSUPP,
                "descriptor-anchored CAS directories require O_NOFOLLOW "
                "and O_DIRECTORY",
                path,
            )
        return os.open(
            path,
            os.O_RDONLY | no_follow | directory | getattr(os, "O_CLOEXEC", 0),
            dir_fd=directory_fd,
        )

    @staticmethod
    def _directory_identity(
        metadata: os.stat_result,
        path: str,
    ) -> tuple[int, int]:
        if not stat.S_ISDIR(metadata.st_mode):
            raise OSError(
                errno.ENOTDIR,
                "CAS directory anchor is not a directory",
                path,
            )
        return int(metadata.st_dev), int(metadata.st_ino)

    @staticmethod
    def _absolute_directory_components(path: str) -> tuple[str, ...]:
        if not isinstance(path, str):
            raise TypeError("content-addressed store root must be a string path")
        if "\0" in path:
            raise ValueError("embedded null byte in CAS root pathname")
        if not os.path.isabs(path):
            raise ValueError("CAS root pathname must be absolute")
        return tuple(component for component in path.split(os.path.sep) if component)

    def _open_root_directory(self, *, create: bool) -> int:
        """Walk the absolute CAS root without following any path component."""
        descriptor: int | None = self._open_directory(os.path.sep)
        try:
            for component in self._absolute_directory_components(self._root_path):
                created = False
                try:
                    child_descriptor = self._open_directory(
                        component,
                        directory_fd=descriptor,
                    )
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(component, 0o777, dir_fd=descriptor)
                        created = True
                    except FileExistsError:
                        pass
                    child_descriptor = self._open_directory(
                        component,
                        directory_fd=descriptor,
                    )
                try:
                    if created:
                        os.fsync(child_descriptor)
                        os.fsync(descriptor)
                except BaseException:
                    os.close(child_descriptor)
                    raise

                parent_descriptor = descriptor
                descriptor = child_descriptor
                os.close(parent_descriptor)

            result = descriptor
            descriptor = None
            return result
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _verify_root_directory(
        self,
        descriptor: int,
    ) -> tuple[int, int]:
        descriptor_identity = self._directory_identity(
            os.fstat(descriptor),
            self._root_path,
        )
        try:
            reopened_descriptor = self._open_root_directory(create=False)
        except OSError as error:
            if error.errno in {errno.ENOTDIR, errno.ELOOP}:
                raise NotADirectoryError(
                    errno.ENOTDIR,
                    "CAS root ancestry changed",
                    self._root_path,
                ) from error
            raise OSError(
                getattr(errno, "ESTALE", errno.EIO),
                "CAS root ancestry changed",
                self._root_path,
            ) from error
        try:
            reopened_identity = self._directory_identity(
                os.fstat(reopened_descriptor),
                self._root_path,
            )
        finally:
            os.close(reopened_descriptor)
        if reopened_identity != descriptor_identity:
            raise OSError(
                getattr(errno, "ESTALE", errno.EIO),
                "CAS root ancestry changed",
                self._root_path,
            )
        return descriptor_identity

    @classmethod
    def _verify_directory_path(
        cls,
        descriptor: int,
        path: str,
        *,
        directory_fd: int | None = None,
    ) -> tuple[int, int]:
        descriptor_identity = cls._directory_identity(os.fstat(descriptor), path)
        path_identity = cls._directory_identity(
            os.stat(
                path,
                dir_fd=directory_fd,
                follow_symlinks=False,
            ),
            path,
        )
        if path_identity != descriptor_identity:
            raise OSError(
                getattr(errno, "ESTALE", errno.EIO),
                "CAS directory identity changed",
                path,
            )
        return descriptor_identity

    @contextmanager
    def _object_directory(
        self,
        object_id: str,
        *,
        create: bool,
    ):
        public_path = self.object_path(object_id)
        shard = object_id[:2]
        leaf = self._object_leaf(object_id)
        root_descriptor = self._open_root_directory(create=False)
        shard_descriptor: int | None = None
        try:
            root_identity = self._directory_identity(
                os.fstat(root_descriptor),
                self._root_path,
            )
            created = False
            if create:
                try:
                    os.mkdir(shard, 0o700, dir_fd=root_descriptor)
                    created = True
                except FileExistsError:
                    pass
            shard_descriptor = self._open_directory(
                shard,
                directory_fd=root_descriptor,
            )
            shard_identity = self._verify_directory_path(
                shard_descriptor,
                shard,
                directory_fd=root_descriptor,
            )
            if created:
                os.fsync(shard_descriptor)
                os.fsync(root_descriptor)
            yield _ContentAddressedObjectDirectory(
                shard_descriptor,
                leaf,
                public_path,
            )
            if self._verify_root_directory(root_descriptor) != root_identity:
                raise OSError(
                    getattr(errno, "ESTALE", errno.EIO),
                    "CAS root identity changed",
                    self._root_path,
                )
            if (
                self._verify_directory_path(
                    shard_descriptor, shard, directory_fd=root_descriptor
                )
                != shard_identity
            ):
                raise OSError(
                    getattr(errno, "ESTALE", errno.EIO),
                    "CAS shard identity changed",
                    public_path,
                )
        finally:
            if shard_descriptor is not None:
                os.close(shard_descriptor)
            os.close(root_descriptor)

    @staticmethod
    def digest(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    def _object_leaf(self, object_id: str) -> str:
        digest = object_id if self.full_digest_leaf else object_id[2:]
        return digest + self.object_suffix

    def _object_id_from_leaf(self, shard: str, leaf: str) -> str | None:
        if self.object_suffix:
            if not leaf.endswith(self.object_suffix):
                return None
            digest_leaf = leaf[: -len(self.object_suffix)]
        else:
            digest_leaf = leaf
        object_id = digest_leaf if self.full_digest_leaf else shard + digest_leaf
        if (
            len(object_id) != 64
            or object_id[:2] != shard
            or any(char not in "0123456789abcdef" for char in object_id)
            or self._object_leaf(object_id) != leaf
        ):
            return None
        return object_id

    def object_path(self, object_id: str) -> str:
        if len(object_id) != 64 or any(
            char not in "0123456789abcdef" for char in object_id
        ):
            raise ValueError("invalid SHA-256 object id")
        return os.path.join(
            self.root,
            object_id[:2],
            self._object_leaf(object_id),
        )

    def _remember_verified_identity(
        self,
        object_id: str,
        identity: StableRegularFileIdentity,
    ) -> None:
        if (
            object_id not in self._verified_identities
            and len(self._verified_identities) >= _INPUT_STORE_IDENTITY_CACHE_CAP
        ):
            self._verified_identities.clear()
        self._verified_identities[object_id] = identity

    def _verified_object_in_directory(
        self,
        object_directory: _ContentAddressedObjectDirectory,
        object_id: str,
    ) -> StableRegularFileIdentity | None:
        try:
            metadata = os.stat(
                object_directory.leaf,
                dir_fd=object_directory.descriptor,
                follow_symlinks=False,
            )
            if not stat.S_ISREG(metadata.st_mode):
                self._verified_identities.pop(object_id, None)
                return None
            identity = StableRegularFileIdentity.from_stat(metadata)
            if self._verified_identities.get(object_id) == identity:
                return identity
            snapshot = stable_regular_file_snapshot(
                object_directory.leaf,
                max_bytes=self.max_object_bytes,
                directory_fd=object_directory.descriptor,
            )
        except (OSError, ValueError):
            self._verified_identities.pop(object_id, None)
            return None
        if snapshot.sha256 != object_id:
            self._verified_identities.pop(object_id, None)
            return None
        return snapshot.identity

    def _verified_object_after_publication(
        self,
        object_directory: _ContentAddressedObjectDirectory,
        object_id: str,
    ) -> StableRegularFileIdentity | None:
        for _ in range(_INPUT_STORE_PUBLICATION_VERIFY_ATTEMPTS):
            try:
                metadata = os.stat(
                    object_directory.leaf,
                    dir_fd=object_directory.descriptor,
                    follow_symlinks=False,
                )
                if not stat.S_ISREG(metadata.st_mode):
                    return None
                snapshot = stable_regular_file_snapshot(
                    object_directory.leaf,
                    max_bytes=self.max_object_bytes,
                    directory_fd=object_directory.descriptor,
                )
            except (OSError, ValueError):
                continue
            if snapshot.sha256 != object_id:
                return None
            return snapshot.identity
        return None

    def _verified_object(self, path: str, object_id: str) -> bool:
        if os.path.abspath(path) != os.path.abspath(self.object_path(object_id)):
            raise ValueError("object path does not match object id")
        try:
            with self._object_directory(object_id, create=False) as object_directory:
                identity = self._verified_object_in_directory(
                    object_directory, object_id
                )
        except (OSError, ValueError):
            self._verified_identities.pop(object_id, None)
            return False
        if identity is None:
            return False
        self._remember_verified_identity(object_id, identity)
        return True

    def put(self, content: bytes, object_id: str | None = None) -> tuple[str, str]:
        if len(content) > self.max_object_bytes:
            raise ValueError(
                f"input object is {len(content)} bytes; limit is {self.max_object_bytes}"
            )
        actual = self.digest(content)
        if object_id is not None and actual != object_id:
            raise ValueError("input object digest mismatch")
        object_id = actual
        path = self.object_path(object_id)
        if self._verified_object(path, object_id):
            return object_id, path
        identity: StableRegularFileIdentity | None = None
        try:
            with self._object_directory(object_id, create=True) as object_directory:
                tmp = f"{object_directory.leaf}.{os.getpid()}.{time.monotonic_ns()}.tmp"
                try:
                    flags = (
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0)
                    )
                    descriptor = os.open(
                        tmp,
                        flags,
                        0o600,
                        dir_fd=object_directory.descriptor,
                    )
                    with os.fdopen(descriptor, "wb") as stream:
                        view = memoryview(content)
                        offset = 0
                        while offset < len(view):
                            written = stream.write(view[offset:])
                            if written is None or written <= 0:
                                raise OSError(
                                    errno.EIO,
                                    "short input-object write",
                                    object_directory.public_path,
                                )
                            offset += written
                        stream.flush()
                        os.fsync(stream.fileno())
                        written_identity = StableRegularFileIdentity.from_stat(
                            os.fstat(stream.fileno())
                        )
                        durable_replace(
                            tmp,
                            object_directory.leaf,
                            directory_fd=object_directory.descriptor,
                        )
                        published_identity = StableRegularFileIdentity.from_stat(
                            os.fstat(stream.fileno())
                        )
                        metadata = os.stat(
                            object_directory.leaf,
                            dir_fd=object_directory.descriptor,
                            follow_symlinks=False,
                        )
                        path_identity = StableRegularFileIdentity.from_stat(metadata)
                        descriptor_content_changed = (
                            published_identity.device != written_identity.device
                            or published_identity.inode != written_identity.inode
                            or published_identity.size != written_identity.size
                            or published_identity.mtime_ns != written_identity.mtime_ns
                        )
                        if (
                            descriptor_content_changed
                            or not stat.S_ISREG(metadata.st_mode)
                            or path_identity != published_identity
                        ):
                            # Correct concurrent writers may converge inside the
                            # same descriptor-anchored shard.
                            self._verified_identities.pop(object_id, None)
                            identity = self._verified_object_after_publication(
                                object_directory, object_id
                            )
                            if identity is None:
                                raise OSError(
                                    getattr(errno, "ESTALE", errno.EIO),
                                    "input object changed during publication",
                                    path,
                                )
                        else:
                            identity = published_identity
                finally:
                    try:
                        os.unlink(
                            tmp,
                            dir_fd=object_directory.descriptor,
                        )
                    except OSError:
                        pass
        except (OSError, ValueError):
            self._verified_identities.pop(object_id, None)
            raise
        assert identity is not None
        self._remember_verified_identity(object_id, identity)
        return object_id, path

    def scan_objects(self, *, max_entries: int) -> ContentAddressedObjectInventory:
        """Inventory canonical regular leaves without following namespace links.

        The entry budget includes root entries and shard entries. A partial
        inventory is explicitly marked incomplete and must not be interpreted
        as proof that an unobserved object is absent.
        """
        if (
            isinstance(max_entries, bool)
            or not isinstance(max_entries, int)
            or max_entries < 1
        ):
            raise ValueError("CAS inventory max_entries must be a positive integer")

        observations: list[ContentAddressedObjectObservation] = []
        scanned_entries = 0
        noncanonical_entries = 0
        complete = True
        root_descriptor = self._open_root_directory(create=False)
        try:
            root_identity = self._directory_identity(
                os.fstat(root_descriptor), self._root_path
            )
            stop = False
            with os.scandir(root_descriptor) as shard_entries:
                for shard_entry in shard_entries:
                    if scanned_entries >= max_entries:
                        complete = False
                        break
                    scanned_entries += 1
                    shard = shard_entry.name
                    if (
                        len(shard) != 2
                        or any(char not in "0123456789abcdef" for char in shard)
                        or not shard_entry.is_dir(follow_symlinks=False)
                    ):
                        noncanonical_entries += 1
                        continue

                    shard_descriptor = self._open_directory(
                        shard,
                        directory_fd=root_descriptor,
                    )
                    try:
                        shard_identity = self._verify_directory_path(
                            shard_descriptor,
                            shard,
                            directory_fd=root_descriptor,
                        )
                        with os.scandir(shard_descriptor) as object_entries:
                            for object_entry in object_entries:
                                if scanned_entries >= max_entries:
                                    complete = False
                                    stop = True
                                    break
                                scanned_entries += 1
                                object_id = self._object_id_from_leaf(
                                    shard, object_entry.name
                                )
                                metadata = os.stat(
                                    object_entry.name,
                                    dir_fd=shard_descriptor,
                                    follow_symlinks=False,
                                )
                                if object_id is None or not stat.S_ISREG(
                                    metadata.st_mode
                                ):
                                    noncanonical_entries += 1
                                    continue
                                observations.append(
                                    ContentAddressedObjectObservation(
                                        object_id=object_id,
                                        identity=StableRegularFileIdentity.from_stat(
                                            metadata
                                        ),
                                    )
                                )
                        if (
                            self._verify_directory_path(
                                shard_descriptor,
                                shard,
                                directory_fd=root_descriptor,
                            )
                            != shard_identity
                        ):
                            raise OSError(
                                getattr(errno, "ESTALE", errno.EIO),
                                "CAS shard identity changed during inventory",
                                self._root_path,
                            )
                    finally:
                        os.close(shard_descriptor)
                    if stop:
                        break

            if self._verify_root_directory(root_descriptor) != root_identity:
                raise OSError(
                    getattr(errno, "ESTALE", errno.EIO),
                    "CAS root identity changed during inventory",
                    self._root_path,
                )
        finally:
            os.close(root_descriptor)

        observations.sort(key=lambda observation: observation.object_id)
        return ContentAddressedObjectInventory(
            objects=tuple(observations),
            scanned_entries=scanned_entries,
            noncanonical_entries=noncanonical_entries,
            complete=complete,
        )

    def snapshot(
        self,
        object_id: str,
        *,
        retain_content: bool = False,
    ) -> StableRegularFileSnapshot:
        try:
            with self._object_directory(object_id, create=False) as object_directory:
                snapshot = stable_regular_file_snapshot(
                    object_directory.leaf,
                    max_bytes=self.max_object_bytes,
                    retain_content=retain_content,
                    directory_fd=object_directory.descriptor,
                )
        except (OSError, ValueError):
            self._verified_identities.pop(object_id, None)
            raise
        return snapshot

    def import_path(self, path: str) -> tuple[str, str, bytes]:
        snapshot = stable_regular_file_snapshot(
            path,
            max_bytes=self.max_object_bytes,
            retain_content=True,
        )
        assert snapshot.content is not None
        content = snapshot.content
        object_id, stored_path = self.put(content, snapshot.sha256)
        return object_id, stored_path, content

    def materialize(self, object_id: str, content: bytes | None) -> str:
        path = self.object_path(object_id)
        if self._verified_object(path, object_id):
            return path
        if content is None:
            if os.path.lexists(path):
                raise ValueError(f"cached object {object_id} failed verification")
            raise FileNotFoundError(f"object {object_id} is not cached")
        _, path = self.put(content, object_id)
        return path


class BitmapDeltaJournal:
    """Bounded version journal for sparse AFL bitmap synchronization."""

    def __init__(self, history_limit: int = 64):
        self.history_limit = max(1, history_limit)
        self.history: dict[int, tuple[tuple[int, int], ...]] = {}

    def record(self, version: int, delta: list[tuple[int, int]]) -> None:
        if version <= 0:
            raise ValueError("bitmap versions start at 1")
        self.history[version] = tuple(delta)
        while len(self.history) > self.history_limit:
            self.history.pop(min(self.history))

    def payload(
        self,
        worker_version: int,
        current_version: int,
        full_bitmap: bytes,
    ) -> dict[str, Any]:
        if worker_version >= current_version:
            return {"bitmap_version": current_version}
        needed = range(max(1, worker_version + 1), current_version + 1)
        if not all(version in self.history for version in needed):
            return {"bitmap_version": current_version, "bitmap_full": full_bitmap}
        merged: dict[int, int] = {}
        for version in needed:
            for index, bits in self.history[version]:
                merged[index] = merged.get(index, 0) | bits
        return {
            "bitmap_version": current_version,
            "bitmap_delta": sorted(merged.items()),
        }

    @staticmethod
    def apply(
        bitmap: bytearray | None,
        payload: dict[str, Any],
        minimum_size: int = 65536,
    ) -> bytearray:
        full = payload.get("bitmap_full")
        if isinstance(full, bytes):
            return bytearray(full)
        if bitmap is None:
            bitmap = bytearray(minimum_size)
        delta = payload.get("bitmap_delta", ())
        if isinstance(delta, (list, tuple)):
            for entry in delta:
                if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                    continue
                index, bits = int(entry[0]), int(entry[1]) & 0xFF
                if index < 0:
                    continue
                if index >= len(bitmap):
                    bitmap.extend(b"\x00" * (index + 1 - len(bitmap)))
                bitmap[index] |= bits
        return bitmap


class ShardedBitmapDeltaJournal:
    """Shard a sparse bitmap delta journal by AFL bitmap index."""

    def __init__(self, history_limit: int = 64, shard_count: int = 1):
        self.shard_count = max(1, int(shard_count))
        self.shards = [
            BitmapDeltaJournal(history_limit) for _ in range(self.shard_count)
        ]

    def shard_for_index(self, index: int) -> int:
        return int(index) % self.shard_count

    def record(self, version: int, delta: list[tuple[int, int]]) -> None:
        grouped: list[list[tuple[int, int]]] = [[] for _ in range(self.shard_count)]
        for index, bits in delta:
            grouped[self.shard_for_index(index)].append((index, bits))
        for shard, shard_delta in zip(self.shards, grouped):
            shard.record(version, shard_delta)

    def payload(
        self,
        worker_version: int,
        current_version: int,
        full_bitmap: bytes,
    ) -> dict[str, Any]:
        if worker_version >= current_version:
            return {"bitmap_version": current_version}
        needed = range(max(1, worker_version + 1), current_version + 1)
        for shard in self.shards:
            if not all(version in shard.history for version in needed):
                return {
                    "bitmap_version": current_version,
                    "bitmap_full": full_bitmap,
                }
        merged: dict[int, int] = {}
        for shard in self.shards:
            for version in needed:
                for index, bits in shard.history[version]:
                    merged[index] = merged.get(index, 0) | bits
        return {
            "bitmap_version": current_version,
            "bitmap_delta": sorted(merged.items()),
            "bitmap_shards": self.shard_count,
        }


_MAX_COVERAGE_TRANSACTION_BYTES = 64 << 20
_MAX_COVERAGE_SHARD_BYTES = 64 << 20
_MAX_COVERAGE_HEARTBEAT_BYTES = 4096
_MAX_PENDING_COVERAGE_TRANSACTIONS = 4096
_MAX_COVERAGE_TRANSACTION_DIRECTORY_ENTRIES = 16384
_MAX_COVERAGE_MAP_SIZE = 1 << 23
_MAX_COVERAGE_SPARSE_EDGES = 1 << 20
_MAX_COVERAGE_CLAIM_BATCH = 4096
_MAX_COVERAGE_CLAIM_FEATURES = 1 << 18
_MAX_COVERAGE_SHARDS = 4096
_MAX_COVERAGE_COORDINATORS = 4096
_COVERAGE_PULL_LOCK_BATCH = 8
_COVERAGE_SPARSE_SET_THRESHOLD = 4096
_COVERAGE_PACKED_INDEX_SHIFT = 8
_COVERAGE_PACKED_CANDIDATE_SHIFT = 31
_COVERAGE_PACKED_INDEX_MASK = _MAX_COVERAGE_MAP_SIZE - 1


class CoverageOwnerShardGossip:
    """Authoritative coverage shards shared by multiple coordinators.

    Each bitmap index has exactly one persistent shard record. Coordinators
    submit candidate bits under a short shard lock, so novelty is decided
    against the same authoritative state even when two masters triage the same
    testcase concurrently. Epoch-based pulls then gossip committed bits into
    each master's local bitmap and worker delta journal.

    A live coordinator is named as the preferred owner for locality and
    observability. Any coordinator may proxy a commit while holding the shard
    lock, which keeps coverage progress available when the preferred owner is
    slow or unavailable; OR-merge makes these proxy commits deterministic.
    """

    def __init__(
        self,
        root: str,
        *,
        shard_count: int = 64,
        coordinator_id: str = "master",
        coordinator_index: int = 0,
        coordinator_count: int = 1,
        heartbeat_ttl: float = 30.0,
        lock_ttl: float = 30.0,
        lock_acquire_timeout: float = 60.0,
        verify_filesystem: bool = False,
        filesystem_probe_timeout: float = 5.0,
        filesystem_requirements: SharedFilesystemRequirementProfile = (
            COVERAGE_SHARED_FILESYSTEM_REQUIREMENTS
        ),
    ) -> None:
        if (
            type(shard_count) is not int
            or not 1 <= shard_count <= _MAX_COVERAGE_SHARDS
        ):
            raise ValueError("coverage shard count is outside its supported range")
        if (
            type(coordinator_index) is not int
            or type(coordinator_count) is not int
            or not 0 <= coordinator_index < coordinator_count
            or coordinator_count > _MAX_COVERAGE_COORDINATORS
        ):
            raise ValueError("coverage coordinator topology is invalid")
        if (
            not isinstance(coordinator_id, str)
            or not coordinator_id
            or len(coordinator_id) > 256
            or "\x00" in coordinator_id
        ):
            raise ValueError("coverage coordinator identity is invalid")
        self.root = root
        self.shard_count = shard_count
        self.coordinator_id = coordinator_id
        self.coordinator_index = coordinator_index
        self.coordinator_count = coordinator_count
        self.heartbeat_ttl = _finite_duration(
            heartbeat_ttl, 1.0, "coverage heartbeat TTL"
        )
        self.lock_ttl = _finite_duration(lock_ttl, 1.0, "coverage lock age hint")
        self.lock_acquire_timeout = _finite_duration(
            lock_acquire_timeout, 0.001, "coverage lock acquisition timeout"
        )
        self.filesystem_capabilities = (
            probe_shared_state_filesystem(
                root,
                timeout=filesystem_probe_timeout,
                requirements=filesystem_requirements,
            )
            if verify_filesystem
            else None
        )
        self.seen_epochs = [-1 for _ in range(self.shard_count)]
        self.known: dict[int, int] = {}
        self.claims = 0
        self.claim_batches = 0
        self.claim_shard_writes = 0
        self.claimed_features = 0
        self.duplicate_features = 0
        self.pulls = 0
        self.pulled_features = 0
        self.recovered_transactions = 0
        self.recovery_scans_outside_lock = 0
        self.recovery_rescans_under_lock = 0
        self._recent_claim_shards: set[int] = set()
        self._last_transaction_path: str | None = None
        durable_makedirs(self._state_root())
        durable_makedirs(self._heartbeat_root())
        durable_makedirs(self._transaction_root())
        self.heartbeat()
        pending = self._pending_transactions()
        if pending:
            pending_shards = {
                int(record["shard"])
                for _path, transaction in pending
                for record in transaction["records"]
            }
            with self._locked_shards_after_recovery(pending_shards):
                pass

    def _state_root(self) -> str:
        return os.path.join(self.root, "state")

    def _heartbeat_root(self) -> str:
        return os.path.join(self.root, "coordinators")

    def shard_for_index(self, index: int) -> int:
        if type(index) is not int or not 0 <= index < _MAX_COVERAGE_MAP_SIZE:
            raise ValueError("invalid bitmap index")
        return index % self.shard_count

    def state_path(self, shard: int) -> str:
        if shard < 0 or shard >= self.shard_count:
            raise ValueError("invalid coverage shard")
        return os.path.join(self._state_root(), f"{shard:04d}.json")

    def lock_path(self, shard: int) -> str:
        return self.state_path(shard) + ".lock"

    def _transaction_root(self) -> str:
        return os.path.join(self.root, "claim-transactions")

    def transaction_path(self, transaction_id: str | None = None) -> str:
        if transaction_id is not None:
            if re.fullmatch(r"[0-9a-f]{64}", transaction_id) is None:
                raise ValueError("invalid coverage transaction identity")
            return os.path.join(self._transaction_root(), f"{transaction_id}.json")
        if self._last_transaction_path is not None:
            return self._last_transaction_path
        # Compatibility path for recovery of pre-sharded transaction journals.
        return os.path.join(self.root, ".coverage-claim-transaction.json")

    def heartbeat_path(self, index: int | None = None) -> str:
        slot = self.coordinator_index if index is None else max(0, int(index))
        return os.path.join(self._heartbeat_root(), f"{slot:04d}.json")

    def heartbeat(self, *, now: float | None = None) -> None:
        now = _finite_timestamp(
            time.time() if now is None else now, "coverage timestamp"
        )
        path = self.heartbeat_path()
        record = {
            "schema": 1,
            "id": self.coordinator_id,
            "index": self.coordinator_index,
            "count": self.coordinator_count,
            "updated": now,
        }
        content = (
            json.dumps(
                record,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("ascii")
            + b"\n"
        )
        if len(content) > _MAX_COVERAGE_HEARTBEAT_BYTES:
            raise ValueError("coverage coordinator heartbeat exceeds byte budget")
        _atomic_publish_bytes(path, content, "coverage heartbeat")

    def _live_coordinators(self, now: float) -> dict[int, str]:
        live: dict[int, str] = {}
        for index in range(self.coordinator_count):
            try:
                snapshot = stable_regular_file_snapshot(
                    self.heartbeat_path(index),
                    max_bytes=_MAX_COVERAGE_HEARTBEAT_BYTES,
                    retain_content=True,
                )
                if snapshot.identity.size <= 0 or snapshot.content is None:
                    continue
                record = json.loads(
                    snapshot.content.decode("ascii"),
                    object_pairs_hook=_json_object_without_duplicates,
                    parse_constant=_reject_nonfinite_json,
                )
                if not isinstance(record, dict):
                    continue
                updated = float(record.get("updated", 0.0) or 0.0)
                identity = record.get("id")
                if (
                    record.get("schema") == 1
                    and record.get("index") == index
                    and record.get("count") == self.coordinator_count
                    and isinstance(identity, str)
                    and bool(identity)
                    and math.isfinite(updated)
                    and updated >= 0.0
                    and now - updated < self.heartbeat_ttl
                ):
                    live[index] = identity
            except (OSError, ValueError, TypeError, OverflowError):
                continue
        live.setdefault(self.coordinator_index, self.coordinator_id)
        return live

    def _owner_for_shard_from_live(
        self,
        shard: int,
        live: Mapping[int, str],
    ) -> tuple[int, str]:
        if type(shard) is not int or not 0 <= shard < self.shard_count:
            raise ValueError("invalid coverage shard")
        preferred = shard % self.coordinator_count
        for offset in range(self.coordinator_count):
            candidate = (preferred + offset) % self.coordinator_count
            if candidate in live:
                return candidate, str(live[candidate])
        return self.coordinator_index, self.coordinator_id

    def owner_for_shard(
        self,
        shard: int,
        *,
        now: float | None = None,
    ) -> tuple[int, str]:
        now = _finite_timestamp(
            time.time() if now is None else now, "coverage timestamp"
        )
        return self._owner_for_shard_from_live(
            shard,
            self._live_coordinators(now),
        )

    @contextmanager
    def _locked(self, shard: int):
        with _bounded_advisory_lock(
            self.lock_path(shard),
            timeout=self.lock_acquire_timeout,
            description=f"coverage-owner shard lock: {shard}",
            age_hint=self.lock_ttl,
        ):
            yield

    @staticmethod
    def _empty_shard(shard: int) -> dict[str, Any]:
        return {
            "schema": 1,
            "shard": shard,
            "epoch": 0,
            "entries": [],
            "contributors": {},
        }

    def _valid_shard_record(self, record: Any, shard: int) -> bool:
        if (
            not isinstance(record, dict)
            or type(record.get("schema")) is not int
            or record.get("schema") != 1
            or type(record.get("shard")) is not int
            or record.get("shard") != shard
            or type(record.get("epoch")) is not int
            or record.get("epoch", -1) < 0
            or not isinstance(record.get("entries"), list)
            or not isinstance(record.get("contributors"), dict)
        ):
            return False
        previous_index = -1
        for item in record["entries"]:
            if (
                not isinstance(item, (list, tuple))
                or len(item) != 2
                or type(item[0]) is not int
                or type(item[1]) is not int
                or item[0] < 0
                or item[0] >= _MAX_COVERAGE_MAP_SIZE
                or self.shard_for_index(item[0]) != shard
                or item[0] <= previous_index
                or not 1 <= item[1] <= 0xFF
            ):
                return False
            previous_index = item[0]
        for identity, commits in record["contributors"].items():
            if (
                not isinstance(identity, str)
                or not identity
                or type(commits) is not int
                or commits < 0
            ):
                return False
        if "updated" in record:
            try:
                updated = float(record["updated"])
            except (TypeError, ValueError, OverflowError):
                return False
            if not math.isfinite(updated) or updated < 0.0:
                return False
        return True

    def _read_shard(self, shard: int) -> dict[str, Any]:
        try:
            snapshot = stable_regular_file_snapshot(
                self.state_path(shard),
                max_bytes=_MAX_COVERAGE_SHARD_BYTES,
                retain_content=True,
            )
        except FileNotFoundError:
            return self._empty_shard(shard)
        try:
            if snapshot.identity.size <= 0 or snapshot.content is None:
                raise ValueError("empty coverage-owner shard record")
            record = json.loads(
                snapshot.content.decode("ascii"),
                object_pairs_hook=_json_object_without_duplicates,
                parse_constant=_reject_nonfinite_json,
            )
        except (UnicodeError, ValueError, TypeError, RecursionError) as error:
            raise ValueError(f"invalid coverage-owner shard record: {shard}") from error
        if not self._valid_shard_record(record, shard):
            raise ValueError(f"invalid coverage-owner shard record: {shard}")
        return record

    def _write_shard(self, shard: int, record: dict[str, Any]) -> None:
        path = self.state_path(shard)
        content = (
            json.dumps(
                record,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("ascii")
            + b"\n"
        )
        if len(content) > _MAX_COVERAGE_SHARD_BYTES:
            raise ValueError("coverage-owner shard record exceeds byte budget")
        _atomic_publish_bytes(path, content, "coverage shard")

    @staticmethod
    def _transaction_digest(record: Mapping[str, Any]) -> str:
        payload = {
            key: value
            for key, value in record.items()
            if key != "transaction_sha256"
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()

    def _new_transaction_path(self) -> str:
        material = (
            f"{self.coordinator_id}\x00{os.getpid()}\x00{time.time_ns()}\x00"
            f"{os.urandom(32).hex()}"
        ).encode("utf-8")
        path = self.transaction_path(hashlib.sha256(material).hexdigest())
        self._last_transaction_path = path
        return path

    def _write_transaction(self, record: dict[str, Any], path: str) -> None:
        content = (
            json.dumps(
                record,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("ascii")
            + b"\n"
        )
        if len(content) > _MAX_COVERAGE_TRANSACTION_BYTES:
            raise ValueError("coverage-owner batch transaction exceeds byte budget")
        _atomic_publish_bytes(path, content, "coverage transaction")

    def _read_transaction(self, path: str) -> dict[str, Any] | None:
        try:
            snapshot = stable_regular_file_snapshot(
                path,
                max_bytes=_MAX_COVERAGE_TRANSACTION_BYTES,
                retain_content=True,
            )
            if snapshot.identity.size <= 0 or snapshot.content is None:
                raise ValueError("invalid coverage-owner batch transaction")
            record = json.loads(
                snapshot.content.decode("ascii"),
                object_pairs_hook=_json_object_without_duplicates,
                parse_constant=_reject_nonfinite_json,
            )
        except FileNotFoundError:
            return None
        except (UnicodeError, ValueError, TypeError, RecursionError) as error:
            raise ValueError("invalid coverage-owner batch transaction") from error
        if (
            not isinstance(record, dict)
            or set(record)
            != {
                "schema",
                "shard_count",
                "coordinator",
                "created",
                "records",
                "novel_by_candidate",
                "transaction_sha256",
            }
            or record.get("schema") != 1
            or record.get("shard_count") != self.shard_count
            or not isinstance(record.get("coordinator"), str)
            or not record.get("coordinator")
            or not isinstance(record.get("records"), list)
            or not record.get("records")
            or not isinstance(record.get("novel_by_candidate"), list)
            or record.get("transaction_sha256")
            != self._transaction_digest(record)
        ):
            raise ValueError("invalid coverage-owner batch transaction")
        try:
            created = float(record["created"])
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("invalid coverage-owner batch transaction") from error
        if not math.isfinite(created) or created < 0.0:
            raise ValueError("invalid coverage-owner batch transaction")
        previous_shard = -1
        for shard_record in record["records"]:
            if not isinstance(shard_record, dict):
                raise ValueError("invalid coverage-owner batch transaction")
            shard = shard_record.get("shard")
            if (
                type(shard) is not int
                or not 0 <= shard < self.shard_count
                or shard <= previous_shard
                or not self._valid_shard_record(shard_record, shard)
            ):
                raise ValueError("invalid coverage-owner batch transaction")
            previous_shard = shard
        if any(
            type(value) is not int or value < 0
            for value in record["novel_by_candidate"]
        ):
            raise ValueError("invalid coverage-owner batch transaction")
        return record

    def _pending_transaction_paths(self) -> list[str]:
        paths: list[str] = []
        legacy = os.path.join(self.root, ".coverage-claim-transaction.json")
        if os.path.isfile(legacy):
            paths.append(legacy)
        try:
            entries = os.scandir(self._transaction_root())
        except FileNotFoundError:
            entries = None
        if entries is not None:
            with entries:
                scanned = 0
                for entry in entries:
                    scanned += 1
                    if scanned > _MAX_COVERAGE_TRANSACTION_DIRECTORY_ENTRIES:
                        raise ValueError(
                            "coverage-owner transaction directory budget exceeded"
                        )
                    if (
                        entry.is_file(follow_symlinks=False)
                        and re.fullmatch(
                            r"[0-9a-f]{64}\.json", entry.name
                        ) is not None
                    ):
                        paths.append(entry.path)
                        if len(paths) > _MAX_PENDING_COVERAGE_TRANSACTIONS:
                            raise ValueError(
                                "coverage-owner pending transaction budget exceeded"
                            )
        return sorted(set(paths))

    def _pending_transactions(self) -> list[tuple[str, dict[str, Any]]]:
        return self._pending_transactions_from_paths(
            self._pending_transaction_paths()
        )

    def _pending_transactions_from_paths(
        self,
        paths: Iterable[str],
    ) -> list[tuple[str, dict[str, Any]]]:
        pending: list[tuple[str, dict[str, Any]]] = []
        for path in paths:
            transaction = self._read_transaction(path)
            if transaction is not None:
                pending.append((path, transaction))
        return pending

    @staticmethod
    def _intersecting_transactions(
        pending: list[tuple[str, dict[str, Any]]],
        shards: set[int],
    ) -> tuple[list[tuple[str, dict[str, Any]]], set[int]]:
        selected: list[tuple[str, dict[str, Any]]] = []
        selected_paths: set[str] = set()
        closure = set(shards)
        changed = True
        while changed:
            changed = False
            for path, transaction in pending:
                transaction_shards = {
                    int(record["shard"]) for record in transaction["records"]
                }
                if path not in selected_paths and transaction_shards & closure:
                    selected.append((path, transaction))
                    selected_paths.add(path)
                    before = len(closure)
                    closure.update(transaction_shards)
                    changed = changed or len(closure) != before
        return selected, closure

    def _apply_transaction_record(self, record: dict[str, Any]) -> None:
        shard = int(record["shard"])
        current = self._read_shard(shard)
        current_epoch = int(current.get("epoch", 0))
        target_epoch = int(record.get("epoch", 0))
        if current_epoch >= target_epoch:
            current_bits = {
                int(index): int(bits) & 0xFF
                for index, bits in current.get("entries", ())
            }
            if any(
                current_bits.get(int(index), 0) & int(bits) != int(bits)
                for index, bits in record.get("entries", ())
            ):
                raise ValueError(
                    "coverage-owner transaction would roll back a newer shard"
                )
            return
        self._write_shard(shard, record)

    @contextmanager
    def _locked_shards_after_recovery(self, shards: set[int]):
        locked_shards = set(shards)
        for _attempt in range(self.shard_count + 2):
            pending = self._pending_transactions()
            self.recovery_scans_outside_lock += 1
            pending_paths = {path for path, _transaction in pending}
            with ExitStack() as stack:
                for shard in sorted(locked_shards):
                    stack.enter_context(self._locked(shard))
                current_paths = set(self._pending_transaction_paths())
                if current_paths != pending_paths:
                    pending = self._pending_transactions_from_paths(
                        sorted(current_paths)
                    )
                    self.recovery_rescans_under_lock += 1
                selected, closure = self._intersecting_transactions(
                    pending, locked_shards
                )
                missing = closure - locked_shards
                if missing:
                    locked_shards.update(missing)
                    continue
                for path, expected in sorted(
                    selected,
                    key=lambda item: (float(item[1]["created"]), item[0]),
                ):
                    transaction = self._read_transaction(path)
                    if transaction is None:
                        continue
                    if transaction["transaction_sha256"] != expected["transaction_sha256"]:
                        raise ValueError("coverage-owner transaction changed during recovery")
                    for record in transaction["records"]:
                        self._apply_transaction_record(record)
                    durable_unlink(path)
                    self.recovered_transactions += 1
                yield
                return
        raise RuntimeError("coverage-owner transaction lock closure did not converge")

    def _validated_delta_entries(
        self,
        delta: bytes | bytearray | list[tuple[int, int]],
    ) -> Iterable[tuple[int, int]]:
        if isinstance(delta, (bytes, bytearray)):
            if len(delta) > _MAX_COVERAGE_MAP_SIZE:
                raise ValueError("coverage bitmap exceeds negotiated map size")
            for index, bits in enumerate(delta):
                if bits:
                    yield index, int(bits)
            return
        if not isinstance(delta, list) or len(delta) > _MAX_COVERAGE_SPARSE_EDGES:
            raise ValueError("coverage delta must be a bounded sparse list")
        seen: set[int] | None = (
            set() if len(delta) <= _COVERAGE_SPARSE_SET_THRESHOLD else None
        )
        seen_bitmap = (
            None
            if seen is not None
            else bytearray((_MAX_COVERAGE_MAP_SIZE + 7) // 8)
        )
        for item in delta:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise ValueError("coverage delta contains a malformed row")
            index, bits = item
            if (
                type(index) is not int
                or not 0 <= index < _MAX_COVERAGE_MAP_SIZE
                or type(bits) is not int
                or not 1 <= bits <= 0xFF
            ):
                raise ValueError("coverage delta contains an invalid row")
            if seen is not None:
                if index in seen:
                    raise ValueError("coverage delta contains an invalid row")
                seen.add(index)
            else:
                assert seen_bitmap is not None
                byte_index, bit_index = divmod(index, 8)
                mask = 1 << bit_index
                if seen_bitmap[byte_index] & mask:
                    raise ValueError("coverage delta contains an invalid row")
                seen_bitmap[byte_index] |= mask
            yield index, bits

    def _group_deltas(
        self,
        deltas: list[bytes | bytearray | list[tuple[int, int]]],
    ) -> tuple[dict[int, array], int]:
        """Validate and pack a batch without expanding dense bitmaps."""
        grouped: dict[int, array] = {}
        feature_count = 0
        for candidate, delta in enumerate(deltas):
            for index, bits in self._validated_delta_entries(delta):
                feature_count += 1
                if feature_count > _MAX_COVERAGE_CLAIM_FEATURES:
                    raise ValueError(
                        "coverage claim batch exceeds its feature budget"
                    )
                shard = self.shard_for_index(index)
                entries = grouped.get(shard)
                if entries is None:
                    entries = array("Q")
                    grouped[shard] = entries
                entries.append(
                    (candidate << _COVERAGE_PACKED_CANDIDATE_SHIFT)
                    | (index << _COVERAGE_PACKED_INDEX_SHIFT)
                    | bits
                )
        return grouped, feature_count

    def claim(
        self,
        delta: bytes | bytearray | list[tuple[int, int]],
        *,
        now: float | None = None,
    ) -> int:
        """Atomically commit candidate bits and return globally novel bits."""
        return self.claim_many([delta], now=now)[0]

    def claim_many(
        self,
        deltas: list[bytes | bytearray | list[tuple[int, int]]],
        *,
        now: float | None = None,
    ) -> list[int]:
        """Commit a candidate batch with at most one write per touched shard.

        Candidate order defines novelty attribution, matching sequential
        ``claim`` calls.  Grouping only changes the persistence schedule: each
        shard is locked, read and durably replaced once for the whole batch.
        """
        if not isinstance(deltas, list) or len(deltas) > _MAX_COVERAGE_CLAIM_BATCH:
            raise ValueError("coverage claim batch exceeds its candidate budget")
        now = _finite_timestamp(
            time.time() if now is None else now, "coverage timestamp"
        )
        grouped, _feature_count = self._group_deltas(deltas)
        novel_by_candidate = [0 for _ in deltas]
        if not grouped:
            self.claims += len(deltas)
            self.claim_batches += 1
            return novel_by_candidate
        self.heartbeat(now=now)
        live_coordinators = self._live_coordinators(now)
        duplicates = 0
        changed_records: list[dict[str, Any]] = []
        with self._locked_shards_after_recovery(set(grouped)):
            for shard, entries in sorted(grouped.items()):
                record = self._read_shard(shard)
                state = {
                    int(index): int(bits) & 0xFF
                    for index, bits in record.get("entries", ())
                    if int(index) >= 0 and int(bits)
                }
                changed = False
                for packed in entries:
                    candidate = packed >> _COVERAGE_PACKED_CANDIDATE_SHIFT
                    index = (
                        packed >> _COVERAGE_PACKED_INDEX_SHIFT
                    ) & _COVERAGE_PACKED_INDEX_MASK
                    bits = packed & 0xFF
                    old = state.get(index, 0)
                    new_bits = bits & ~old
                    novel_by_candidate[candidate] += new_bits.bit_count()
                    duplicates += (bits & old).bit_count()
                    if new_bits:
                        state[index] = old | bits
                        changed = True
                if not changed:
                    continue
                owner_index, owner_id = self._owner_for_shard_from_live(
                    shard,
                    live_coordinators,
                )
                contributors = record.get("contributors", {})
                if not isinstance(contributors, dict):
                    contributors = {}
                contributors[self.coordinator_id] = (
                    int(contributors.get(self.coordinator_id, 0) or 0) + 1
                )
                record.update(
                    {
                        "schema": 1,
                        "shard": shard,
                        "epoch": int(record.get("epoch", 0) or 0) + 1,
                        "owner_index": owner_index,
                        "owner": owner_id,
                        "last_writer": self.coordinator_id,
                        "updated": now,
                        "entries": sorted(state.items()),
                        "contributors": contributors,
                    }
                )
                changed_records.append(record)
            if changed_records:
                transaction = {
                    "schema": 1,
                    "shard_count": self.shard_count,
                    "coordinator": self.coordinator_id,
                    "created": now,
                    "records": changed_records,
                    "novel_by_candidate": novel_by_candidate,
                }
                transaction["transaction_sha256"] = self._transaction_digest(
                    transaction
                )
                transaction_path = self._new_transaction_path()
                self._write_transaction(transaction, transaction_path)
                try:
                    for record in changed_records:
                        self._write_shard(int(record["shard"]), record)
                except OSError:
                    # Retry the idempotent forward commit once. If storage
                    # remains unavailable, recovery retains the unique WAL.
                    for record in changed_records:
                        self._write_shard(int(record["shard"]), record)
                durable_unlink(transaction_path)
                for record in changed_records:
                    shard = int(record["shard"])
                    self.claim_shard_writes += 1
                    self._recent_claim_shards.add(shard)
        self.claims += len(deltas)
        self.claim_batches += 1
        self.claimed_features += sum(novel_by_candidate)
        self.duplicate_features += duplicates
        return novel_by_candidate

    def pull(
        self,
        shards: Any = None,
    ) -> list[tuple[int, int]]:
        """Pull epochs newer than the local view and return missing bitmap bits."""
        merged: dict[int, int] = {}
        if shards is None:
            shard_source = set(range(self.shard_count))
        else:
            if isinstance(shards, (str, bytes, bytearray, Mapping)):
                raise ValueError("coverage pull shards must be an integer iterable")
            try:
                iterator = iter(shards)
            except TypeError as error:
                raise ValueError(
                    "coverage pull shards must be an integer iterable"
                ) from error
            requested: list[int] = []
            for shard in iterator:
                if (
                    len(requested) >= self.shard_count
                    or type(shard) is not int
                    or not 0 <= shard < self.shard_count
                ):
                    raise ValueError("coverage pull contains an invalid shard")
                requested.append(shard)
            shard_source = set(requested)
        ordered_shards = sorted(shard_source)
        for offset in range(0, len(ordered_shards), _COVERAGE_PULL_LOCK_BATCH):
            batch = ordered_shards[offset : offset + _COVERAGE_PULL_LOCK_BATCH]
            with self._locked_shards_after_recovery(set(batch)):
                for shard in batch:
                    record = self._read_shard(shard)
                    epoch = int(record.get("epoch", 0))
                    if epoch <= self.seen_epochs[shard]:
                        continue
                    for index, bits in record.get("entries", ()):
                        old = self.known.get(index, 0)
                        new_bits = bits & ~old
                        if new_bits:
                            merged[index] = merged.get(index, 0) | new_bits
                            self.known[index] = old | bits
                    self.seen_epochs[shard] = epoch
        self.pulls += 1
        self.pulled_features += sum(bits.bit_count() for bits in merged.values())
        return sorted(merged.items())

    def pull_recent_claims(self) -> list[tuple[int, int]]:
        shards = set(self._recent_claim_shards)
        self._recent_claim_shards.clear()
        return self.pull(shards)

    def snapshot(self) -> dict[str, Any]:
        live = self._live_coordinators(time.time())
        return {
            "schema": 1,
            "shards": self.shard_count,
            "coordinator": self.coordinator_id,
            "coordinator_index": self.coordinator_index,
            "coordinator_count": self.coordinator_count,
            "live_coordinators": len(live),
            "claims": self.claims,
            "claim_batches": self.claim_batches,
            "claim_shard_writes": self.claim_shard_writes,
            "claimed_features": self.claimed_features,
            "duplicate_features": self.duplicate_features,
            "pulls": self.pulls,
            "pulled_features": self.pulled_features,
            "recovered_transactions": self.recovered_transactions,
            "recovery_scans_outside_lock": self.recovery_scans_outside_lock,
            "recovery_rescans_under_lock": self.recovery_rescans_under_lock,
            "known_bytes": len(self.known),
            "max_epoch": max(self.seen_epochs, default=-1),
        }


class PersistentShardLedger:
    """Append-only digest ledger split into deterministic shard files."""

    def __init__(self, root: str, shard_count: int = 16, namespace: str = "processed"):
        self.root = root
        self.shard_count = max(1, int(shard_count))
        self.namespace = namespace
        durable_makedirs(root)

    @staticmethod
    def _valid_digest(digest: str) -> bool:
        return len(digest) == 64 and all(char in "0123456789abcdef" for char in digest)

    def shard_for_digest(self, digest: str) -> int:
        if not self._valid_digest(digest):
            raise ValueError("invalid SHA-256 digest")
        return int(digest[:8], 16) % self.shard_count

    def shard_path(self, shard: int) -> str:
        if shard < 0 or shard >= self.shard_count:
            raise ValueError("invalid shard")
        return os.path.join(self.root, f"{self.namespace}.{shard:03d}.log")

    def add(self, digest: str) -> None:
        if not self._valid_digest(digest):
            return
        path = self.shard_path(self.shard_for_digest(digest))
        try:
            new_file = not os.path.exists(path)
            with open(path, "a", encoding="ascii") as stream:
                stream.write(digest)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            if new_file:
                fsync_directory(os.path.dirname(path) or ".")
        except OSError:
            return

    def load_recent(self, limit: int = 0) -> set[str]:
        result: set[str] = set()
        for shard in range(self.shard_count):
            path = self.shard_path(shard)
            try:
                with open(path, encoding="ascii", errors="ignore") as stream:
                    for line in stream:
                        digest = line.strip()
                        if self._valid_digest(digest):
                            result.add(digest)
                            if limit > 0 and len(result) > limit:
                                result.pop()
            except OSError:
                continue
        return result


class WorkLeaseJournal:
    """Append-only recovery journal for in-flight distributed work items.

    The journal is intentionally JSONL instead of a mutable database: dispatch is
    one append, completion is one append, and recovery can ignore torn/corrupt
    tail lines left by a killed coordinator.
    """

    def __init__(
        self,
        path: str,
        lease_ttl: float = 300.0,
        compact_after: int = 8192,
    ):
        self.path = path
        self.lease_ttl = _finite_duration(lease_ttl, 1.0, "work lease TTL")
        self.compact_after = max(128, int(compact_after))
        self.leases: dict[str, dict[str, Any]] = {}
        self.completed: set[str] = set()
        self.events = 0
        directory = os.path.dirname(path)
        if directory:
            durable_makedirs(directory)
        self._load()

    @staticmethod
    def work_id(payload: dict[str, Any]) -> str:
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
            allow_nan=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8", errors="ignore") as stream:
                for line in stream:
                    try:
                        record = json.loads(
                            line,
                            object_pairs_hook=_json_object_without_duplicates,
                            parse_constant=_reject_nonfinite_json,
                        )
                    except (ValueError, RecursionError):
                        continue
                    if not isinstance(record, dict):
                        continue
                    work_id = str(record.get("id", ""))
                    if len(work_id) != 64 or any(
                        char not in "0123456789abcdef" for char in work_id
                    ):
                        continue
                    op = record.get("op")
                    self.events += 1
                    if op == "lease":
                        payload = record.get("payload")
                        if not isinstance(payload, dict):
                            continue
                        try:
                            if self.work_id(payload) != work_id:
                                continue
                            worker_raw = record.get("worker", 0)
                            if isinstance(worker_raw, bool):
                                continue
                            worker = int(worker_raw or 0)
                            updated = _finite_timestamp(
                                record.get("time", 0.0) or 0.0,
                                "journal lease timestamp",
                            )
                            attempts = int(record.get("attempts", 1) or 1)
                        except (TypeError, ValueError, OverflowError):
                            continue
                        clock = str(record.get("clock", "legacy"))
                        if worker < 0 or attempts < 1 or clock not in {
                            "legacy",
                            "unix",
                        }:
                            continue
                        self.completed.discard(work_id)
                        self.leases[work_id] = {
                            "id": work_id,
                            "payload": payload,
                            "worker": worker,
                            "updated": updated,
                            "attempts": attempts,
                            "clock": clock,
                        }
                    elif op == "done":
                        if work_id in self.leases:
                            self.completed.add(work_id)
                            self.leases.pop(work_id, None)
                    elif op == "reclaim":
                        self.leases.pop(work_id, None)
        except OSError:
            return

    def _append(self, record: dict[str, Any]) -> None:
        new_file = not os.path.exists(self.path)
        encoded = json.dumps(
            record,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        with open(self.path, "a", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if new_file:
            fsync_directory(os.path.dirname(self.path) or ".")
        self.events += 1

    def _maybe_compact(self) -> None:
        if self.events >= self.compact_after:
            self.compact()

    def lease(
        self,
        work_id: str,
        payload: dict[str, Any],
        *,
        worker: int = 0,
        now: float | None = None,
    ) -> bool:
        if not isinstance(payload, dict):
            raise ValueError("work lease payload must be an object")
        try:
            expected_work_id = self.work_id(payload)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("work lease payload is not canonical JSON") from error
        if (
            not isinstance(work_id, str)
            or len(work_id) != 64
            or any(char not in "0123456789abcdef" for char in work_id)
            or work_id != expected_work_id
            or work_id in self.completed
            or work_id in self.leases
        ):
            return False
        if isinstance(worker, bool):
            raise ValueError("work lease worker must be a non-negative integer")
        try:
            worker = int(worker)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError(
                "work lease worker must be a non-negative integer"
            ) from error
        if worker < 0:
            raise ValueError("work lease worker must be a non-negative integer")
        now = _finite_timestamp(
            time.time() if now is None else now,
            "work lease timestamp",
        )
        attempts = 1
        entry = {
            "id": work_id,
            "payload": dict(payload),
            "worker": worker,
            "updated": now,
            "attempts": attempts,
            "clock": "unix",
        }
        self._append(
            {
                "op": "lease",
                "id": work_id,
                "payload": dict(payload),
                "worker": worker,
                "time": now,
                "attempts": attempts,
                "clock": "unix",
            }
        )
        self.completed.discard(work_id)
        self.leases[work_id] = entry
        self._maybe_compact()
        return True

    def complete(
        self,
        work_id: str,
        now: float | None = None,
        *,
        worker: int | None = None,
    ) -> bool:
        if not isinstance(work_id, str) or len(work_id) != 64 or any(
            char not in "0123456789abcdef" for char in work_id
        ):
            return False
        lease = self.leases.get(work_id)
        if lease is None:
            return False
        if worker is not None:
            if isinstance(worker, bool):
                return False
            try:
                requested_worker = int(worker)
            except (TypeError, ValueError, OverflowError):
                return False
            if int(lease.get("worker", 0)) != requested_worker:
                return False
        now = _finite_timestamp(
            time.time() if now is None else now,
            "work completion timestamp",
        )
        self._append({"op": "done", "id": work_id, "time": now, "clock": "unix"})
        self.completed.add(work_id)
        self.leases.pop(work_id, None)
        self._maybe_compact()
        return True

    def abandon(
        self,
        work_id: str,
        now: float | None = None,
        *,
        worker: int | None = None,
    ) -> bool:
        """Undo a local lease that never reached a worker."""
        if (
            not isinstance(work_id, str)
            or len(work_id) != 64
            or any(char not in "0123456789abcdef" for char in work_id)
            or work_id not in self.leases
        ):
            return False
        lease = self.leases[work_id]
        if worker is not None:
            if isinstance(worker, bool):
                return False
            try:
                requested_worker = int(worker)
            except (TypeError, ValueError, OverflowError):
                return False
            if int(lease.get("worker", 0)) != requested_worker:
                return False
        now = _finite_timestamp(
            time.time() if now is None else now,
            "work reclaim timestamp",
        )
        self._append({"op": "reclaim", "id": work_id, "time": now, "clock": "unix"})
        self.leases.pop(work_id, None)
        self._maybe_compact()
        return True

    def recover_expired(
        self,
        *,
        now: float | None = None,
        lease_ttl: float | None = None,
        limit: int = 0,
    ) -> list[dict[str, Any]]:
        now = _finite_timestamp(
            time.time() if now is None else now,
            "work recovery timestamp",
        )
        if lease_ttl is None:
            ttl = self.lease_ttl
        else:
            try:
                ttl = float(lease_ttl)
            except (TypeError, ValueError, OverflowError) as error:
                raise ValueError("work recovery TTL must be finite") from error
            if not math.isfinite(ttl):
                raise ValueError("work recovery TTL must be finite")
            ttl = max(0.0, ttl)
        expired = [
            entry
            for entry in self.leases.values()
            if (
                ttl == 0.0
                or entry.get("clock") != "unix"
                or now - float(entry.get("updated", 0.0)) >= ttl
            )
        ]
        expired.sort(
            key=lambda entry: (
                int(entry.get("attempts", 0)),
                float(entry.get("updated", 0.0)),
                str(entry.get("id", "")),
            )
        )
        if limit > 0:
            expired = expired[:limit]
        recovered: list[dict[str, Any]] = []
        for entry in expired:
            work_id = str(entry["id"])
            payload = dict(entry.get("payload", {}))
            self._append(
                {
                    "op": "reclaim",
                    "id": work_id,
                    "time": now,
                    "clock": "unix",
                }
            )
            recovered.append(payload)
            self.leases.pop(work_id, None)
            self._maybe_compact()
        return recovered

    def compact(self) -> None:
        tmp = f"{self.path}.{os.getpid()}.{time.monotonic_ns()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as stream:
                for entry in sorted(
                    self.leases.values(), key=lambda item: str(item.get("id", ""))
                ):
                    json.dump(
                        {
                            "op": "lease",
                            "id": entry["id"],
                            "payload": entry["payload"],
                            "worker": int(entry.get("worker", 0)),
                            "time": float(entry.get("updated", 0.0)),
                            "attempts": int(entry.get("attempts", 1)),
                            "clock": str(entry.get("clock", "legacy")),
                        },
                        stream,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                    stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            durable_replace(tmp, self.path)
            self.events = len(self.leases)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass


class FencedWorkLeaseTable:
    """Cross-coordinator lease table with fencing tokens.

    Unlike WorkLeaseJournal, this table is meant to be shared by multiple MPI
    masters on a common filesystem. A kernel-released per-work advisory lock
    serializes claim/complete operations; the long-lived lease is just a JSON
    record with a fencing token. A stale master can finish late, but its token
    will no longer match after another master has stolen the expired lease.
    """

    def __init__(
        self,
        root: str,
        *,
        shard_count: int = 256,
        lease_ttl: float = 300.0,
        lock_ttl: float = 30.0,
        lock_acquire_timeout: float = 60.0,
        verify_filesystem: bool = False,
        filesystem_probe_timeout: float = 5.0,
        filesystem_requirements: SharedFilesystemRequirementProfile = (
            LEASE_SHARED_FILESYSTEM_REQUIREMENTS
        ),
    ) -> None:
        self.root = root
        self.shard_count = max(1, int(shard_count))
        self.lease_ttl = _finite_duration(lease_ttl, 1.0, "lease TTL")
        self.lock_ttl = _finite_duration(lock_ttl, 1.0, "lock age hint")
        self.lock_acquire_timeout = _finite_duration(
            lock_acquire_timeout, 0.001, "lock acquisition timeout"
        )
        self.filesystem_capabilities = (
            probe_shared_state_filesystem(
                root,
                timeout=filesystem_probe_timeout,
                requirements=filesystem_requirements,
            )
            if verify_filesystem
            else None
        )
        durable_makedirs(root)

    @staticmethod
    def valid_work_id(work_id: Any) -> bool:
        return (
            isinstance(work_id, str)
            and len(work_id) == 64
            and all(char in "0123456789abcdef" for char in work_id)
        )

    def shard_for_work(self, work_id: str) -> int:
        if not self.valid_work_id(work_id):
            raise ValueError("invalid work id")
        return int(work_id[:8], 16) % self.shard_count

    def shard_dir(self, shard: int) -> str:
        if shard < 0 or shard >= self.shard_count:
            raise ValueError("invalid lease shard")
        return os.path.join(self.root, f"{shard:03d}")

    def record_path(self, work_id: str) -> str:
        shard = self.shard_for_work(work_id)
        return os.path.join(self.shard_dir(shard), f"{work_id}.json")

    def lock_path(self, work_id: str) -> str:
        return self.record_path(work_id) + ".lock"

    @contextmanager
    def _locked(self, work_id: str):
        with _bounded_advisory_lock(
            self.lock_path(work_id),
            timeout=self.lock_acquire_timeout,
            description=f"fenced lease lock: {work_id}",
            age_hint=self.lock_ttl,
        ):
            yield

    def _read_record(self, work_id: str) -> dict[str, Any] | None:
        try:
            with open(self.record_path(work_id), encoding="utf-8") as stream:
                record = json.load(stream)
            return record if isinstance(record, dict) else None
        except (OSError, ValueError, TypeError):
            return None

    @staticmethod
    def _timestamp(value: Any) -> float | None:
        try:
            timestamp = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(timestamp) or timestamp < 0.0:
            return None
        return timestamp

    @classmethod
    def _valid_record(
        cls,
        record: dict[str, Any] | None,
        work_id: str,
    ) -> bool:
        return bool(
            isinstance(record, dict)
            and record.get("schema") == 1
            and record.get("id") == work_id
            and record.get("status") in {"leased", "committing", "done"}
            and isinstance(record.get("payload"), dict)
            and isinstance(record.get("token"), str)
            and bool(record.get("token"))
            and cls._timestamp(record.get("updated")) is not None
            and ("commit" not in record or isinstance(record.get("commit"), dict))
        )

    @classmethod
    def _now(cls, value: float | None) -> float:
        timestamp = cls._timestamp(time.time() if value is None else value)
        if timestamp is None:
            raise ValueError("lease timestamp must be finite and non-negative")
        return timestamp

    def _write_record(
        self,
        work_id: str,
        record: dict[str, Any],
        *,
        sync_batch: _DirectorySyncBatch | None = None,
    ) -> None:
        path = self.record_path(work_id)
        durable_makedirs(os.path.dirname(path))
        tmp = f"{path}.{os.getpid()}.{time.time_ns()}.tmp"
        published = False
        try:
            with open(tmp, "w", encoding="utf-8") as stream:
                json.dump(record, stream, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            if sync_batch is None:
                durable_replace(tmp, path)
            else:
                sync_batch.replace(tmp, path)
            published = True
        finally:
            if not published:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def claim(
        self,
        work_id: str,
        payload: dict[str, Any],
        *,
        owner: str,
        worker: int = 0,
        lease_ttl: float | None = None,
        now: float | None = None,
    ) -> str | None:
        if not self.valid_work_id(work_id):
            return None
        now = self._now(now)
        if lease_ttl is None:
            effective_ttl = self.lease_ttl
        else:
            effective_ttl = self._timestamp(lease_ttl)
            if effective_ttl is None:
                raise ValueError("claim lease TTL must be finite and non-negative")
        owner = str(owner or "master")
        token = f"{owner}:{int(worker)}:{os.getpid()}:{time.time_ns()}"
        with self._locked(work_id):
            previous = self._read_record(work_id)
            if previous is None and os.path.exists(self.record_path(work_id)):
                return None
            if previous is not None and not self._valid_record(previous, work_id):
                return None
            if previous and previous.get("status") == "done":
                return None
            if previous and previous.get("status") == "committing":
                # begin_commit is the irreversible fencing decision.  Letting
                # another coordinator steal it on a wall-clock timeout would
                # allow the old holder to resume and publish after ownership
                # changed.  Only pre-commit leases are recoverable.
                return None
            if previous and previous.get("status") == "leased":
                updated = self._timestamp(previous.get("updated"))
                assert updated is not None
                if effective_ttl > 0.0 and now - updated < effective_ttl:
                    return None
            attempts = 1
            steals = 0
            previous_owner = ""
            if previous:
                try:
                    attempts = int(previous.get("attempts", 0) or 0) + 1
                    steals = int(previous.get("steals", 0) or 0)
                except (TypeError, ValueError, OverflowError):
                    attempts = 1
                    steals = 0
                previous_owner = str(previous.get("owner", "") or "")
                if previous_owner and previous_owner != owner:
                    steals += 1
            self._write_record(
                work_id,
                {
                    "schema": 1,
                    "id": work_id,
                    "status": "leased",
                    "payload": dict(payload),
                    "owner": owner,
                    "previous_owner": previous_owner,
                    "worker": int(worker),
                    "token": token,
                    "updated": now,
                    "attempts": attempts,
                    "steals": steals,
                },
            )
        return token

    def heartbeat(
        self,
        work_id: str,
        token: str,
        *,
        now: float | None = None,
    ) -> bool:
        if not self.valid_work_id(work_id) or not token:
            return False
        result = self.heartbeat_many({work_id: token}, now=now)
        return result.renewed == (work_id,)

    def heartbeat_many(
        self,
        leases: Mapping[str, str],
        *,
        now: float | None = None,
    ) -> LeaseHeartbeatBatch:
        """Renew leases with one directory barrier per affected shard.

        Every replacement retains its own file fsync and per-record fencing
        lock.  Directory barriers are deferred only to the end of this natural
        heartbeat batch.  The ``finally`` block flushes already-published
        entries when a later record fails; a barrier failure propagates, so the
        caller can never acknowledge durability that the filesystem rejected.
        """
        if not isinstance(leases, Mapping):
            raise TypeError("lease heartbeat batch must be a mapping")

        valid: list[tuple[int, str, str]] = []
        lost: list[str] = []
        for work_id, raw_token in leases.items():
            token = raw_token if isinstance(raw_token, str) else str(raw_token or "")
            if not self.valid_work_id(work_id) or not token:
                if isinstance(work_id, str):
                    lost.append(work_id)
                continue
            valid.append((self.shard_for_work(work_id), work_id, token))

        if not valid:
            return LeaseHeartbeatBatch((), tuple(sorted(lost)), 0)

        timestamp = self._now(now)
        renewed: list[str] = []
        sync_batch = _DirectorySyncBatch()
        directory_syncs = 0
        try:
            for _shard, work_id, token in sorted(valid):
                with self._locked(work_id):
                    record = self._read_record(work_id)
                    if (
                        not self._valid_record(record, work_id)
                        or record.get("status") not in {"leased", "committing"}
                        or str(record.get("token", "")) != token
                    ):
                        lost.append(work_id)
                        continue
                    assert record is not None
                    record["updated"] = timestamp
                    self._write_record(work_id, record, sync_batch=sync_batch)
                    renewed.append(work_id)
            directory_syncs = sync_batch.pending_count
        finally:
            sync_batch.flush()

        return LeaseHeartbeatBatch(
            tuple(sorted(renewed)),
            tuple(sorted(lost)),
            directory_syncs,
        )

    def abandon(self, work_id: str, token: str) -> bool:
        """Release a current pre-commit lease after dispatch did not happen."""
        if not self.valid_work_id(work_id) or not token:
            return False
        with self._locked(work_id):
            record = self._read_record(work_id)
            if (
                not self._valid_record(record, work_id)
                or record.get("status") != "leased"
                or str(record.get("token", "")) != str(token)
            ):
                return False
            try:
                durable_unlink(self.record_path(work_id))
            except OSError:
                return False
        return True

    def begin_commit(
        self,
        work_id: str,
        token: str,
        *,
        commit: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> bool:
        """Irreversibly fence a worker result before it mutates shared state.

        A committing record is deliberately not lease-recoverable.  Recovery
        without a transaction journal could otherwise race a resumed holder
        that has already crossed this decision point.
        """
        if not self.valid_work_id(work_id) or not token:
            return False
        if commit is not None and not isinstance(commit, dict):
            raise ValueError("commit manifest must be a dictionary")
        commit_copy = dict(commit) if commit is not None else None
        now = self._now(now)
        with self._locked(work_id):
            record = self._read_record(work_id)
            if (
                not self._valid_record(record, work_id)
                or record.get("status") not in {"leased", "committing"}
                or str(record.get("token", "")) != str(token)
            ):
                return False
            if record.get("status") == "committing":
                existing = record.get("commit")
                if commit_copy is not None and existing != commit_copy:
                    return False
            record["status"] = "committing"
            record["updated"] = now
            if commit_copy is not None:
                record["commit"] = commit_copy
            self._write_record(work_id, record)
        return True

    def complete_once(
        self,
        work_id: str,
        token: str,
        *,
        now: float | None = None,
    ) -> str:
        """Complete a current token and distinguish the unique state change."""
        if not self.valid_work_id(work_id) or not token:
            return "stale"
        now = self._now(now)
        with self._locked(work_id):
            record = self._read_record(work_id)
            if not self._valid_record(record, work_id):
                return "stale"
            if record.get("status") == "done":
                return (
                    "already" if str(record.get("token", "")) == str(token) else "stale"
                )
            if record.get("status") not in {"leased", "committing"} or str(
                record.get("token", "")
            ) != str(token):
                return "stale"
            record["status"] = "done"
            record["completed"] = now
            self._write_record(work_id, record)
        return "completed"

    def replay_commit_once(
        self,
        work_id: str,
        token: str,
        publish: Callable[[dict[str, Any]], None],
        *,
        now: float | None = None,
    ) -> str:
        """Publish one WAL decision and complete it under the record lock.

        The callback runs only for the current committing token.  Keeping the
        record lock across replay prevents another coordinator from removing
        staging objects while this coordinator verifies them.  A callback
        failure leaves the durable record in ``committing`` state for retry.
        """
        if not self.valid_work_id(work_id) or not token or not callable(publish):
            return "stale"
        now = self._now(now)
        with self._locked(work_id):
            record = self._read_record(work_id)
            if not self._valid_record(record, work_id):
                return "stale"
            if record.get("status") == "done":
                return (
                    "already"
                    if str(record.get("token", "")) == str(token)
                    else "stale"
                )
            if (
                record.get("status") != "committing"
                or str(record.get("token", "")) != str(token)
                or not isinstance(record.get("commit"), dict)
            ):
                return "stale"
            publish(dict(record["commit"]))
            record["status"] = "done"
            record["completed"] = now
            self._write_record(work_id, record)
        return "completed"

    def complete(
        self,
        work_id: str,
        token: str,
        *,
        now: float | None = None,
    ) -> bool:
        return self.complete_once(work_id, token, now=now) in {"completed", "already"}

    def recover_expired(
        self,
        *,
        now: float | None = None,
        lease_ttl: float | None = None,
        limit: int = 0,
    ) -> list[dict[str, Any]]:
        return [
            payload
            for _work_id, payload in self.recover_expired_records(
                now=now,
                lease_ttl=lease_ttl,
                limit=limit,
            )
        ]

    def recover_expired_records(
        self,
        *,
        now: float | None = None,
        lease_ttl: float | None = None,
        limit: int = 0,
    ) -> list[tuple[str, dict[str, Any]]]:
        """Return expired payloads bound to their authoritative record ids."""
        now = self._now(now)
        if lease_ttl is None:
            ttl = self.lease_ttl
        else:
            ttl = self._timestamp(lease_ttl)
            if ttl is None:
                raise ValueError("lease TTL must be finite and non-negative")
        expired: list[tuple[float, str, dict[str, Any]]] = []
        for shard in range(self.shard_count):
            directory = self.shard_dir(shard)
            try:
                names = os.listdir(directory)
            except OSError:
                continue
            for name in names:
                if not name.endswith(".json"):
                    continue
                work_id = name[:-5]
                if not self.valid_work_id(work_id):
                    continue
                record = self._read_record(work_id)
                if (
                    not self._valid_record(record, work_id)
                    or record.get("status") != "leased"
                ):
                    continue
                updated = self._timestamp(record.get("updated"))
                assert updated is not None
                if ttl > 0.0 and now - updated < ttl:
                    continue
                payload = record.get("payload")
                if isinstance(payload, dict):
                    expired.append((updated, work_id, dict(payload)))
        expired.sort(key=lambda item: (item[0], item[1]))
        if limit > 0:
            expired = expired[:limit]
        return [(work_id, payload) for _updated, work_id, payload in expired]

    def snapshot_records(
        self,
        *,
        limit: int = 0,
    ) -> list[tuple[str, dict[str, Any]]]:
        """Return a stable, structurally validated snapshot of all records."""
        records: list[tuple[float, str, dict[str, Any]]] = []
        for shard in range(self.shard_count):
            directory = self.shard_dir(shard)
            try:
                names = os.listdir(directory)
            except OSError:
                continue
            for name in names:
                if not name.endswith(".json"):
                    continue
                work_id = name[:-5]
                if not self.valid_work_id(work_id):
                    continue
                record = self._read_record(work_id)
                if not self._valid_record(record, work_id):
                    # Records are published with atomic replace, so a listed
                    # final path that still exists but cannot be validated is
                    # persistent corruption, not an in-progress write.  Do not
                    # let a recovery coordinator mistake that state for an
                    # empty/committed epoch and remove the journal.
                    if os.path.exists(self.record_path(work_id)):
                        raise ValueError(f"invalid fenced lease record: {work_id}")
                    continue
                updated = self._timestamp(record.get("updated"))
                assert updated is not None
                records.append((updated, work_id, dict(record)))
        records.sort(key=lambda item: (item[0], item[1]))
        if limit > 0:
            records = records[:limit]
        return [(work_id, record) for _updated, work_id, record in records]

    def recover_committing_records(
        self,
        *,
        limit: int = 0,
    ) -> list[tuple[str, dict[str, Any], str, dict[str, Any]]]:
        """Return durable commit decisions that are safe to redo idempotently."""
        committing: list[tuple[str, dict[str, Any], str, dict[str, Any]]] = []
        for work_id, record in self.snapshot_records():
            if record.get("status") != "committing":
                continue
            payload = record.get("payload")
            token = record.get("token")
            commit = record.get("commit")
            if (
                not isinstance(payload, dict)
                or not isinstance(token, str)
                or not token
                or not isinstance(commit, dict)
            ):
                raise ValueError(f"committing record lacks durable manifest: {work_id}")
            committing.append(
                (
                    work_id,
                    dict(payload),
                    token,
                    dict(commit),
                )
            )
        if limit > 0:
            committing = committing[:limit]
        return committing

    def snapshot_counts(self, *, now: float | None = None) -> dict[str, int]:
        now = self._now(now)
        counts = {"leased": 0, "committing": 0, "expired": 0, "done": 0}
        for shard in range(self.shard_count):
            try:
                names = os.listdir(self.shard_dir(shard))
            except OSError:
                continue
            for name in names:
                if not name.endswith(".json"):
                    continue
                work_id = name[:-5]
                if not self.valid_work_id(work_id):
                    continue
                record = self._read_record(work_id)
                if not self._valid_record(record, work_id):
                    continue
                status = str(record.get("status", ""))
                if status == "done":
                    counts["done"] += 1
                elif status in {"leased", "committing"}:
                    counts[status] += 1
                    if status == "leased":
                        updated = self._timestamp(record.get("updated"))
                        assert updated is not None
                        if now - updated >= self.lease_ttl:
                            counts["expired"] += 1
        return counts


class FencedTargetLeaseTable(FencedWorkLeaseTable):
    """Group-atomic cross-coordinator leases for branch targets.

    A work lease identifies one exact seed execution.  A target lease instead
    protects every non-skipped branch in a directed replay job, so two masters
    do not intentionally dispatch overlapping target groups while the lease is
    current, even when they start from different seeds.  All per-target update
    locks are acquired in digest order; cooperating coordinators observe the
    overlap check and record publication as one transaction without deadlock.

    Target records are released rather than marked done because a branch may be
    worth revisiting with another seed after the current solve has finished.
    Rejected operations are validated before any mutation.  Successful group
    mutations file-fsync each record and group directory barriers by shard.
    Claim-side crash residue is conservative and TTL-bounded. Release rollback
    is best effort: this protocol does not claim cross-file crash atomicity, so
    a persistent storage fault can weaken target de-duplication until recovery.
    """

    MAX_TARGETS = 64

    @classmethod
    def normalize_targets(cls, targets: Iterable[Any]) -> tuple[int, ...]:
        normalized: list[int] = []
        seen: set[int] = set()
        for value in targets:
            try:
                target = int(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if target <= 0 or target in seen:
                continue
            normalized.append(target)
            seen.add(target)
            if len(normalized) >= cls.MAX_TARGETS:
                break
        return tuple(normalized)

    @staticmethod
    def target_id(target: int) -> str:
        return hashlib.sha256(f"target:{int(target)}".encode("ascii")).hexdigest()

    @classmethod
    def _target_ids(
        cls,
        targets: Iterable[Any],
    ) -> tuple[tuple[int, str], ...]:
        return tuple(
            sorted(
                (
                    (target, cls.target_id(target))
                    for target in cls.normalize_targets(targets)
                ),
                key=lambda item: item[1],
            )
        )

    @staticmethod
    def _active(record: dict[str, Any] | None, now: float, ttl: float) -> bool:
        if not record or record.get("status") != "leased":
            return False
        try:
            updated = float(record.get("updated", 0.0) or 0.0)
        except (TypeError, ValueError, OverflowError):
            updated = 0.0
        return now - updated < ttl

    def claim_group(
        self,
        targets: Iterable[Any],
        payload: dict[str, Any],
        *,
        owner: str,
        worker: int = 0,
        now: float | None = None,
    ) -> str | None:
        target_ids = self._target_ids(targets)
        if not target_ids:
            return None
        now = self._now(now)
        owner = str(owner or "master")
        worker = int(worker)
        payload = dict(payload)
        # Reject unsupported values before lock acquisition and publication.
        json.dumps(payload, sort_keys=True, separators=(",", ":"))
        token = f"{owner}:{worker}:{os.getpid()}:{time.time_ns()}"
        with ExitStack() as stack:
            for _target, target_id in target_ids:
                stack.enter_context(self._locked(target_id))
            previous = {
                target_id: self._read_record(target_id)
                for _target, target_id in target_ids
            }
            if any(
                (
                    previous[target_id] is None
                    and os.path.exists(self.record_path(target_id))
                )
                or (
                    previous[target_id] is not None
                    and not self._valid_record(previous[target_id], target_id)
                )
                for _target, target_id in target_ids
            ):
                return None
            if any(
                self._active(previous[target_id], now, self.lease_ttl)
                for _target, target_id in target_ids
            ):
                return None

            group = [target for target, _target_id in target_ids]
            publications: dict[str, dict[str, Any]] = {}
            for target, target_id in target_ids:
                old = previous[target_id]
                attempts = 1
                steals = 0
                previous_owner = ""
                if old:
                    try:
                        attempts = int(old.get("attempts", 0) or 0) + 1
                        steals = int(old.get("steals", 0) or 0)
                    except (TypeError, ValueError, OverflowError):
                        attempts = 1
                        steals = 0
                    previous_owner = str(old.get("owner", "") or "")
                    if previous_owner and previous_owner != owner:
                        steals += 1
                publications[target_id] = {
                    "schema": 1,
                    "id": target_id,
                    "target": target,
                    "targets": group,
                    "status": "leased",
                    "payload": dict(payload),
                    "owner": owner,
                    "previous_owner": previous_owner,
                    "worker": worker,
                    "token": token,
                    "updated": now,
                    "attempts": attempts,
                    "steals": steals,
                }

            # Validate every JSON payload before publishing the first member.
            # This prevents a late serialization error from becoming a partial
            # group mutation; _write_record performs the actual durable write.
            for record in publications.values():
                json.dumps(record, sort_keys=True, separators=(",", ":"))

            written: list[str] = []
            sync_batch = _DirectorySyncBatch()
            try:
                for _target, target_id in target_ids:
                    self._write_record(
                        target_id,
                        publications[target_id],
                        sync_batch=sync_batch,
                    )
                    written.append(target_id)
                sync_batch.flush()
            except OSError:
                # Best-effort rollback while all group locks are still held.
                # If rollback itself fails, the surviving record is a safe,
                # conservative lease and will expire after lease_ttl.
                for target_id in written:
                    try:
                        old = previous[target_id]
                        if old is None:
                            sync_batch.unlink(self.record_path(target_id))
                        else:
                            self._write_record(target_id, old, sync_batch=sync_batch)
                    except OSError:
                        pass
                try:
                    sync_batch.flush()
                except OSError:
                    pass
                return None
        return token

    def heartbeat_group(
        self,
        targets: Iterable[Any],
        token: str,
        *,
        now: float | None = None,
    ) -> bool:
        target_ids = self._target_ids(targets)
        if not target_ids or not token:
            return False
        now = self._now(now)
        with ExitStack() as stack:
            for _target, target_id in target_ids:
                stack.enter_context(self._locked(target_id))
            records = [
                self._read_record(target_id) for _target, target_id in target_ids
            ]
            if any(
                not self._valid_record(record, target_id)
                or record.get("status") != "leased"
                or str(record.get("token", "")) != str(token)
                for (_target, target_id), record in zip(target_ids, records)
            ):
                return False
            previous = [dict(record) for record in records if record is not None]
            written: list[int] = []
            sync_batch = _DirectorySyncBatch()
            try:
                for index, ((_target, target_id), record) in enumerate(
                    zip(target_ids, records)
                ):
                    assert record is not None
                    renewed = dict(record)
                    renewed["updated"] = now
                    self._write_record(target_id, renewed, sync_batch=sync_batch)
                    written.append(index)
                sync_batch.flush()
            except OSError:
                # Preserve one group-wide expiry point if a later member cannot
                # be renewed. Rollback is best effort for the same reason as
                # claim_group(): a surviving renewal is conservative and still
                # remains protected by its current token.
                for index in written:
                    try:
                        self._write_record(target_ids[index][1], previous[index])
                    except OSError:
                        pass
                return False
        return True

    def release_group(self, targets: Iterable[Any], token: str) -> bool:
        target_ids = self._target_ids(targets)
        if not target_ids or not token:
            return False
        with ExitStack() as stack:
            for _target, target_id in target_ids:
                stack.enter_context(self._locked(target_id))
            records = [
                self._read_record(target_id) for _target, target_id in target_ids
            ]
            if any(
                not self._valid_record(record, target_id)
                or record.get("status") != "leased"
                or str(record.get("token", "")) != str(token)
                for (_target, target_id), record in zip(target_ids, records)
            ):
                return False

            previous = [dict(record) for record in records if record is not None]
            removed: list[int] = []
            sync_batch = _DirectorySyncBatch()
            try:
                for index, (_target, target_id) in enumerate(target_ids):
                    sync_batch.unlink(self.record_path(target_id))
                    removed.append(index)
                sync_batch.flush()
            except OSError:
                # Restore the exact fenced records while all group locks remain
                # held. If storage also rejects restoration, a partial release
                # can weaken target de-duplication; work/result fencing remains
                # authoritative and this API does not claim crash atomicity.
                for index in removed:
                    try:
                        self._write_record(
                            target_ids[index][1],
                            previous[index],
                            sync_batch=sync_batch,
                        )
                    except OSError:
                        pass
                try:
                    sync_batch.flush()
                except OSError:
                    pass
                return False
        return True


def _valid_hex_digest(value: str, width: int = 64) -> bool:
    return len(value) == width and all(char in "0123456789abcdef" for char in value)


def _normalize_state_actions(raw: Any) -> tuple[tuple[int, str], ...]:
    if not isinstance(raw, (list, tuple)):
        return ()
    actions: list[tuple[int, str]] = []
    seen: set[int] = set()
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            branch = max(0, int(item[0]))
        except (TypeError, ValueError):
            continue
        if branch <= 0 or branch in seen:
            continue
        action = str(item[1]).strip().lower()
        if action not in {"solve", "sample", "skip"}:
            continue
        actions.append((branch, action))
        seen.add(branch)
    return tuple(actions)


_CONTINUATION_SCHEMA = "symcc-live-continuation-v1"
_LIVE_EXPRESSION_SCHEMA = "symcc-live-expression-v1"
_LIVE_SOLVER_FRAME_SCHEMA = "symcc-live-solver-frame-v1"
_LIVE_SYMBOLIC_STORE_SCHEMA = "symcc-live-symbolic-store-v1"
_LIVE_MEMORY_PAGE_SCHEMA = "symcc-live-memory-page-v1"
_LIVE_MEMORY_ROOT_SCHEMA = "symcc-live-memory-root-v1"
_LIVE_PROGRAM_SCHEMA = "symcc-live-program-v1"
_DEFAULT_LIVE_GRAPH_MAX_OBJECTS = 262_144
_DEFAULT_LIVE_GRAPH_MAX_BYTES = 256 * 1024 * 1024


def _bounded_text(value: Any, *, limit: int = 128) -> str:
    return str(value or "").strip()[:limit]


@dataclass(frozen=True)
class ContinuationFrame:
    """Serializable program point for a future live symbolic state."""

    function: str
    block: str
    instruction: int = 0
    call_depth: int = 0

    @classmethod
    def from_mapping(cls, raw: Any) -> "ContinuationFrame | None":
        if not isinstance(raw, dict):
            return None
        try:
            instruction = max(0, int(raw.get("instruction", 0) or 0))
            call_depth = max(0, int(raw.get("call_depth", 0) or 0))
        except (TypeError, ValueError, OverflowError):
            instruction = 0
            call_depth = 0
        function = _bounded_text(raw.get("function"))
        block = _bounded_text(raw.get("block"))
        if not function and not block and instruction == 0:
            return None
        return cls(function, block, instruction, call_depth)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "function": self.function,
            "block": self.block,
            "instruction": self.instruction,
            "call_depth": self.call_depth,
        }


@dataclass(frozen=True)
class LiveContinuationDescriptor:
    """Canonical checkpoint descriptor for GenSym-style live-state work."""

    engine: str
    frames: tuple[ContinuationFrame, ...]
    path_condition_root: str = ""
    symbolic_store_root: str = ""
    symbolic_memory_root: str = ""
    program_root: str = ""
    parent: str = ""
    target_branch: int = 0
    search_path_depth: int = 0
    search_instructions: int = 0
    search_solver_queries: int = 0
    search_goal_branch: int = 0
    search_branch_path: tuple[int, ...] = ()
    search_recent_locations: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, raw: Any) -> "LiveContinuationDescriptor | None":
        if not isinstance(raw, dict):
            return None
        schema = str(raw.get("schema", _CONTINUATION_SCHEMA))
        if schema != _CONTINUATION_SCHEMA:
            return None
        raw_frames = raw.get("frames", ())
        parsed_frames: list[ContinuationFrame] = []
        if isinstance(raw_frames, list):
            for item in raw_frames[:32]:
                frame = ContinuationFrame.from_mapping(item)
                if frame is not None:
                    parsed_frames.append(frame)
        if not parsed_frames:
            frame = ContinuationFrame.from_mapping(raw.get("frame", raw.get("pc", {})))
            if frame is not None:
                parsed_frames.append(frame)
        roots = raw.get("roots", {})
        roots = roots if isinstance(roots, dict) else {}
        path_condition_root = str(
            raw.get("path_condition_root")
            or roots.get("path_condition")
            or raw.get("path_condition", "")
            or ""
        )
        symbolic_store_root = str(
            raw.get("symbolic_store_root")
            or roots.get("symbolic_store")
            or raw.get("symbolic_store", "")
            or ""
        )
        symbolic_memory_root = str(
            raw.get("symbolic_memory_root")
            or roots.get("symbolic_memory")
            or raw.get("symbolic_memory", "")
            or ""
        )
        program_root = str(
            raw.get("program_root")
            or roots.get("program")
            or raw.get("program", "")
            or ""
        )
        parent = str(raw.get("parent", "") or "")

        def digest(value: str) -> str:
            return value if _valid_hex_digest(value) else ""

        try:
            target_branch = max(0, int(raw.get("target_branch", 0) or 0))
        except (TypeError, ValueError, OverflowError):
            target_branch = 0
        search = raw.get("search", {})
        if not isinstance(search, dict):
            return None

        def search_uint(name: str, maximum: int) -> int | None:
            value = search.get(name, 0)
            if isinstance(value, bool) or (
                    isinstance(value, float) and not value.is_integer()):
                return None
            try:
                parsed = int(value or 0)
            except (TypeError, ValueError, OverflowError):
                return None
            return parsed if 0 <= parsed <= maximum else None

        search_path_depth = search_uint("path_depth", (1 << 63) - 1)
        search_instructions = search_uint("instructions", (1 << 63) - 1)
        search_solver_queries = search_uint("solver_queries", (1 << 63) - 1)
        search_goal_branch = search_uint("goal_branch", (1 << 64) - 1)
        if None in {
                search_path_depth, search_instructions,
                search_solver_queries, search_goal_branch}:
            return None
        raw_branch_path = search.get("branch_path", ())
        if not isinstance(raw_branch_path, (list, tuple)) \
                or len(raw_branch_path) > 256:
            return None
        branch_path: list[int] = []
        for value in raw_branch_path:
            if isinstance(value, bool) or (
                    isinstance(value, float) and not value.is_integer()):
                return None
            try:
                token = int(value)
            except (TypeError, ValueError, OverflowError):
                return None
            if not 0 <= token <= (1 << 64) - 1:
                return None
            branch_path.append(token)
        raw_locations = search.get("recent_locations", ())
        if not isinstance(raw_locations, (list, tuple)) \
                or len(raw_locations) > 16:
            return None
        recent_locations: list[str] = []
        for value in raw_locations:
            if not isinstance(value, str) or not value \
                    or len(value.encode("utf-8")) > 512 \
                    or "\x00" in value:
                return None
            recent_locations.append(value)
        descriptor = cls(
            engine=_bounded_text(raw.get("engine", "symcc"), limit=32),
            frames=tuple(parsed_frames),
            path_condition_root=digest(path_condition_root),
            symbolic_store_root=digest(symbolic_store_root),
            symbolic_memory_root=digest(symbolic_memory_root),
            program_root=digest(program_root),
            parent=digest(parent),
            target_branch=target_branch,
            search_path_depth=int(search_path_depth),
            search_instructions=int(search_instructions),
            search_solver_queries=int(search_solver_queries),
            search_goal_branch=int(search_goal_branch),
            search_branch_path=tuple(branch_path),
            search_recent_locations=tuple(recent_locations),
        )
        if (
            not descriptor.frames
            and not descriptor.path_condition_root
            and not descriptor.symbolic_store_root
            and not descriptor.symbolic_memory_root
            and not descriptor.program_root
            and not descriptor.parent
        ):
            return None
        return descriptor

    def to_mapping(self) -> dict[str, Any]:
        result = {
            "schema": _CONTINUATION_SCHEMA,
            "engine": self.engine,
            "frames": [frame.to_mapping() for frame in self.frames],
            "path_condition_root": self.path_condition_root,
            "symbolic_store_root": self.symbolic_store_root,
            "symbolic_memory_root": self.symbolic_memory_root,
            "parent": self.parent,
            "target_branch": self.target_branch,
        }
        if self.program_root:
            result["program_root"] = self.program_root
        if any((
                self.search_path_depth,
                self.search_instructions,
                self.search_solver_queries,
                self.search_goal_branch,
                self.search_branch_path,
                self.search_recent_locations,
        )):
            result["search"] = {
                "path_depth": self.search_path_depth,
                "instructions": self.search_instructions,
                "solver_queries": self.search_solver_queries,
                "goal_branch": self.search_goal_branch,
                "branch_path": list(self.search_branch_path),
                "recent_locations": list(self.search_recent_locations),
            }
        return result

    def checkpoint_id(self) -> str:
        canonical = json.dumps(self.to_mapping(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LiveContinuationBundle:
    """Materialized, digest-checked state needed by a resume adapter."""

    checkpoint_id: str
    descriptor: LiveContinuationDescriptor
    solver_frames: tuple[tuple[str, ...], ...]
    symbolic_store: tuple[tuple[str, str], ...]
    memory_size: int
    memory_page_size: int
    memory_pages: tuple[tuple[int, str], ...]
    graph_object_count: int = 0
    graph_canonical_bytes: int = 0


class _LiveStateTraversalBudget:
    """Bound and memoize one transitive continuation restore."""

    def __init__(self, max_objects: int, max_bytes: int) -> None:
        self.max_objects = max_objects
        self.max_bytes = max_bytes
        self.canonical_bytes = 0
        self.mappings: dict[str, dict[str, Any]] = {}

    @property
    def object_count(self) -> int:
        return len(self.mappings)

    def cached(
        self,
        object_id: str,
        schema: str | None,
    ) -> dict[str, Any] | None:
        value = self.mappings.get(object_id)
        if value is None:
            return None
        if schema is not None and value.get("schema") != schema:
            raise ValueError(f"live-state object is not schema {schema}")
        return value

    def ensure_capacity(self, object_id: str, content_bytes: int) -> None:
        if object_id in self.mappings:
            return
        if self.object_count >= self.max_objects:
            raise ValueError("live-state graph exceeds object budget")
        if self.canonical_bytes + content_bytes > self.max_bytes:
            raise ValueError("live-state graph exceeds canonical-byte budget")

    def remember(
        self,
        object_id: str,
        content_bytes: int,
        value: dict[str, Any],
    ) -> None:
        if object_id in self.mappings:
            return
        self.mappings[object_id] = value
        self.canonical_bytes += content_bytes


class LiveStateStore:
    """Content-addressed continuation state with page-level COW memory."""

    def __init__(
        self,
        root: str,
        *,
        page_size: int = 4096,
        max_object_bytes: int = 16 * 1024 * 1024,
        max_graph_objects: int = _DEFAULT_LIVE_GRAPH_MAX_OBJECTS,
        max_graph_bytes: int = _DEFAULT_LIVE_GRAPH_MAX_BYTES,
    ) -> None:
        for name, value in (
            ("max_graph_objects", max_graph_objects),
            ("max_graph_bytes", max_graph_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self.root = os.path.abspath(root)
        self.page_size = max(64, min(65536, int(page_size)))
        self.max_object_bytes = max(1024, int(max_object_bytes))
        self.max_graph_objects = max_graph_objects
        self.max_graph_bytes = max_graph_bytes
        self.objects = os.path.join(self.root, "objects")
        self._object_store = ContentAddressedInputStore(
            self.objects,
            self.max_object_bytes,
            object_suffix=".json",
        )

    @staticmethod
    def _canonical(value: Any) -> bytes:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")

    @staticmethod
    def _digest(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    def object_path(self, object_id: str) -> str:
        if not _valid_hex_digest(object_id):
            raise ValueError("invalid live-state object id")
        return self._object_store.object_path(object_id)

    def _put_mapping(self, value: Mapping[str, Any]) -> str:
        content = self._canonical(dict(value))
        if len(content) > self.max_object_bytes:
            raise ValueError("live-state object exceeds size limit")
        object_id = self._digest(content)
        self._object_store.put(content, object_id)
        return object_id

    def _get_mapping(
        self,
        object_id: str,
        *,
        schema: str | None = None,
        _budget: _LiveStateTraversalBudget | None = None,
    ) -> dict[str, Any]:
        if _budget is not None:
            cached = _budget.cached(object_id, schema)
            if cached is not None:
                return cached
        if not _valid_hex_digest(object_id):
            raise ValueError("invalid live-state object id")
        try:
            snapshot = self._object_store.snapshot(
                object_id,
                retain_content=True,
            )
        except OSError as exc:
            self._object_store._verified_identities.pop(object_id, None)
            raise ValueError("live-state object is missing or unstable") from exc
        except ValueError as exc:
            self._object_store._verified_identities.pop(object_id, None)
            raise ValueError("live-state object exceeds size limit") from exc
        if snapshot.sha256 != object_id:
            self._object_store._verified_identities.pop(object_id, None)
            raise ValueError("live-state object digest mismatch")
        assert snapshot.content is not None
        content = snapshot.content
        if _budget is not None:
            _budget.ensure_capacity(object_id, len(content))
        try:
            value = json.loads(content.decode("ascii"))
        except (UnicodeDecodeError, ValueError) as exc:
            self._object_store._verified_identities.pop(object_id, None)
            raise ValueError("invalid live-state JSON object") from exc
        if not isinstance(value, dict):
            self._object_store._verified_identities.pop(object_id, None)
            raise ValueError("live-state object must be a mapping")
        if schema is not None and value.get("schema") != schema:
            self._object_store._verified_identities.pop(object_id, None)
            raise ValueError(f"live-state object is not schema {schema}")
        self._object_store._remember_verified_identity(object_id, snapshot.identity)
        if _budget is not None:
            _budget.remember(object_id, len(content), value)
        return value

    def has_object(self, object_id: str) -> bool:
        try:
            self._get_mapping(object_id)
            return True
        except (OSError, ValueError, TypeError):
            return False

    def put_expression(self, expression: Mapping[str, Any]) -> str:
        normalized = {
            "schema": _LIVE_EXPRESSION_SCHEMA,
            "expression": json.loads(self._canonical(dict(expression)).decode("ascii")),
        }
        return self._put_mapping(normalized)

    def get_expression(self, object_id: str) -> dict[str, Any]:
        value = self._get_mapping(object_id, schema=_LIVE_EXPRESSION_SCHEMA)
        expression = value.get("expression")
        if not isinstance(expression, dict):
            raise ValueError("invalid stored live expression")
        return dict(expression)

    def put_program(self, program: Mapping[str, Any]) -> str:
        normalized = json.loads(self._canonical(dict(program)).decode("ascii"))
        normalized["schema"] = _LIVE_PROGRAM_SCHEMA
        return self._put_mapping(normalized)

    def get_program(self, object_id: str) -> dict[str, Any]:
        return self._get_mapping(object_id, schema=_LIVE_PROGRAM_SCHEMA)

    def put_solver_frame(
        self,
        parent: str,
        assertions: Iterable[str],
    ) -> str:
        if parent:
            parent_frame = self._get_mapping(parent, schema=_LIVE_SOLVER_FRAME_SCHEMA)
            depth = int(parent_frame["depth"]) + 1
        else:
            depth = 1
        normalized_assertions: list[str] = []
        for assertion in assertions:
            digest = str(assertion)
            if not _valid_hex_digest(digest):
                raise ValueError("solver assertion is not a stored object")
            self._get_mapping(digest, schema=_LIVE_EXPRESSION_SCHEMA)
            normalized_assertions.append(digest)
            if len(normalized_assertions) > 65536:
                raise ValueError("solver frame has too many assertions")
        return self._put_mapping(
            {
                "schema": _LIVE_SOLVER_FRAME_SCHEMA,
                "parent": parent,
                "depth": depth,
                "assertions": normalized_assertions,
            }
        )

    def restore_solver_frames(
        self,
        root: str,
        *,
        max_frames: int = 65536,
        _budget: _LiveStateTraversalBudget | None = None,
    ) -> tuple[tuple[str, ...], ...]:
        if not root:
            return ()
        frames: list[tuple[str, ...]] = []
        depths: list[int] = []
        seen: set[str] = set()
        current = root
        while current:
            if current in seen:
                raise ValueError("cycle in solver frame chain")
            if len(frames) >= max(1, int(max_frames)):
                raise ValueError("solver frame chain exceeds limit")
            seen.add(current)
            frame = self._get_mapping(
                current,
                schema=_LIVE_SOLVER_FRAME_SCHEMA,
                _budget=_budget,
            )
            assertions_raw = frame.get("assertions", ())
            if not isinstance(assertions_raw, list):
                raise ValueError("invalid solver assertions")
            if len(assertions_raw) > 65536:
                raise ValueError("solver frame has too many assertions")
            assertions = tuple(str(value) for value in assertions_raw)
            for value in assertions:
                if not _valid_hex_digest(value):
                    raise ValueError("missing solver assertion object")
                self._get_mapping(
                    value,
                    schema=_LIVE_EXPRESSION_SCHEMA,
                    _budget=_budget,
                )
            frames.append(assertions)
            depths.append(int(frame.get("depth", 0)))
            current = str(frame.get("parent", ""))
            if current and not _valid_hex_digest(current):
                raise ValueError("invalid solver frame parent")
        frames.reverse()
        depths.reverse()
        if depths != list(range(1, len(depths) + 1)):
            raise ValueError("invalid solver frame depth chain")
        return tuple(frames)

    def get_solver_frame(
        self,
        root: str,
    ) -> tuple[str, tuple[str, ...], int]:
        """Return one validated immutable solver frame for incremental reuse."""
        if not _valid_hex_digest(str(root)):
            raise ValueError("invalid solver frame root")
        frame = self._get_mapping(str(root), schema=_LIVE_SOLVER_FRAME_SCHEMA)
        parent = str(frame.get("parent", ""))
        if parent and not _valid_hex_digest(parent):
            raise ValueError("invalid solver frame parent")
        assertions_raw = frame.get("assertions", ())
        if not isinstance(assertions_raw, list):
            raise ValueError("invalid solver assertions")
        assertions = tuple(str(value) for value in assertions_raw)
        for value in assertions:
            if not _valid_hex_digest(value):
                raise ValueError("missing solver assertion object")
            self._get_mapping(value, schema=_LIVE_EXPRESSION_SCHEMA)
        depth = int(frame.get("depth", 0))
        if depth < 1 or depth > 65536:
            raise ValueError("invalid solver frame depth")
        return parent, assertions, depth

    def put_symbolic_store(
        self,
        values: Mapping[str, str],
    ) -> str:
        entries: list[list[str]] = []
        for raw_name, raw_digest in sorted(values.items()):
            name = str(raw_name)
            digest = str(raw_digest)
            if not name or len(name) > 256:
                raise ValueError("invalid symbolic store name")
            if not _valid_hex_digest(digest):
                raise ValueError("symbolic store value is not a stored object")
            self._get_mapping(digest, schema=_LIVE_EXPRESSION_SCHEMA)
            entries.append([name, digest])
            if len(entries) > 1_000_000:
                raise ValueError("symbolic store exceeds entry limit")
        return self._put_mapping(
            {
                "schema": _LIVE_SYMBOLIC_STORE_SCHEMA,
                "entries": entries,
            }
        )

    def restore_symbolic_store(
        self,
        root: str,
        *,
        _budget: _LiveStateTraversalBudget | None = None,
    ) -> tuple[tuple[str, str], ...]:
        if not root:
            return ()
        store = self._get_mapping(
            root,
            schema=_LIVE_SYMBOLIC_STORE_SCHEMA,
            _budget=_budget,
        )
        entries = store.get("entries", ())
        if not isinstance(entries, list):
            raise ValueError("invalid symbolic store entries")
        if len(entries) > 1_000_000:
            raise ValueError("symbolic store exceeds entry limit")
        restored: list[tuple[str, str]] = []
        seen_names: set[str] = set()
        for entry in entries:
            if not isinstance(entry, list) or len(entry) != 2:
                raise ValueError("invalid symbolic store entry")
            name, digest = str(entry[0]), str(entry[1])
            if (
                not name
                or len(name) > 256
                or name in seen_names
                or not _valid_hex_digest(digest)
            ):
                raise ValueError("invalid symbolic store reference")
            self._get_mapping(
                digest,
                schema=_LIVE_EXPRESSION_SCHEMA,
                _budget=_budget,
            )
            seen_names.add(name)
            restored.append((name, digest))
        return tuple(restored)

    def _put_memory_page(
        self,
        concrete: bytes,
        symbolic: Mapping[int, str],
    ) -> str:
        if len(concrete) != self.page_size:
            raise ValueError("memory page has an invalid size")
        entries: list[list[Any]] = []
        for raw_offset, raw_digest in sorted(symbolic.items()):
            offset = int(raw_offset)
            digest = str(raw_digest)
            if offset < 0 or offset >= self.page_size:
                raise ValueError("symbolic byte is outside its page")
            if not _valid_hex_digest(digest):
                raise ValueError("symbolic byte is not a stored expression")
            self._get_mapping(digest, schema=_LIVE_EXPRESSION_SCHEMA)
            entries.append([offset, digest])
        return self._put_mapping(
            {
                "schema": _LIVE_MEMORY_PAGE_SCHEMA,
                "page_size": self.page_size,
                "concrete_hex": concrete.hex(),
                "symbolic": entries,
            }
        )

    def _get_memory_page(
        self,
        page_id: str,
        *,
        _budget: _LiveStateTraversalBudget | None = None,
    ) -> tuple[bytes, dict[int, str]]:
        page = self._get_mapping(
            page_id,
            schema=_LIVE_MEMORY_PAGE_SCHEMA,
            _budget=_budget,
        )
        if int(page.get("page_size", 0)) != self.page_size:
            raise ValueError("memory page size mismatch")
        try:
            concrete = bytes.fromhex(str(page["concrete_hex"]))
        except (KeyError, ValueError) as exc:
            raise ValueError("invalid concrete memory page") from exc
        if len(concrete) != self.page_size:
            raise ValueError("concrete memory page has wrong length")
        symbolic_raw = page.get("symbolic", ())
        if not isinstance(symbolic_raw, list):
            raise ValueError("invalid symbolic memory page")
        if len(symbolic_raw) > self.page_size:
            raise ValueError("symbolic memory page exceeds cell limit")
        symbolic: dict[int, str] = {}
        for entry in symbolic_raw:
            if not isinstance(entry, list) or len(entry) != 2:
                raise ValueError("invalid symbolic memory cell")
            offset, digest = int(entry[0]), str(entry[1])
            if (
                offset < 0
                or offset >= self.page_size
                or offset in symbolic
                or not _valid_hex_digest(digest)
            ):
                raise ValueError("invalid symbolic memory reference")
            self._get_mapping(
                digest,
                schema=_LIVE_EXPRESSION_SCHEMA,
                _budget=_budget,
            )
            symbolic[offset] = digest
        return concrete, symbolic

    def _put_memory_root(
        self,
        size: int,
        pages: Mapping[int, str],
    ) -> str:
        entries: list[list[Any]] = []
        for raw_index, raw_digest in sorted(pages.items()):
            index = int(raw_index)
            digest = str(raw_digest)
            if index < 0 or not self.has_object(digest):
                raise ValueError("invalid memory page reference")
            self._get_mapping(digest, schema=_LIVE_MEMORY_PAGE_SCHEMA)
            entries.append([index, digest])
        return self._put_mapping(
            {
                "schema": _LIVE_MEMORY_ROOT_SCHEMA,
                "page_size": self.page_size,
                "size": max(0, int(size)),
                "pages": entries,
            }
        )

    def _get_memory_root(
        self,
        root: str,
        *,
        validate_pages: bool = False,
        _budget: _LiveStateTraversalBudget | None = None,
    ) -> tuple[int, dict[int, str]]:
        memory = self._get_mapping(
            root,
            schema=_LIVE_MEMORY_ROOT_SCHEMA,
            _budget=_budget,
        )
        if int(memory.get("page_size", 0)) != self.page_size:
            raise ValueError("memory root page size mismatch")
        size = int(memory.get("size", 0))
        if size < 0:
            raise ValueError("invalid memory root size")
        raw_pages = memory.get("pages", ())
        if not isinstance(raw_pages, list):
            raise ValueError("invalid memory root pages")
        pages: dict[int, str] = {}
        for entry in raw_pages:
            if not isinstance(entry, list) or len(entry) != 2:
                raise ValueError("invalid memory root page entry")
            index, digest = int(entry[0]), str(entry[1])
            if (
                index < 0
                or index in pages
                or not _valid_hex_digest(digest)
                or index * self.page_size >= size
            ):
                raise ValueError("invalid memory page reference")
            if validate_pages:
                self._get_memory_page(digest, _budget=_budget)
            elif not self.has_object(digest):
                raise ValueError("missing memory page")
            pages[index] = digest
        return size, pages

    def create_memory(
        self,
        concrete: bytes = b"",
        symbolic: Mapping[int, str] | None = None,
    ) -> str:
        symbolic = symbolic or {}
        size = max(
            len(concrete),
            max((int(offset) + 1 for offset in symbolic), default=0),
        )
        pages: dict[int, str] = {}
        page_count = (size + self.page_size - 1) // self.page_size
        for page_index in range(page_count):
            start = page_index * self.page_size
            page = bytearray(self.page_size)
            chunk = concrete[start : start + self.page_size]
            page[: len(chunk)] = chunk
            page_symbolic = {
                int(offset) - start: digest
                for offset, digest in symbolic.items()
                if start <= int(offset) < start + self.page_size
            }
            pages[page_index] = self._put_memory_page(bytes(page), page_symbolic)
        return self._put_memory_root(size, pages)

    def fork_memory(
        self,
        root: str,
        *,
        concrete_writes: Mapping[int, bytes | bytearray | int] | None = None,
        symbolic_writes: Mapping[int, str | None] | None = None,
    ) -> str:
        size, pages = self._get_memory_root(root)
        concrete_writes = concrete_writes or {}
        symbolic_writes = symbolic_writes or {}
        concrete_bytes: dict[int, int] = {}
        for raw_address, raw_value in sorted(concrete_writes.items()):
            address = int(raw_address)
            if address < 0:
                raise ValueError("negative concrete write address")
            if isinstance(raw_value, int):
                if raw_value < 0 or raw_value > 255:
                    raise ValueError("concrete byte is outside uint8")
                content = bytes([raw_value])
            elif isinstance(raw_value, (bytes, bytearray)):
                content = bytes(raw_value)
            else:
                raise ValueError("unsupported concrete memory write")
            for offset, value in enumerate(content):
                concrete_bytes[address + offset] = value
        normalized_symbolic: dict[int, str | None] = {}
        for raw_address, raw_digest in symbolic_writes.items():
            address = int(raw_address)
            if address < 0:
                raise ValueError("negative symbolic write address")
            if raw_digest is not None:
                digest = str(raw_digest)
                if not _valid_hex_digest(digest):
                    raise ValueError("symbolic write is not a stored object")
                self._get_mapping(digest, schema=_LIVE_EXPRESSION_SCHEMA)
                normalized_symbolic[address] = digest
            else:
                normalized_symbolic[address] = None
        affected = {
            address // self.page_size
            for address in (*concrete_bytes, *normalized_symbolic)
        }
        new_pages = dict(pages)
        for page_index in sorted(affected):
            page_id = pages.get(page_index)
            if page_id:
                concrete, symbolic = self._get_memory_page(page_id)
                page = bytearray(concrete)
            else:
                page = bytearray(self.page_size)
                symbolic = {}
            start = page_index * self.page_size
            for address, value in concrete_bytes.items():
                if start <= address < start + self.page_size:
                    page[address - start] = value
                    symbolic.pop(address - start, None)
            for address, digest in normalized_symbolic.items():
                if start <= address < start + self.page_size:
                    local = address - start
                    if digest is None:
                        symbolic.pop(local, None)
                    else:
                        symbolic[local] = digest
            new_pages[page_index] = self._put_memory_page(bytes(page), symbolic)
        touched_addresses = [*concrete_bytes, *normalized_symbolic]
        if touched_addresses:
            size = max(size, max(touched_addresses) + 1)
        return self._put_memory_root(size, new_pages)

    def read_memory(
        self,
        root: str,
        address: int,
        length: int,
    ) -> tuple[bytes, dict[int, str]]:
        size, pages = self._get_memory_root(root)
        address = int(address)
        length = int(length)
        if address < 0 or length < 0 or address + length > size:
            raise ValueError("memory read is outside snapshot bounds")
        concrete = bytearray(length)
        symbolic: dict[int, str] = {}
        cursor = 0
        while cursor < length:
            absolute = address + cursor
            page_index = absolute // self.page_size
            local = absolute % self.page_size
            count = min(length - cursor, self.page_size - local)
            page_id = pages.get(page_index)
            if page_id:
                page, page_symbolic = self._get_memory_page(page_id)
                concrete[cursor : cursor + count] = page[local : local + count]
                for offset, digest in page_symbolic.items():
                    if local <= offset < local + count:
                        symbolic[cursor + offset - local] = digest
            cursor += count
        return bytes(concrete), symbolic

    def memory_pages(self, root: str) -> tuple[tuple[int, str], ...]:
        _size, pages = self._get_memory_root(root)
        return tuple(sorted(pages.items()))

    def memory_diff(
        self,
        left_root: str,
        right_root: str,
    ) -> tuple[int, ...]:
        _left_size, left = self._get_memory_root(left_root)
        _right_size, right = self._get_memory_root(right_root)
        return tuple(
            sorted(
                index
                for index in set(left).union(right)
                if left.get(index) != right.get(index)
            )
        )

    def put_continuation(
        self,
        descriptor: LiveContinuationDescriptor | Mapping[str, Any],
    ) -> str:
        if not isinstance(descriptor, LiveContinuationDescriptor):
            parsed = LiveContinuationDescriptor.from_mapping(descriptor)
            if parsed is None:
                raise ValueError("invalid live continuation descriptor")
            descriptor = parsed
        roots = (
            (descriptor.path_condition_root, _LIVE_SOLVER_FRAME_SCHEMA),
            (descriptor.symbolic_store_root, _LIVE_SYMBOLIC_STORE_SCHEMA),
            (descriptor.symbolic_memory_root, _LIVE_MEMORY_ROOT_SCHEMA),
            (descriptor.program_root, _LIVE_PROGRAM_SCHEMA),
        )
        for root, schema in roots:
            if root:
                self._get_mapping(root, schema=schema)
        if descriptor.parent:
            self._get_mapping(descriptor.parent, schema=_CONTINUATION_SCHEMA)
        object_id = self._put_mapping(descriptor.to_mapping())
        if object_id != descriptor.checkpoint_id():
            raise AssertionError("continuation identity is not canonical")
        return object_id

    def restore_continuation(
        self,
        checkpoint_id: str,
    ) -> LiveContinuationBundle:
        budget = _LiveStateTraversalBudget(
            self.max_graph_objects,
            self.max_graph_bytes,
        )
        raw = self._get_mapping(
            checkpoint_id,
            schema=_CONTINUATION_SCHEMA,
            _budget=budget,
        )
        descriptor = LiveContinuationDescriptor.from_mapping(raw)
        if descriptor is None or descriptor.checkpoint_id() != checkpoint_id:
            raise ValueError("invalid continuation checkpoint identity")

        lineage = [descriptor]
        seen_checkpoints = {checkpoint_id}
        parent = descriptor.parent
        while parent:
            if parent in seen_checkpoints:
                raise ValueError("cycle in continuation parent chain")
            seen_checkpoints.add(parent)
            parent_raw = self._get_mapping(
                parent,
                schema=_CONTINUATION_SCHEMA,
                _budget=budget,
            )
            parent_descriptor = LiveContinuationDescriptor.from_mapping(parent_raw)
            if parent_descriptor is None or parent_descriptor.checkpoint_id() != parent:
                raise ValueError("invalid parent continuation identity")
            lineage.append(parent_descriptor)
            parent = parent_descriptor.parent

        for ancestor in lineage[1:]:
            self.restore_solver_frames(
                ancestor.path_condition_root,
                _budget=budget,
            )
            self.restore_symbolic_store(
                ancestor.symbolic_store_root,
                _budget=budget,
            )
            if ancestor.symbolic_memory_root:
                self._get_memory_root(
                    ancestor.symbolic_memory_root,
                    validate_pages=True,
                    _budget=budget,
                )
            if ancestor.program_root:
                self._get_mapping(
                    ancestor.program_root,
                    schema=_LIVE_PROGRAM_SCHEMA,
                    _budget=budget,
                )

        solver_frames = self.restore_solver_frames(
            descriptor.path_condition_root,
            _budget=budget,
        )
        symbolic_store = self.restore_symbolic_store(
            descriptor.symbolic_store_root,
            _budget=budget,
        )
        if descriptor.symbolic_memory_root:
            memory_size, pages = self._get_memory_root(
                descriptor.symbolic_memory_root,
                validate_pages=True,
                _budget=budget,
            )
        else:
            memory_size, pages = 0, {}
        if descriptor.program_root:
            self._get_mapping(
                descriptor.program_root,
                schema=_LIVE_PROGRAM_SCHEMA,
                _budget=budget,
            )
        return LiveContinuationBundle(
            checkpoint_id=checkpoint_id,
            descriptor=descriptor,
            solver_frames=solver_frames,
            symbolic_store=symbolic_store,
            memory_size=memory_size,
            memory_page_size=self.page_size,
            memory_pages=tuple(sorted(pages.items())),
            graph_object_count=budget.object_count,
            graph_canonical_bytes=budget.canonical_bytes,
        )


@dataclass
class StateTaskStats:
    """Bookkeeping for a continuation/state task shard."""

    task_id: str
    shard: int
    owner: int
    leases: int = 0
    completions: int = 0
    failures: int = 0
    total_generated: int = 0
    reward: float = 0.0
    elapsed: float = 0.0
    last_lease: float = 0.0
    last_complete: float = 0.0


class StateShardCoordinator:
    """Shard continuation-level work and steer workers toward owned states.

    A state task is smaller than a seed: it is the tuple of immutable input
    identity, focus slice, target branch, and optional S2F actionseed. This
    gives the MPI master a GenSym-style state-level scheduling layer without
    requiring workers to share Python object references or coordinator-local
    paths.
    """

    def __init__(
        self,
        shard_count: int = 16,
        worker_count: int = 1,
        steal_window: int = 64,
        lease_ttl: float = 300.0,
    ) -> None:
        self.shard_count = max(1, int(shard_count))
        self.worker_count = max(1, int(worker_count))
        self.steal_window = max(1, int(steal_window))
        self.lease_ttl = max(1.0, float(lease_ttl))
        self.stats: dict[str, StateTaskStats] = {}
        self.leases: dict[str, tuple[int, float]] = {}

    def shard_for_task(self, task_id: str) -> int:
        if not _valid_hex_digest(task_id):
            raise ValueError("invalid state task id")
        return int(task_id[:8], 16) % self.shard_count

    def owner_for_shard(
        self,
        shard: int,
        active_worker_count: int | None = None,
    ) -> int:
        workers = max(1, int(active_worker_count or self.worker_count))
        return shard % workers + 1

    @staticmethod
    def payload_from_item(item: Any, sha256: str = "") -> dict[str, Any]:
        if not isinstance(item, (list, tuple)) or len(item) < 1:
            raise ValueError("invalid state work item")
        path = str(item[0])
        focus = "" if len(item) < 2 or item[1] is None else str(item[1])
        try:
            target = max(0, int(item[2])) if len(item) >= 3 else 0
        except (TypeError, ValueError):
            target = 0
        digest = sha256 if _valid_hex_digest(str(sha256)) else ""
        actions = _normalize_state_actions(item[3] if len(item) > 3 else ())
        schedule_prefix: list[int] = []
        if len(item) > 4 and isinstance(item[4], (list, tuple)):
            for value in item[4]:
                try:
                    tid = int(value)
                except (TypeError, ValueError):
                    continue
                if tid >= 0:
                    schedule_prefix.append(tid)
                if len(schedule_prefix) >= 256:
                    break
        payload = {
            "input": digest or path,
            "path": path if not digest else "",
            "focus_bytes": focus,
            "target_branch": target,
            "s2f_actions": [[branch, action] for branch, action in actions],
            "schedule_prefix": schedule_prefix,
        }
        if len(item) > 5:
            continuation = LiveContinuationDescriptor.from_mapping(item[5])
            if continuation is not None:
                payload["continuation"] = continuation.to_mapping()
                payload["continuation_id"] = continuation.checkpoint_id()
        return payload

    @staticmethod
    def task_id(payload: dict[str, Any]) -> str:
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), default=str
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def describe_item(
        self,
        item: Any,
        *,
        sha256: str = "",
        active_worker_count: int | None = None,
    ) -> dict[str, Any]:
        payload = self.payload_from_item(item, sha256)
        task_id = self.task_id(payload)
        shard = self.shard_for_task(task_id)
        owner = self.owner_for_shard(shard, active_worker_count)
        stats = self.stats.get(task_id)
        if stats is None:
            stats = StateTaskStats(task_id, shard, owner)
            self.stats[task_id] = stats
        else:
            stats.shard = shard
            stats.owner = owner
        return {
            "state_task_id": task_id,
            "state_shard": shard,
            "state_owner": owner,
            "state_payload": payload,
        }

    def _priority(self, item: Any, active_worker_count: int) -> float:
        meta = self.describe_item(item, active_worker_count=active_worker_count)
        task_id = meta["state_task_id"]
        payload = meta["state_payload"]
        stats = self.stats.get(task_id)
        action_bonus = 0.04 * len(payload.get("s2f_actions", ()))
        target_bonus = 0.18 if int(payload.get("target_branch", 0) or 0) else 0.0
        if stats is None or stats.leases == 0:
            return 1.0 + target_bonus + action_bonus
        reward = stats.reward
        retry_penalty = min(0.5, 0.08 * stats.failures + 0.03 * stats.leases)
        return 1.0 + target_bonus + action_bonus + reward - retry_penalty

    def order_work_items(
        self,
        items: list[Any],
        *,
        active_worker_count: int | None = None,
    ) -> list[Any]:
        workers = max(1, int(active_worker_count or self.worker_count))
        buckets: dict[int, list[tuple[float, int, Any]]] = {
            worker: [] for worker in range(1, workers + 1)
        }
        overflow: list[tuple[float, int, Any]] = []
        for index, item in enumerate(items):
            meta = self.describe_item(item, active_worker_count=workers)
            owner = int(meta["state_owner"])
            entry = (-self._priority(item, workers), index, item)
            if owner in buckets:
                buckets[owner].append(entry)
            else:
                overflow.append(entry)
        for bucket in buckets.values():
            bucket.sort()
        overflow.sort()

        ordered: list[Any] = []
        progress = True
        while progress:
            progress = False
            for worker in range(1, workers + 1):
                bucket = buckets[worker]
                if bucket:
                    ordered.append(bucket.pop(0)[2])
                    progress = True
        ordered.extend(entry[2] for entry in overflow)
        return ordered

    def work_index(
        self,
        worker: int,
        work_queue: list[Any],
        start_index: int,
        *,
        active_worker_count: int | None = None,
    ) -> int | None:
        if start_index >= len(work_queue):
            return None
        workers = max(1, int(active_worker_count or self.worker_count))
        worker = max(1, min(int(worker), workers))
        end = min(len(work_queue), start_index + self.steal_window)
        best_owned: tuple[float, int] | None = None
        best_steal: tuple[float, int] | None = None
        for index in range(start_index, end):
            item = work_queue[index]
            meta = self.describe_item(item, active_worker_count=workers)
            score = self._priority(item, workers)
            candidate = (score, -index)
            if int(meta["state_owner"]) == worker:
                if best_owned is None or candidate > best_owned:
                    best_owned = candidate
            elif best_steal is None or candidate > best_steal:
                best_steal = candidate
        selected = best_owned if best_owned is not None else best_steal
        if selected is None:
            return start_index
        return -selected[1]

    def lease(
        self,
        task_id: str,
        worker: int,
        *,
        now: float | None = None,
    ) -> bool:
        if not _valid_hex_digest(task_id):
            return False
        if task_id in self.leases:
            return False
        now = time.time() if now is None else float(now)
        shard = self.shard_for_task(task_id)
        stats = self.stats.get(task_id)
        if stats is None:
            stats = StateTaskStats(task_id, shard, self.owner_for_shard(shard))
            self.stats[task_id] = stats
        stats.leases += 1
        stats.last_lease = now
        self.leases[task_id] = (int(worker), now)
        return True

    def complete(
        self,
        task_id: str,
        *,
        reward: float = 0.0,
        generated: int = 0,
        elapsed: float = 0.0,
        killed: bool = False,
        now: float | None = None,
        worker: int | None = None,
    ) -> bool:
        if not _valid_hex_digest(task_id):
            return False
        lease = self.leases.get(task_id)
        if worker is not None and (lease is None or lease[0] != int(worker)):
            return False
        now = time.time() if now is None else float(now)
        shard = self.shard_for_task(task_id)
        stats = self.stats.get(task_id)
        if stats is None:
            stats = StateTaskStats(task_id, shard, self.owner_for_shard(shard))
            self.stats[task_id] = stats
        stats.completions += 1
        stats.total_generated += max(0, int(generated))
        stats.elapsed += max(0.0, float(elapsed))
        stats.reward = 0.75 * stats.reward + 0.25 * max(0.0, float(reward))
        if killed or generated <= 0:
            stats.failures += 1
        stats.last_complete = now
        self.leases.pop(task_id, None)
        return True

    def abandon(self, task_id: str, worker: int | None = None) -> bool:
        """Undo a state lease that was reserved but never dispatched."""
        if not _valid_hex_digest(task_id):
            return False
        lease = self.leases.get(task_id)
        if lease is None:
            return False
        if worker is not None and lease[0] != int(worker):
            return False
        self.leases.pop(task_id, None)
        stats = self.stats.get(task_id)
        if stats is not None and stats.leases > 0:
            stats.leases -= 1
        return True

    def discard(
        self,
        task_id: str,
        worker: int | None = None,
        *,
        now: float | None = None,
    ) -> bool:
        """Retire an executed state task whose fenced result was rejected."""
        if not _valid_hex_digest(task_id):
            return False
        lease = self.leases.get(task_id)
        if lease is None:
            return False
        if worker is not None and lease[0] != int(worker):
            return False
        self.leases.pop(task_id, None)
        stats = self.stats.get(task_id)
        if stats is not None:
            stats.failures += 1
            stats.last_complete = time.time() if now is None else float(now)
        return True

    def recover_expired(self, *, now: float | None = None) -> list[str]:
        now = time.time() if now is None else float(now)
        expired = [
            task_id
            for task_id, (_worker, updated) in self.leases.items()
            if now - updated >= self.lease_ttl
        ]
        for task_id in expired:
            self.leases.pop(task_id, None)
            stats = self.stats.get(task_id)
            if stats is not None:
                stats.failures += 1
        return sorted(expired)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "shard_count": self.shard_count,
            "worker_count": self.worker_count,
            "steal_window": self.steal_window,
            "clock": "unix",
            "stats": [vars(entry) for entry in self.stats.values()],
            "leases": [
                {"task_id": task_id, "worker": worker, "updated": updated}
                for task_id, (worker, updated) in self.leases.items()
            ],
        }

    def restore(self, raw: Any) -> None:
        if not isinstance(raw, dict):
            return
        unix_clock = raw.get("clock") == "unix"
        stats = raw.get("stats", ())
        if isinstance(stats, list):
            for item in stats[-65536:]:
                if not isinstance(item, dict):
                    continue
                task_id = str(item.get("task_id", ""))
                if not _valid_hex_digest(task_id):
                    continue
                try:
                    self.stats[task_id] = StateTaskStats(
                        task_id=task_id,
                        shard=max(0, int(item.get("shard", 0))),
                        owner=max(1, int(item.get("owner", 1))),
                        leases=max(0, int(item.get("leases", 0))),
                        completions=max(0, int(item.get("completions", 0))),
                        failures=max(0, int(item.get("failures", 0))),
                        total_generated=max(0, int(item.get("total_generated", 0))),
                        reward=max(0.0, float(item.get("reward", 0.0))),
                        elapsed=max(0.0, float(item.get("elapsed", 0.0))),
                        last_lease=(
                            max(0.0, float(item.get("last_lease", 0.0)))
                            if unix_clock
                            else 0.0
                        ),
                        last_complete=(
                            max(0.0, float(item.get("last_complete", 0.0)))
                            if unix_clock
                            else 0.0
                        ),
                    )
                except (TypeError, ValueError, OverflowError):
                    continue
        leases = raw.get("leases", ())
        if isinstance(leases, list):
            for item in leases[-65536:]:
                if not isinstance(item, dict):
                    continue
                task_id = str(item.get("task_id", ""))
                if not _valid_hex_digest(task_id):
                    continue
                try:
                    self.leases[task_id] = (
                        max(1, int(item.get("worker", 1))),
                        0.0,
                    )
                except (TypeError, ValueError, OverflowError):
                    continue
