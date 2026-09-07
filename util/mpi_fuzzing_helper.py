#!/usr/bin/env python3
"""
MPI-parallel fuzzing helper for SymCC + AFL integration.

This is the MPI-parallel equivalent of symcc_fuzzing_helper. It monitors
an AFL fuzzer's queue and distributes SymCC executions across MPI workers.
New test cases that produce novel coverage are fed back to AFL.

Architecture:
    Rank 0 (Master): Monitors AFL queue, distributes inputs, triages results
    Ranks 1..N-1 (Workers): Run SymCC on assigned inputs

Usage:
    mpirun -np <N> python3 mpi_fuzzing_helper.py \
        -a <fuzzer_name> -o <afl_output_dir> -n <symcc_name> -- TARGET [ARGS...]

Requirements:
    - mpi4py  (pip install mpi4py)
    - An MPI implementation (OpenMPI, MPICH, etc.)
    - AFL (afl-showmap must be available)
    - SymCC-instrumented target binary

Example:
    # Start AFL first:
    afl-fuzz -M fuzzer01 -i seeds -o /tmp/afl_out -- ./target_afl @@

    # Then start SymCC MPI helper:
    mpirun -np 8 python3 mpi_fuzzing_helper.py \
        -a fuzzer01 -o /tmp/afl_out -n symcc -- ./target_symcc @@
"""

import argparse
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
import errno
import fcntl
import hashlib
import heapq
import json
import math
import os
import random
import shlex
import signal
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import typing

from concolic_engine import get_engine  # concolic 引擎抽象(symcc / symsan 可切换)
from coverage_queue_transaction import CoverageQueueTransactionStore
from agentic_concolic_hooks import (
    AgenticBackendManager,
    BuiltinAgenticPlanner,
    append_task,
    apply_hint,
    load_hints,
)
from structured_agentic_concolic import (
    StructuredAgenticController,
    StructuredDecision,
)
from adaptive_components import (
    AdaptiveParallelismController,
    ComponentPortfolio,
)
from afl_streaming_showmap import StreamingShowmap, parse_sparse_edge_rows
from distributed_state import (
    BitmapDeltaJournal,
    ContentAddressedInputStore,
    COVERAGE_SHARED_FILESYSTEM_REQUIREMENTS,
    CoverageOwnerShardGossip,
    FencedTargetLeaseTable,
    FencedWorkLeaseTable,
    LEASE_SHARED_FILESYSTEM_REQUIREMENTS,
    PersistentShardLedger,
    ShardedBitmapDeltaJournal,
    SharedFilesystemRequirementProfile,
    StableRegularFileIdentity,
    StateShardCoordinator,
    LiveContinuationDescriptor,
    LiveStateStore,
    WorkLeaseJournal,
    durable_unlink,
    merge_shared_filesystem_requirements,
    probe_shared_state_filesystem,
    stable_regular_file_snapshot,
)
from live_continuation import LiveContinuationExecutor
from llvm_to_continuation import lower_llvm_to_program
from executor_portfolio import ExecutorPortfolio
from hybrid_feedback import (
    AdaptiveHybridScheduler,
    SolverTelemetry,
    load_directed_distance_map,
    load_static_dependency_map,
)
from offline_policy import OfflinePolicyController
from online_value_profile import (
    OnlineValueProfileCoordinator,
    install_value_profile_update,
    value_profile_update_payload,
)
from schedule_exploration import (
    DporScheduleExplorer,
    normalize_schedule_prefix,
    prepend_ld_preload,
    write_schedule_prefix,
)
from parasuit_parameter_policy import (
    ParaSuitSelfConfiguringPolicy,
    branch_outcome_features,
)
from self_config import sanitize_parameter_overrides
from semantic_fallback import SemanticFallbackPlanner
from semantic_proposals import SemanticProposalGenerator
from smt_algorithm_scheduler import SMTAlgorithmScheduler
from string_constraints import (
    SymccJsonStringBackend,
    load_string_constraints,
    materialize_string_candidates,
    string_solver_backend_from_configuration,
)
from topseed_selector import TopSeedSelector
from verified_proposals import VerifiedProposalManager
from mpi4py import MPI

try:
    from .mpi_lifecycle import (
        TAG_READY,
        TAG_RESULT,
        TAG_STOP,
        TAG_STOP_ACK as TAG_STOP_ACK,
        _ShutdownGenerationGate as _ShutdownGenerationGate,
        _bounded_mpi_barrier,
        _bounded_mpi_timeout,
        _cooperative_shutdown_workers,
        _make_shutdown_token as _make_shutdown_token,
        _send_shutdown_ack,
        _shutdown_ack_status as _shutdown_ack_status,
        _shutdown_stop_token,
    )
except ImportError:
    from mpi_lifecycle import (
        TAG_READY,
        TAG_RESULT,
        TAG_STOP,
        TAG_STOP_ACK as TAG_STOP_ACK,
        _ShutdownGenerationGate as _ShutdownGenerationGate,
        _bounded_mpi_barrier,
        _bounded_mpi_timeout,
        _cooperative_shutdown_workers,
        _make_shutdown_token as _make_shutdown_token,
        _send_shutdown_ack,
        _shutdown_ack_status as _shutdown_ack_status,
        _shutdown_stop_token,
    )


def _command_executable_sha256(command: list[str]) -> str:
    """Hash the exact executable used to namespace cross-run profile sites."""
    if not command:
        return ""
    candidate = command[0]
    resolved = candidate if os.path.sep in candidate else shutil.which(candidate)
    if not resolved:
        return ""
    digest = hashlib.sha256()
    try:
        with open(resolved, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def _self_config_program_key(command: list[str]) -> str:
    """Bind campaign-local value history to argv and executable content."""
    return json.dumps(
        {
            "schema": "symcc-self-config-program-key-v1",
            "argv": [str(argument) for argument in command],
            "executable_sha256": _command_executable_sha256(command),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


# MPI tags
TAG_WORK = 1
# 注：bitmap 版本经 TAG_WORK 消息载荷（"bitmap_version"）随派发传播，无需独立 tag。

def _startup_env_int(name: str, default: int, lower: int, upper: int) -> int:
    """Parse import-time controls without making module import fallible."""
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


TIMEOUT_SEC = _startup_env_int(
    "SYMCC_TIMEOUT", 30, 1, 86_400
)  # SymCC execution timeout in seconds.
SHOWMAP_TIMEOUT_MS = "5000"
_WORKER_SEEN_CAP = (
    300_000  # 跨 item 内容去重集上限(约 300k×~50B≈15MB);超限清空,只损失去重机会
)
_WORKER_OBJECT_CACHE_CAP = 300_000
STATS_INTERVAL_SEC = 60
MAX_GENERATION_DEPTH = _startup_env_int(
    "SYMCC_MAX_DEPTH", 0, 0, 1_000_000
)  # 0 means unlimited.
# AFL extras hint token 文件数上限：循环复用固定文件池，避免长时间运行产生数百万小文件
# 耗尽 inode。AFL 字典体量本就有限，几千个 token 已充分。
MAX_HINT_FILES = 4096
# AflConfig._file_cache 条目上限：AFL queue 极长时防止无界内存增长。
MAX_FILE_CACHE = 200000
# 去重/跟踪容器（processed_files / _content_hashes / file_generation / grimoire_seen）
# 硬上限：超长 campaign 下这些集合随派发数无界增长。超限后渐进裁剪到低水位，
# 避免整表清空在单个时间点重新放行全部历史任务。
MAX_DEDUP_ENTRIES = 5000000
_MAX_FUZZER_STATS_BYTES = 1024 * 1024

# Per-parent hybrid worker result admission. These defaults match the standalone
# runner's aggregate limits; each result object is additionally bounded by the
# existing MPI input transport limit before it can enter a result message.
_DEFAULT_HYBRID_RESULT_MAX_OBJECTS = 4096
_DEFAULT_HYBRID_RESULT_MAX_BYTES = 256 * 1024 * 1024
_DEFAULT_HYBRID_RESULT_MAX_HINTS = 65536
_MAX_HYBRID_RESULT_OBJECTS = 1_000_000
_MAX_HYBRID_RESULT_BYTES = 1024 * 1024 * 1024 * 1024
_MAX_HYBRID_RESULT_HINTS = 1_000_000
_MAX_AFL_ARTIFACT_ID = (1 << 32) - 1
_WORKER_RESULT_AUXILIARY_SLACK = 16
_DEFAULT_TIMEOUT_SITES_MAX = 65_536
_MAX_TIMEOUT_SITES = 1_000_000
_DEFAULT_TIMEOUT_SITES_MAX_BYTES = 1024 * 1024
_MAX_TIMEOUT_SITES_MAX_BYTES = 64 * 1024 * 1024
_DEFAULT_SCHEDULE_TRACE_MAX_BYTES = 1024 * 1024
_MAX_SCHEDULE_TRACE_MAX_BYTES = 64 * 1024 * 1024
_DEFAULT_LIVE_GRAPH_MAX_OBJECTS = 262_144
_DEFAULT_LIVE_GRAPH_MAX_BYTES = 256 * 1024 * 1024
_MAX_LIVE_GRAPH_OBJECTS = 10_000_000
_COVERAGE_SNAPSHOT_MAGIC = b"SCOVv1\x00\x00"
_MAX_COVERAGE_SNAPSHOT_BYTES = 64 * 1024 * 1024


def _trim_tracking_container(
    container: set[typing.Any] | dict[typing.Any, typing.Any],
    limit: int,
    *,
    retain_ratio: float = 0.875,
) -> int:
    """Trim a tracking container incrementally and return removed entries.

    Dictionaries preserve insertion order, so their oldest records leave first.
    Sets have no recency information; removing an arbitrary bounded subset still
    avoids the much larger duplicate-analysis cliff caused by clearing all state.
    """
    limit = max(0, int(limit))
    if len(container) <= limit:
        return 0
    retain_ratio = min(0.99, max(0.5, float(retain_ratio)))
    target = 0 if limit == 0 else max(1, min(limit, int(limit * retain_ratio)))
    remove_count = len(container) - target
    if isinstance(container, dict):
        for _ in range(remove_count):
            container.pop(next(iter(container)), None)
    else:
        for _ in range(remove_count):
            container.pop()
    return remove_count


def _publish_coverage_snapshot(path: str, version: int, bitmap: bytes) -> bool:
    """Atomically publish an ephemeral monotonic worker coverage snapshot."""
    if (
        type(version) is not int
        or not 0 <= version <= (1 << 64) - 1
        or not isinstance(bitmap, bytes)
        or len(bitmap) > _MAX_COVERAGE_SNAPSHOT_BYTES
        or not path
    ):
        return False
    directory = os.path.dirname(path) or "."
    try:
        os.makedirs(directory, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=".coverage-snapshot-", suffix=".tmp", dir=directory
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                fd = -1
                stream.write(_COVERAGE_SNAPSHOT_MAGIC)
                stream.write(int(version).to_bytes(8, "little", signed=False))
                stream.write(len(bitmap).to_bytes(8, "little", signed=False))
                stream.write(bitmap)
                stream.flush()
            os.replace(temporary, path)
            temporary = ""
        finally:
            if fd >= 0:
                os.close(fd)
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
    except OSError:
        return False
    return True


def _read_coverage_snapshot(
    path: str,
    *,
    newer_than: int | None = None,
) -> tuple[int, bytes | None] | None:
    """Read a snapshot payload only when its version may advance the caller."""
    if newer_than is not None and (
        isinstance(newer_than, bool)
        or not isinstance(newer_than, int)
        or newer_than < -1
    ):
        raise ValueError("coverage snapshot version floor is invalid")
    try:
        with open(path, "rb") as stream:
            header = stream.read(24)
            if len(header) != 24 or header[:8] != _COVERAGE_SNAPSHOT_MAGIC:
                return None
            version = int.from_bytes(header[8:16], "little", signed=False)
            size = int.from_bytes(header[16:24], "little", signed=False)
            if size > _MAX_COVERAGE_SNAPSHOT_BYTES:
                return None
            if newer_than is not None and version <= newer_than:
                return version, None
            bitmap = stream.read(size + 1)
    except OSError:
        return None
    if len(bitmap) != size:
        return None
    return version, bitmap


def _refresh_worker_coverage_snapshot(
    path: str | None,
    coverage: "CoverageBitmap | None",
    version_ref: list[int] | None,
) -> bool:
    """Merge a newer shared snapshot before worker-side candidate filtering."""
    if not path or coverage is None or version_ref is None:
        return False
    snapshot = _read_coverage_snapshot(path, newer_than=version_ref[0])
    if snapshot is None:
        return False
    version, bitmap = snapshot
    if bitmap is None:
        return False
    changed = coverage.merge_delta(bitmap) > 0
    coverage.consume_delta()
    version_ref[0] = version
    return changed


def _retire_exited_query_service(
    process: subprocess.Popen | None,
    log_stream: typing.IO | None,
    dependent: typing.Any = None,
) -> tuple[subprocess.Popen | None, typing.IO | None, int | None]:
    """Retire an asynchronously exited query service exactly once."""
    if process is None:
        return None, log_stream, None
    returncode = process.poll()
    if returncode is None:
        return process, log_stream, None
    # The service owns a dedicated session.  Its leader is already gone, so no
    # coordinator remains to drain solver children; retire the orphaned group.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ValueError):
        pass
    if log_stream is not None:
        try:
            log_stream.close()
        except OSError:
            pass
        log_stream = None
    if dependent is not None:
        dependent.query_store = None
    return None, log_stream, int(returncode)


def _terminate_query_service(
    process: subprocess.Popen,
    *,
    timeout: float,
) -> None:
    """Terminate a query-service session and all solver descendants."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ValueError):
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
    try:
        process.wait(timeout=max(0.0, float(timeout)))
        return
    except (OSError, subprocess.TimeoutExpired, TypeError, ValueError):
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ValueError):
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
    try:
        process.wait(timeout=max(0.1, float(timeout)))
    except (OSError, subprocess.TimeoutExpired, TypeError, ValueError):
        pass

# AFL 覆盖率 bitmap 的初始大小（afl-showmap 自动按目标实际边数调整 map size，实测这些
# 目标为几千到几万；此为惰性分配的初值，_merge_sparse 遇到更大的 edge_id 会自动增长）。
_AFL_MAP_SIZE = 65536
_AFL_COVERAGE_BASELINE_SCHEMA = "symcc-afl-coverage-baseline-v1"

# showmap 稀疏边记录：u32 edge_id + u8 hit-count（'<' 无填充 → 每条恰 5 字节）。
# 用 Struct.iter_unpack 一次性 C 层批量解码整段，替代逐边 unpack_from 的 Python 热循环
# （实测 3.5-3.9x，此为 get_edges 每个 concolic 产出的用例都跑的最热函数）。

# 细粒度并行分解（opt-in，SYMCC_WORKER_DIVERSITY=1）：给每个 worker 一个不同的 concolic
# 策略画像 + 不相交的符号化字节区间，使相似种子在不同 worker 上产出发散（非重叠）的输入。
# 目的：突破并行 concolic 的"下游冗余"瓶颈（相同翻转→相同下游代码），让 worker 数可扩展到
# 远超 ~12 的经验饱和点——每个 worker 分到 P(区间)×S(策略) 网格中的一格不重复的工作。
# 策略轴（受 AFL ensemble 配置多样性启发，实测对 AFL 有效，此为其 concolic 侧类比）：
SYMCC_STRATEGY_PROFILES: list[dict[str, str]] = [
    {"SYMCC_EXECUTOR_CLASS": "exact"},  # 精确 Z3 单分支 executor
    {"SYMCC_EXECUTOR_CLASS": "tailored", "SYMCC_FAST_SOLVE": "1"},
    {"SYMCC_EXECUTOR_CLASS": "tailored", "SYMCC_MULTI_SOLVE": "1"},
    {"SYMCC_EXECUTOR_CLASS": "tailored", "SYMCC_MULTI_SOLVE": "2"},
    {
        "SYMCC_EXECUTOR_CLASS": "tailored",
        "SYMCC_FAST_SOLVE": "1",
        "SYMCC_MULTI_SOLVE": "1",
    },
    {"SYMCC_EXECUTOR_CLASS": "tailored", "SYMCC_OPTIMISTIC_FIRST": "1"},
    {
        "SYMCC_EXECUTOR_CLASS": "sampling",
        "SYMCC_POLY_RANGE_BYTES": "8",
        "SYMCC_POLY_LINEAR_BYTES": "6",
        "SYMCC_POLY_TEMPLATE_BYTES": "8",
        "SYMCC_POLY_TEMPLATE_PAIRS": "24",
        "SYMCC_POLY_WALK": "john",
        "SYMCC_POLY_DENSE_DIM": "16",
        "SYMCC_POLY_JOHN_STEPS": "4",
        "SYMCC_POLY_SAMPLES": "4",
        "SYMCC_POLY_CROSS_PREFIX": "1",
        "SYMCC_POLY_PROJECTED_REUSE": "1",
        "SYMCC_POLY_EXACT_PROJECTION": "1",
        "SYMCC_POLY_FIELD_RENAMING": "1",
        "SYMCC_POLY_CROSS_PREFIX_PROBES": "32",
        "SYMCC_UNSAT_CORE_CACHE": "1",
    },
]
_STRATEGY_KEYS = {key for profile in SYMCC_STRATEGY_PROFILES for key in profile}
_MAX_BRANCH_ID = (1 << 64) - 1


def _strategy_executor(strategy: int) -> str:
    if 0 <= strategy < len(SYMCC_STRATEGY_PROFILES):
        return SYMCC_STRATEGY_PROFILES[strategy].get("SYMCC_EXECUTOR_CLASS", "exact")
    return "exact"


def _configured_solver_component(value: typing.Any) -> str:
    component = str(value or "learned").strip().lower()
    return component if component in {"exact", "learned", "diverse"} else "learned"


def _normalize_branch_id(value: typing.Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        branch = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return branch if 0 < branch <= _MAX_BRANCH_ID else 0


def _normalize_s2f_actions(raw: typing.Any) -> tuple[tuple[int, str], ...]:
    if not isinstance(raw, (list, tuple)):
        return ()
    result: list[tuple[int, str]] = []
    seen: set[int] = set()
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        branch = _normalize_branch_id(item[0])
        action = str(item[1]).strip().lower()
        if branch == 0 or action not in {"solve", "sample", "skip"}:
            continue
        if branch in seen:
            continue
        seen.add(branch)
        result.append((branch, action))
    return tuple(result[:64])


def _target_group(
    target_branch: typing.Any,
    actions: typing.Any = (),
) -> tuple[int, ...]:
    """Return the ordered, unique non-skipped targets of one work item."""
    primary = _normalize_branch_id(target_branch)
    targets: list[int] = []
    if primary:
        targets.append(primary)
    for branch, action in _normalize_s2f_actions(actions):
        if action != "skip" and branch not in targets:
            targets.append(branch)
    return tuple(targets[:64])


def _enforce_target_contract(
    message: dict[str, typing.Any],
    target_branch: int,
    actions: tuple[tuple[int, str], ...],
) -> tuple[int, ...]:
    """Restore a scheduler/proposal target after heuristic hint rewriting."""
    normalized_actions = _normalize_s2f_actions(actions)
    group = _target_group(target_branch, normalized_actions)
    if not group:
        return ()
    message["target_branch"] = _normalize_branch_id(target_branch)
    if normalized_actions:
        message["s2f_actions"] = normalized_actions
    else:
        message.pop("s2f_actions", None)
    return group


def _lease_heartbeat_interval(*lease_ttls: typing.Any) -> float:
    valid: list[float] = []
    for value in lease_ttls:
        try:
            ttl = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(ttl) and ttl > 0.0:
            valid.append(ttl)
    shortest = min(valid, default=3.0)
    return max(0.1, min(30.0, shortest / 3.0))


def _heartbeat_fenced_leases(
    shared_work_leases: typing.Any,
    active_leases: typing.Mapping[int, str],
    active_lease_fences: typing.Mapping[int, str],
    shared_target_leases: typing.Any,
    active_target_leases: typing.Mapping[int, tuple[tuple[int, ...], str]],
    pending_target_leases: typing.Mapping[tuple[int, ...], str],
) -> dict[str, int]:
    """Renew independent shared leases without coupling master liveness to I/O."""
    stats = {
        "work_ok": 0,
        "work_failed": 0,
        "target_ok": 0,
        "target_failed": 0,
    }
    if shared_work_leases is not None:
        work_heartbeats = []
        for worker, fence in list(active_lease_fences.items()):
            lease_id = active_leases.get(worker, "")
            if not lease_id:
                continue
            work_heartbeats.append((lease_id, fence))
        batch_method = getattr(type(shared_work_leases), "heartbeat_many", None)
        if work_heartbeats and callable(batch_method):
            try:
                result = shared_work_leases.heartbeat_many(dict(work_heartbeats))
                renewed_ids = set(result.renewed)
            except OSError:
                renewed_ids = set()
            for lease_id, _fence in work_heartbeats:
                stats["work_ok" if lease_id in renewed_ids else "work_failed"] += 1
        else:
            for lease_id, fence in work_heartbeats:
                try:
                    renewed = shared_work_leases.heartbeat(lease_id, fence)
                except OSError:
                    renewed = False
                stats["work_ok" if renewed else "work_failed"] += 1
    if shared_target_leases is not None:
        targets_and_tokens = list(active_target_leases.values())
        targets_and_tokens.extend(pending_target_leases.items())
        for targets, token in targets_and_tokens:
            try:
                renewed = shared_target_leases.heartbeat_group(targets, token)
            except OSError:
                renewed = False
            stats["target_ok" if renewed else "target_failed"] += 1
    return stats


_DISPATCH_TOKEN_HEX_LENGTH = 64


def _normalize_dispatch_token(value: typing.Any) -> str:
    """Return a canonical transport token or reject malformed identities."""
    if not isinstance(value, str) or len(value) != _DISPATCH_TOKEN_HEX_LENGTH:
        return ""
    if any(char not in "0123456789abcdef" for char in value):
        return ""
    return value


def _make_dispatch_token(epoch: str, worker: int, sequence: int) -> str:
    """Derive an opaque identity that cannot repeat across worker generations."""
    canonical_epoch = _normalize_dispatch_token(epoch)
    worker = int(worker)
    sequence = int(sequence)
    if not canonical_epoch or worker < 1 or sequence < 1:
        raise ValueError("invalid dispatch token coordinates")
    material = (f"symcc-dispatch-v1\0{canonical_epoch}\0{worker}\0{sequence}").encode(
        "ascii"
    )
    return hashlib.sha256(material).hexdigest()


def _dispatch_result_status(
    expected_token: typing.Any,
    result: typing.Any,
) -> str:
    """Classify a result before any rank-owned state is consumed."""
    expected = _normalize_dispatch_token(expected_token)
    if not expected:
        return "unowned"
    if not isinstance(result, dict):
        return "malformed"
    reported_value = result.get("dispatch_token")
    if reported_value is None or (
        isinstance(reported_value, str) and not reported_value
    ):
        return "missing"
    reported = _normalize_dispatch_token(reported_value)
    if not reported:
        return "malformed"
    if reported != expected:
        return "stale"
    return "current"


def _ready_generation_status(
    expected_token: typing.Any,
    ready: typing.Any,
) -> str:
    """Classify READY without confusing a prior worker generation with current."""
    expected = _normalize_dispatch_token(expected_token)
    if not isinstance(ready, dict):
        return "malformed"
    reported_value = ready.get("completed_dispatch_token")
    if reported_value is None or (
        isinstance(reported_value, str) and not reported_value
    ):
        return "missing" if expected else "idle"
    reported = _normalize_dispatch_token(reported_value)
    if not reported:
        return "malformed"
    if not expected:
        return "idle"
    if reported != expected:
        return "stale"
    return "current"


def _ready_bitmap_version(value: typing.Any, current_version: int) -> int:
    """Accept only worker versions that can exist in this master epoch."""
    if isinstance(value, bool):
        return -1
    try:
        reported = int(value)
        current = max(0, int(current_version))
    except (TypeError, ValueError, OverflowError):
        return -1
    if reported < -1 or reported > current:
        return -1
    return reported


class _DispatchGenerationGate:
    """Join independently received RESULT and READY messages by generation."""

    def __init__(self) -> None:
        self.ready: dict[int, str] = {}
        self.invalid_results: dict[int, str] = {}

    def observe_ready(
        self,
        worker: int,
        expected_token: typing.Any,
        message: typing.Any,
    ) -> str:
        worker = int(worker)
        expected = _normalize_dispatch_token(expected_token)
        status = _ready_generation_status(expected, message)
        if status == "current":
            self.ready[worker] = expected
        elif status == "idle":
            self.retire(worker)
        return status

    def observe_result(
        self,
        worker: int,
        expected_token: typing.Any,
        result: typing.Any,
    ) -> str:
        worker = int(worker)
        expected = _normalize_dispatch_token(expected_token)
        status = _dispatch_result_status(expected, result)
        if status == "current":
            self.invalid_results.pop(worker, None)
        elif status in {"missing", "malformed"} and expected:
            self.invalid_results[worker] = expected
        return status

    def has_ready(self, worker: int, expected_token: typing.Any) -> bool:
        expected = _normalize_dispatch_token(expected_token)
        return bool(expected and self.ready.get(int(worker)) == expected)

    def recoverable(self, worker: int, expected_token: typing.Any) -> bool:
        expected = _normalize_dispatch_token(expected_token)
        worker = int(worker)
        return bool(
            expected
            and self.ready.get(worker) == expected
            and self.invalid_results.get(worker) == expected
        )

    def retire(self, worker: int) -> None:
        worker = int(worker)
        self.ready.pop(worker, None)
        self.invalid_results.pop(worker, None)


class _RetiredDispatchGate:
    """Park timed-out ranks until READY proves the retired generation ended."""

    def __init__(self) -> None:
        self.tokens: dict[int, str] = {}

    def park(self, worker: int, dispatch_token: typing.Any) -> None:
        token = _normalize_dispatch_token(dispatch_token)
        if not token:
            raise ValueError("invalid retired dispatch token")
        self.tokens[int(worker)] = token

    def is_parked(self, worker: int) -> bool:
        return int(worker) in self.tokens

    def observe_ready(self, worker: int, message: typing.Any) -> str:
        worker = int(worker)
        token = self.tokens.get(worker)
        if token is None:
            return "unowned"
        status = _ready_generation_status(token, message)
        if status == "current":
            self.tokens.pop(worker, None)
            return "recovered"
        return status


def _send_dispatch_result(
    comm: typing.Any,
    result: typing.Mapping[str, typing.Any],
    dispatch_token: typing.Any,
) -> str:
    """Attach the authoritative token on every worker result path."""
    token = _normalize_dispatch_token(dispatch_token)
    if not token:
        raise RuntimeError("worker received an invalid dispatch token")
    outbound = dict(result)
    outbound["dispatch_token"] = token
    comm.send(outbound, dest=0, tag=TAG_RESULT)
    return token


class _DispatchReservationTransaction:
    """Rollback heterogeneous reservations until a result is committed."""

    def __init__(self, worker: int, dispatch_token: str) -> None:
        self.worker = int(worker)
        self.dispatch_token = _normalize_dispatch_token(dispatch_token)
        if not self.dispatch_token:
            raise ValueError("invalid dispatch transaction token")
        self.dispatched = False
        self.dispatched_at: float | None = None
        self.closed = False
        self._callbacks: list[
            tuple[
                str,
                typing.Callable[[], typing.Any],
                typing.Callable[[], typing.Any] | None,
            ]
        ] = []

    def defer(
        self,
        name: str,
        before_send: typing.Callable[[], typing.Any],
        after_send: typing.Callable[[], typing.Any] | None = None,
    ) -> None:
        if self.closed:
            raise RuntimeError("dispatch transaction is already closed")
        self._callbacks.append((str(name), before_send, after_send))

    def mark_dispatched(self, *, now: float | None = None) -> None:
        if self.closed:
            raise RuntimeError("dispatch transaction is already closed")
        try:
            dispatched_at = time.monotonic() if now is None else float(now)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("dispatch timestamp must be finite") from error
        if not math.isfinite(dispatched_at) or dispatched_at < 0.0:
            raise ValueError("dispatch timestamp must be finite")
        self.dispatched = True
        self.dispatched_at = dispatched_at

    def expired(self, *, now: float, timeout: float) -> bool:
        try:
            observed_at = float(now)
            timeout_seconds = float(timeout)
        except (TypeError, ValueError, OverflowError):
            return False
        if (
            self.closed
            or not self.dispatched
            or self.dispatched_at is None
            or not math.isfinite(observed_at)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0.0
        ):
            return False
        return observed_at - self.dispatched_at >= timeout_seconds

    def commit(self) -> None:
        self._callbacks.clear()
        self.closed = True

    def rollback(self) -> dict[str, typing.Any]:
        if self.closed:
            return {"attempted": 0, "failed": 0, "failures": []}
        failures: list[str] = []
        attempted = 0
        for name, before_send, after_send in reversed(self._callbacks):
            attempted += 1
            callback = (
                after_send
                if self.dispatched and after_send is not None
                else before_send
            )
            try:
                callback()
            except Exception:
                # Rollback is a failure-isolation boundary: one broken cleanup
                # must not prevent the remaining independent resources from
                # being retired.
                failures.append(name)
        self._callbacks.clear()
        self.closed = True
        return {
            "attempted": attempted,
            "failed": len(failures),
            "failures": failures,
        }


def _rollback_dispatch_registries(
    worker: int,
    *registries: dict[int, _DispatchReservationTransaction],
) -> dict[str, typing.Any]:
    """Remove and compensate every transaction registered for one worker."""
    transactions: list[_DispatchReservationTransaction] = []
    seen: set[int] = set()
    for registry in registries:
        transaction = registry.pop(int(worker), None)
        if transaction is None or id(transaction) in seen:
            continue
        seen.add(id(transaction))
        transactions.append(transaction)
    attempted = 0
    failures: list[str] = []
    for transaction in reversed(transactions):
        result = transaction.rollback()
        attempted += int(result["attempted"])
        failures.extend(str(name) for name in result["failures"])
    return {
        "transactions": len(transactions),
        "attempted": attempted,
        "failed": len(failures),
        "failures": failures,
    }


def _rollback_owned_dispatch(
    worker: int,
    expected_token: typing.Any,
    active_dispatches: dict[int, _DispatchReservationTransaction],
    active_items: dict[int, tuple],
    *owned_registries: dict[int, typing.Any],
) -> tuple[tuple, dict[str, typing.Any]] | None:
    """Rollback one exact generation and clear its rank-owned side tables."""
    worker = int(worker)
    expected = _normalize_dispatch_token(expected_token)
    transaction = active_dispatches.get(worker)
    item = active_items.get(worker)
    if (
        not expected
        or transaction is None
        or item is None
        or transaction.dispatch_token != expected
    ):
        return None
    active_dispatches.pop(worker, None)
    active_items.pop(worker, None)
    rollback = transaction.rollback()
    for registry in owned_registries:
        registry.pop(worker, None)
    return item, rollback


def _expired_dispatches(
    active_dispatches: typing.Mapping[int, _DispatchReservationTransaction],
    *,
    now: float,
    timeout: float,
) -> tuple[tuple[int, str], ...]:
    """Return exact generations whose master-side watchdog has expired."""
    try:
        observed_at = float(now)
        timeout_seconds = float(timeout)
    except (TypeError, ValueError, OverflowError):
        return ()
    if (
        not math.isfinite(observed_at)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0.0
    ):
        return ()
    return tuple(
        (int(worker), transaction.dispatch_token)
        for worker, transaction in sorted(active_dispatches.items())
        if transaction.expired(now=observed_at, timeout=timeout_seconds)
    )


def _dispatch_watchdog_timeout(value: typing.Any, default: float) -> float:
    """Parse a finite watchdog timeout; zero explicitly disables it."""
    fallback = min(86400.0, max(1.0, float(default)))
    try:
        timeout = float(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    if not math.isfinite(timeout):
        return fallback
    return min(86400.0, max(0.0, timeout))


def _bounded_finite_float(
    value: typing.Any,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    """Parse one finite configuration value into an explicit closed range."""
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    if not math.isfinite(parsed):
        parsed = default
    return min(maximum, max(minimum, parsed))


def _afl_artifact_id(filename: str) -> int | None:
    """Parse AFL's variable-width decimal ``id:`` field."""
    if not filename.startswith("id:"):
        return None
    raw = filename[3:].partition(",")[0]
    if not raw or len(raw) > 10 or not raw.isdigit():
        return None
    value = int(raw)
    return value if value <= _MAX_AFL_ARTIFACT_ID else None


def _afl_source_id(filename: str) -> str:
    """Preserve the validated source ID, including AFL's zero padding."""
    if _afl_artifact_id(filename) is None:
        return "000000"
    return filename[3:].partition(",")[0]


def _next_afl_artifact_id(path: str) -> int:
    next_id = 0
    try:
        names = os.listdir(path)
    except OSError:
        return 0
    for name in names:
        artifact_id = _afl_artifact_id(name)
        if artifact_id is not None:
            next_id = max(next_id, artifact_id + 1)
    return next_id


def _merge_shared_filesystem_requirements(
    left: SharedFilesystemRequirementProfile,
    right: SharedFilesystemRequirementProfile,
) -> SharedFilesystemRequirementProfile:
    """Return the least contract that covers both co-located protocols."""
    left_required = set(left.required_operations)
    right_required = set(right.required_operations)
    if right_required.issubset(left_required):
        return left
    if left_required.issubset(right_required):
        return right
    return merge_shared_filesystem_requirements(
        "hybrid-combined-v1",
        left,
        right,
    )


def _shared_filesystem_preflight_contracts(
    symcc_dir: str,
    environment: typing.Mapping[str, str] | None = None,
) -> tuple[tuple[str, SharedFilesystemRequirementProfile], ...]:
    """Enumerate and merge path-specific contracts before service startup.

    The feature switches are evaluated once from the supplied environment so
    preflight and later construction cannot accidentally qualify different
    default paths. Co-located protocols are qualified against the union of their
    operations, so caching cannot let a weaker probe stand in for a stronger one.
    """
    env = os.environ if environment is None else environment
    disabled = {"0", "false", "off", "no"}
    multi_master = env.get("SYMCC_MULTI_MASTER_LEASES", "0").lower() not in disabled
    target_leases = (
        env.get("SYMCC_MULTI_MASTER_TARGET_LEASES", "1").lower() not in disabled
    )
    coverage_gossip = (
        env.get(
            "SYMCC_COVERAGE_GOSSIP",
            "1" if multi_master else "0",
        ).lower()
        not in disabled
    )

    candidates: list[tuple[str, SharedFilesystemRequirementProfile]] = []
    if multi_master:
        candidates.append(
            (
                env.get(
                    "SYMCC_MULTI_MASTER_LEASE_DIR",
                    os.path.join(symcc_dir, ".work_lease_table"),
                ),
                LEASE_SHARED_FILESYSTEM_REQUIREMENTS,
            )
        )
        if target_leases:
            candidates.append(
                (
                    env.get(
                        "SYMCC_MULTI_MASTER_TARGET_LEASE_DIR",
                        os.path.join(symcc_dir, ".target_lease_table"),
                    ),
                    LEASE_SHARED_FILESYSTEM_REQUIREMENTS,
                )
            )
    if coverage_gossip:
        candidates.append(
            (
                env.get(
                    "SYMCC_COVERAGE_OWNER_DIR",
                    os.path.join(symcc_dir, ".coverage_owner"),
                ),
                COVERAGE_SHARED_FILESYSTEM_REQUIREMENTS,
            )
        )

    distinct: dict[str, SharedFilesystemRequirementProfile] = {}
    for candidate, requirements in candidates:
        canonical = os.path.realpath(os.path.abspath(candidate))
        previous = distinct.get(canonical)
        distinct[canonical] = (
            requirements
            if previous is None
            else _merge_shared_filesystem_requirements(previous, requirements)
        )
    return tuple(distinct.items())


def _shared_filesystem_preflight_roots(
    symcc_dir: str,
    environment: typing.Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Compatibility view of the path-specific preflight contracts."""
    return tuple(
        root
        for root, _requirements in _shared_filesystem_preflight_contracts(
            symcc_dir, environment
        )
    )


def _authoritative_state_task(
    active_task: typing.Any,
    reported_task: typing.Any,
) -> tuple[str, bool]:
    """Prefer master-owned state identity and report worker disagreement."""
    active = str(active_task or "")
    reported = str(reported_task or "")
    if active:
        return active, bool(reported and reported != active)
    return reported, False


def _write_s2f_action_file(path: str, actions: tuple[tuple[int, str], ...]) -> bool:
    if not actions:
        return False
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="ascii") as stream:
            for branch, action in actions:
                stream.write(f"{branch} {action}\n")
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def _work_item_parts(
    item: tuple,
) -> tuple[
    str,
    str | None,
    int,
    tuple[tuple[int, str], ...],
    tuple[int, ...],
    dict | None,
]:
    """Decode backward-compatible work tuples.

    Existing items use ``(path, focus, target[, actions])``.  DPOR replay adds
    an optional fifth element containing the logical-thread-id schedule prefix.
    """
    input_file = str(item[0])
    item_focus = item[1] if len(item) > 1 else None
    target_branch = _normalize_branch_id(item[2] if len(item) > 2 else 0)
    s2f_actions = _normalize_s2f_actions(item[3] if len(item) > 3 else ())
    schedule_prefix = normalize_schedule_prefix(item[4] if len(item) > 4 else ())
    continuation = (
        LiveContinuationDescriptor.from_mapping(item[5]) if len(item) > 5 else None
    )
    return (
        input_file,
        item_focus,
        target_branch,
        s2f_actions,
        schedule_prefix,
        continuation.to_mapping() if continuation is not None else None,
    )


def _scheduled_work_item(path: str, prefix: tuple[int, ...]) -> tuple:
    return (path, None, 0, (), normalize_schedule_prefix(prefix))


def _work_item_from_lease_payload(payload: dict) -> tuple | None:
    path = str(payload.get("path", "") or "")
    if not path:
        return None
    focus = str(payload.get("focus_bytes", "") or "")
    target = _normalize_branch_id(payload.get("target_branch", 0))
    actions = _normalize_s2f_actions(payload.get("s2f_actions", ()))
    schedule_prefix = normalize_schedule_prefix(payload.get("schedule_prefix", ()))
    continuation = LiveContinuationDescriptor.from_mapping(payload.get("continuation"))
    if continuation is not None:
        return (
            path,
            focus or None,
            target,
            actions,
            schedule_prefix,
            continuation.to_mapping(),
        )
    if actions or schedule_prefix:
        return (path, focus or None, target, actions, schedule_prefix)
    return (path, focus or None, target)


def _recoverable_work_item_from_lease_payload(payload: dict) -> tuple | None:
    """Admit replay work with a live seed or a self-contained continuation."""
    item = _work_item_from_lease_payload(payload)
    if item is None:
        return None
    path, _focus, _target, _actions, _schedule, continuation = _work_item_parts(item)
    if os.path.isfile(path) or continuation is not None:
        return item
    return None


def _work_item_recovery_payload(item: tuple) -> dict[str, typing.Any]:
    """Encode the semantic work identity without a transport generation token."""
    path, focus, target, actions, schedule, continuation = _work_item_parts(item)
    return {
        "path": path,
        "focus_bytes": str(focus or ""),
        "target_branch": _normalize_branch_id(target),
        "s2f_actions": [[branch, action] for branch, action in actions],
        "schedule_prefix": list(schedule),
        "continuation": continuation,
    }


def _enqueue_dispatch_recovery(
    item: tuple,
    work_queue: list[tuple],
    work_index: int,
    attempts: dict[str, int],
    retry_limit: int,
    deferred: WorkLeaseJournal,
    *,
    worker: int,
) -> dict[str, typing.Any]:
    """Apply bounded retry with a fail-open in-memory durability fallback."""
    payload = _work_item_recovery_payload(item)
    recovery_id = WorkLeaseJournal.work_id(payload)
    prior_attempts = max(0, int(attempts.get(recovery_id, 0)))
    retry_limit = max(0, int(retry_limit))
    if prior_attempts < retry_limit:
        attempts[recovery_id] = prior_attempts + 1
        work_queue.insert(max(0, int(work_index)), item)
        disposition = "requeued"
    else:
        try:
            recorded = deferred.lease(
                recovery_id,
                payload,
                worker=int(worker),
            )
        except OSError:
            recorded = False
        persisted = recorded or recovery_id in deferred.leases
        if persisted:
            disposition = "deferred"
        else:
            work_queue.insert(max(0, int(work_index)), item)
            disposition = "requeued-unpersisted"
    return {
        "disposition": disposition,
        "attempt": prior_attempts + 1,
        "recovery_id": recovery_id,
    }


def _find_schedule_preload() -> str:
    explicit = os.environ.get("SYMCC_SCHEDULE_PRELOAD", "")
    if explicit:
        return explicit
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    candidates = [
        os.path.join(os.getcwd(), "libsymcc_schedule_rt.so"),
        os.path.join(repo_root, "build", "libsymcc_schedule_rt.so"),
        os.path.join(repo_root, "libsymcc_schedule_rt.so"),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return candidates[1]


def _parse_site_set(raw: str | None) -> set[int]:
    if not raw:
        return set()
    sites = set()
    for item in raw.replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            value = int(item, 0)
        except ValueError:
            continue
        if value > 0:
            sites.add(value)
    return sites


def _pin_self_to_reserved_core(rank: int) -> None:
    """按 SYMCC_CPU_LIST 把本 rank 钉到保留逻辑核（其派生的 SymCC 子进程会继承亲和性）。

    编排层（run_benchmark）在高并行度下计算与 AFL 自动绑核互斥的保留核段并经此环境变量
    传入，消除 MPI rank 在 AFL 已绑核上漂移造成的核冲突/迁移。未设置则不钉核（保持默认
    调度）。OpenMPI 启动时的绑核会被此处的 sched_setaffinity 覆盖（已验证）。"""
    spec = os.environ.get("SYMCC_CPU_LIST")
    if not spec or not hasattr(os, "sched_setaffinity"):
        return
    try:
        cores = [int(x) for x in spec.split(",") if x.strip()]
    except ValueError:
        return
    if not cores:
        return
    assigned = set(cores) if rank == 0 else {cores[rank % len(cores)]}
    try:
        os.sched_setaffinity(0, assigned)
    except OSError as error:
        print(
            f"[Rank {rank}] CPU affinity setup failed for "
            f"{sorted(assigned)}: {error}",
            file=sys.stderr,
            flush=True,
        )


def _balanced_regions(density: "list[int]", per: int) -> "list[tuple[int, int]]":
    """把 [0,len) 划分为 per 个连续字节区间，使各区间累计分支密度尽量相等（热点字节→窄
    区间隔离，冷区→宽区间）。闭区间 [(lo,hi),...]。密度全 0 时退回等宽。"""
    L = len(density)
    if per <= 1 or L == 0:
        return [(0, max(0, L - 1))]
    total = sum(density)
    if total <= 0:  # 无密度信息 → 等宽
        return [((i * L) // per, ((i + 1) * L) // per - 1) for i in range(per)]
    target = total / per
    regions: "list[tuple[int, int]]" = []
    lo = 0
    acc = 0
    for i in range(L):
        acc += density[i]
        remaining_cuts = per - 1 - len(regions)
        # 累计越过下一目标线、还需切点、且剩余字节够分给剩余区间时切一刀
        if (
            remaining_cuts > 0
            and acc >= target * (len(regions) + 1)
            and (L - 1 - i) >= remaining_cuts
        ):
            regions.append((lo, i))
            lo = i + 1
    regions.append((lo, L - 1))
    # 密度集中在尾部时贪心可能切不满 per 个区间（早期未越过目标线、末尾已无空间下刀）。
    # 补切最宽区间直到凑够 per 个，保证工作项数可预期、不让分到该种子的 worker 空闲。
    while len(regions) < per:
        widest = max(range(len(regions)), key=lambda k: regions[k][1] - regions[k][0])
        wlo, whi = regions[widest]
        if whi <= wlo:
            break  # 全为单字节区间，无法再细分
        mid = (wlo + whi) // 2
        regions[widest : widest + 1] = [(wlo, mid), (mid + 1, whi)]
    return regions


def _build_work_items(
    paths: "list[str]",
    target_count: int,
    diversity: bool,
    focus_parts: int,
    density_fn: "typing.Callable[[str], list[int] | None] | None" = None,
    target_branches: "dict[str, int] | None" = None,
    target_actions: "dict[str, tuple[tuple[int, str], ...]] | None" = None,
) -> "list[tuple]":
    """把种子路径构建为 ``(path, focus, target_branch[, actions])`` 工作项。

    动态工作窃取（自适应粒度）：默认每种子 1 个整-种子项（无冗余 CPU）。多样性模式下，
    当整-种子项数 < target_count（=活跃 worker 数）即出现空闲产能时，把种子按不相交字节
    区间细分为子项来填满空闲 worker——种子充足时不细分，避免种子够用时 P 倍重复执行的浪费。
    实测（pcre2）：等宽字节分区负载不均（符号化工作集中在少数驱动分支的字节），故仅用它
    填补"本会空闲"的产能，而非无条件细分。target 个工作项按种子近似均分（前 rem 个种子多
    分一段），仅按需细分，每种子上限 focus_parts 段。"""
    targets = target_branches or {}
    actions = target_actions or {}

    def make_item(path: str, focus: str | None, target: int) -> tuple:
        action_rows = actions.get(path, ())
        if action_rows:
            return (path, focus, target, action_rows)
        return (path, focus, target)

    base: "list[tuple]" = [make_item(p, None, targets.get(p, 0)) for p in paths]
    if not diversity or not base or len(base) >= target_count:
        return base
    n = len(base)
    # 只细分到"恰好填满空闲产能"的程度：把 target_count 个工作项尽量均匀分到 n 个种子——
    # 前 rem 个种子多分一段（k=base_k+1），其余分 base_k 段。避免此前 per=ceil(target/n)
    # 一刀切导致"种子略少于 target 时全部 2 倍细分"的浪费（seeds=23、target=24 → 46 项）。
    base_k = target_count // n
    rem = target_count % n
    items: "list[tuple]" = []
    for idx, path in enumerate(paths):
        target_branch = targets.get(path, 0)
        # 精确的 prefix target 必须保留完整符号输入。若再按字节切片，目标分支依赖的
        # 其他字节会被具体化，导致运行时无法重建对应路径约束。
        if target_branch:
            items.append(make_item(path, None, target_branch))
            continue
        k = min(focus_parts, base_k + (1 if idx < rem else 0))
        if k <= 1:  # 该种子无需细分（整段一项即可填满其份额）
            items.append(make_item(path, None, target_branch))
            continue
        try:
            flen = os.path.getsize(path)
        except OSError:
            flen = 0
        if flen <= k:  # 太短，不细分（避免空/退化区间）
            items.append(make_item(path, None, target_branch))
            continue
        # 优先按分支密度均衡划分（热点字节隔离到窄区间），无密度信息则退回等宽。
        density = density_fn(path) if density_fn is not None else None
        if density and len(density) == flen and sum(density) > 0:
            for lo, hi in _balanced_regions(density, k):
                items.append(make_item(path, f"{lo}-{hi}", target_branch))
        else:
            for i in range(k):
                lo = (i * flen) // k
                hi = ((i + 1) * flen) // k - 1  # 闭区间，减 1 使相邻不重叠
                items.append(make_item(path, f"{lo}-{max(lo, hi)}", target_branch))
    return items


def _comparison_taint_offsets(
    telemetry: SolverTelemetry | None,
    *,
    max_span: int = 64,
) -> list[int]:
    if telemetry is None:
        return []
    offsets: list[int] = []
    for (
        _site,
        _branch,
        _count,
        lo,
        hi,
        _taken,
        interesting,
    ) in telemetry.comparison_taints:
        if not interesting or hi < lo:
            continue
        span = hi - lo + 1
        if span <= max_span:
            offsets.extend(range(lo, hi + 1))
        else:
            offsets.extend((lo, hi))
    return offsets


def _write_compact_focus_set(
    path: str,
    offsets: list[int],
    *,
    max_entries: int = 512,
) -> bool:
    unique = sorted(
        dict.fromkeys(offset for offset in offsets[-max_entries:] if offset >= 0)
    )
    if not unique:
        return False
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as stream:
            for offset in unique:
                stream.write(f"{offset}\n")
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


class AflConfig:
    """AFL fuzzer configuration, read from fuzzer_stats."""

    def __init__(
        self,
        fuzzer_output_dir: str,
        max_input_bytes: int | None = None,
    ) -> None:
        self.queue = os.path.join(fuzzer_output_dir, "queue")
        if max_input_bytes is None:
            self.max_input_bytes = _bounded_env_int(
                os.environ,
                "SYMCC_MAX_TRANSPORT_INPUT",
                16 * 1024 * 1024,
                1,
                _MAX_HYBRID_RESULT_BYTES,
            )
        elif (
            isinstance(max_input_bytes, bool)
            or not isinstance(max_input_bytes, int)
            or max_input_bytes < 1
            or max_input_bytes > _MAX_HYBRID_RESULT_BYTES
        ):
            raise ValueError("invalid AFL input admission limit")
        else:
            self.max_input_bytes = max_input_bytes
        # 每文件静态属性缓存：以路径和稳定 inode 身份共同索引；同名替换会重新计算。
        # 这消除未变 queue 项在每轮重复读文件/哈希的开销，同时不依赖 AFL 永不替换
        # 路径这一较弱假设。
        self._file_cache: dict[str, dict] = {}
        stats_path = os.path.join(fuzzer_output_dir, "fuzzer_stats")

        with open(stats_path) as f:
            stats = f.read(_MAX_FUZZER_STATS_BYTES + 1)
        if len(stats) > _MAX_FUZZER_STATS_BYTES:
            raise RuntimeError("fuzzer_stats exceeds the 1 MiB parser limit")

        # Parse the command line from fuzzer_stats
        parts: list[str] | None = None
        for line in stats.splitlines():
            field, separator, value = line.partition(":")
            if field.strip() == "command_line" and separator:
                cmd_str = value.strip()
                try:
                    parts = shlex.split(cmd_str)
                except ValueError as error:
                    raise RuntimeError(
                        "Could not parse command_line in fuzzer_stats"
                    ) from error
                break
        if parts is None:
            raise RuntimeError("Could not find command_line in fuzzer_stats")
        if not parts:
            raise RuntimeError("command_line in fuzzer_stats is empty")

        # 查找 afl-showmap：先从 afl-fuzz 同目录找，再从 PATH 找
        afl_binary = parts[0]
        afl_dir = os.path.dirname(afl_binary)
        if afl_dir:
            candidate = os.path.join(afl_dir, "afl-showmap")
            if os.path.isfile(candidate):
                self.show_map = candidate
            else:
                self.show_map = shutil.which("afl-showmap") or "afl-showmap"
        else:
            # afl-fuzz 是通过 PATH 调用的，afl-showmap 也应该在 PATH 中
            self.show_map = shutil.which("afl-showmap") or "afl-showmap"

        # Extract target command (after --)
        try:
            dash_idx = parts.index("--")
            self.target_command = parts[dash_idx + 1 :]  # skip '--'
        except ValueError:
            self.target_command = parts[-1:]
        if not self.target_command:
            raise RuntimeError("command_line in fuzzer_stats has no target")

        self.use_stdin = not any("@@" in argument for argument in self.target_command)
        self.use_qemu = "-Q" in parts

    def best_new_testcases(
        self,
        seen: set[str],
        batch_size: int | None = None,
        analyzed_hashes: set[str] | None = None,
        edge_yield: dict[str, float] | None = None,
        frontier_fn: "typing.Callable[[str], float] | None" = None,
        score_fn: "typing.Callable[[str, dict, float, float], float] | None" = None,
    ) -> list[str]:
        """
        Return a list of unseen test cases from the AFL queue, scored by priority.

        增强种子调度策略（受 CoFuzz ICSE'23 启发）：
        1. 边产出率加权：历史上 concolic 分析后产出新覆盖的种子类型优先
        2. +cov 标记：AFL 认为发现新覆盖 → 高优先
        3. 稀有边覆盖：触达稀有边的种子优先（AFL 文件名中的 +rare）
        4. 文件大小效率：根据 size/yield 比率动态调整
        5. 新颖度衰减：越新的种子优先，但对极新种子不再过度加分
        """
        if not os.path.isdir(self.queue):
            return []

        cache = self._file_cache
        # 硬上限：dict 保持插入序，超限时按 FIFO 逐出最旧条目——它们多为最早发现、
        # 早已派发（在 seen 中）的 queue 文件。偶尔逐出未处理条目仅导致其下轮被重新
        # stat/哈希，代价有界（O(超出量)/次，非每次 O(cache) 重建列表）且不影响正确性。
        while len(cache) > MAX_FILE_CACHE:
            cache.pop(next(iter(cache)))
        new_candidates: list[tuple[float, str, str, str]] = []
        top_candidates: list[tuple[float, str, str, str]] = []
        scan_budget_sec = _bounded_env_float(
            os.environ,
            "SYMCC_QUEUE_SCAN_BUDGET_SEC",
            1.0,
            0.0,
            60.0,
        )
        scan_work_elapsed = 0.0
        scan_limit = _bounded_env_int(
            os.environ,
            "SYMCC_QUEUE_SCAN_MAX",
            8192,
            1,
            MAX_DEDUP_ENTRIES,
        )
        scanned_entries = 0
        try:
            for entry in os.scandir(self.queue):
                fpath = entry.path
                if fpath in seen:
                    continue

                # Both bounds are budgets for unseen admission work.  Applying
                # the wall-clock deadline before this check made a long prefix
                # of immutable, already-dispatched queue names consume the
                # complete budget and starve every newly appended AFL seed.
                if scan_budget_sec > 0.0 and scan_work_elapsed >= scan_budget_sec:
                    break
                if scanned_entries >= scan_limit:
                    break
                scanned_entries += 1
                admission_started = time.monotonic()

                # 每文件静态属性只计算一次（AFL queue 文件不可变）
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    continue
                identity = StableRegularFileIdentity.from_stat(metadata)
                attrs = cache.get(fpath)
                if attrs is None or attrs.get("identity") != identity:
                    try:
                        snapshot = stable_regular_file_snapshot(
                            fpath,
                            max_bytes=self.max_input_bytes,
                        )
                    except (OSError, ValueError):
                        continue
                    if snapshot.identity != identity:
                        continue
                    name = entry.name
                    fsize = identity.size
                    afl_id = _afl_artifact_id(name) or 0
                    # 大小/新颖度得分（与 edge_yield 无关，可预计算并缓存）
                    static_score = 0.0
                    if "+cov" in name:
                        static_score += 100.0
                    if "+rare" in name:
                        static_score += 60.0
                    if "symcc_" in name:
                        static_score += 20.0
                    if fsize > 50 * 1024:
                        static_score -= 40.0
                    elif fsize > 10240:
                        static_score -= min(30.0, (fsize - 10240) / 1024.0)
                    elif fsize < 256:
                        static_score += 10.0
                    static_score += min(50.0, math.log1p(afl_id) * 5.0)
                    seed_type = (
                        "cov"
                        if "+cov" in name
                        else ("symcc" if "symcc_" in name else "normal")
                    )
                    # 内容摘要与稳定inode身份一起缓存；同名replacement会重新准入。
                    attrs = {
                        "name": name,
                        "size": fsize,
                        "static": static_score,
                        "type": seed_type,
                        "hash": snapshot.sha256,
                        "identity": snapshot.identity,
                    }
                    cache[fpath] = attrs
                    while len(cache) > MAX_FILE_CACHE:
                        cache.pop(next(iter(cache)))

                # 内容去重（用缓存哈希，不再重复读文件）
                if analyzed_hashes is not None and attrs["hash"] in analyzed_hashes:
                    continue

                # 动态部分：edge_yield 每轮变化 → 廉价的算术叠加
                type_yield = (
                    edge_yield.get(attrs["type"], 0.5)
                    if edge_yield is not None
                    else 0.5
                )
                frontier = frontier_fn(fpath) if frontier_fn is not None else 0.0
                if score_fn is not None:
                    score = score_fn(fpath, attrs, type_yield, frontier)
                else:
                    score = attrs["static"] + type_yield * 30.0
                # K-Scheduler 风格前沿加权（opt-in，SYMCC_KSCHED=1）：优先覆盖当前稀有边
                # （≈ 覆盖前沿/CFG 中心性）的种子，把 concolic 预算投向最可能触达未探索区域处。
                if score_fn is None:
                    score += frontier * 40.0

                candidate = (score, attrs["name"], fpath, attrs["hash"])
                if batch_size is None:
                    new_candidates.append(candidate)
                elif batch_size > 0:
                    if len(top_candidates) < batch_size:
                        heapq.heappush(top_candidates, candidate)
                    elif candidate > top_candidates[0]:
                        heapq.heapreplace(top_candidates, candidate)
                scan_work_elapsed += time.monotonic() - admission_started
        except OSError:
            return []

        # Master只消费K个候选，扫描期间就维持固定容量min-heap，使候选选择空间
        # 从O(U)降为O(K)；tuple tie-break也消除scandir枚举顺序对同分项的影响。
        if batch_size is not None:
            top = sorted(top_candidates, reverse=True)
        else:
            top = sorted(new_candidates, key=lambda c: -c[0])
        # 扫描期间 FIFO 逐出可能碰到一个较早枚举但最终进入 Top-K 的高分候选。
        # 当 K 可由缓存完整容纳时，将选中项重新插到末端，保证 dispatch 仍能把
        # 随后的稳定快照与评分时摘要比较。先保存完整 attrs，再统一移除/追加，避免
        # 逐项追加时再次逐出另一个选中项。
        if batch_size is not None and len(top) <= MAX_FILE_CACHE:
            selected_records = [
                (
                    candidate[2],
                    cache.get(candidate[2], {"hash": candidate[3]}),
                )
                for candidate in top
            ]
            for path, _attrs in selected_records:
                cache.pop(path, None)
            for path, selected_attrs in selected_records:
                cache[path] = selected_attrs
            while len(cache) > MAX_FILE_CACHE:
                cache.pop(next(iter(cache)))
        return [c[2] for c in top]

    def run_showmap(
        self, testcase: str, bitmap_path: str
    ) -> "tuple[str, bytes | list[tuple[int, int]] | None]":
        """
        Run afl-showmap on a test case.

        Returns:
            ("success", bitmap_data_or_sparse_edges) | ("hang", None) |
            ("crash", None)
        """
        showmap_timeout_ms = _bounded_env_int(
            os.environ,
            "SYMCC_AFL_SHOWMAP_TIMEOUT_MS",
            int(SHOWMAP_TIMEOUT_MS),
            50,
            60_000,
        )
        if self.use_stdin:
            edges = corpus_showmap_edges(
                self.show_map,
                self.target_command,
                [testcase],
                os.path.dirname(bitmap_path) or ".",
                timeout_ms=showmap_timeout_ms,
                use_qemu=self.use_qemu,
            )
            if edges is not None:
                return "success", edges
            return "error", None

        cmd = [self.show_map]
        if self.use_qemu:
            cmd.append("-Q")
        cmd.extend(
            ["-t", str(showmap_timeout_ms), "-m", "none", "-b", "-o", bitmap_path]
        )

        # Build target command with @@ replaced
        for arg in self.target_command:
            cmd.append(arg.replace("@@", str(testcase)))

        try:
            run_timeout = max(1.0, showmap_timeout_ms / 1000.0 + 2.0)
            proc = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=run_timeout,
            )

            if proc.returncode == 0:
                with open(bitmap_path, "rb") as f:
                    bitmap_data = f.read()
                return "success", bitmap_data
            elif proc.returncode == 1:
                return "hang", None
            elif proc.returncode == 2:
                return "crash", None
            else:
                return "error", None
        except subprocess.TimeoutExpired:
            return "hang", None
        except FileNotFoundError:
            print(
                f"[Master] afl-showmap not found at: {self.show_map}", file=sys.stderr
            )
            return "error", None
        except (OSError, subprocess.SubprocessError) as e:
            print(f"[Master] afl-showmap error: {e}", file=sys.stderr)
            return "error", None


_COVERAGE_BITMAP_CHUNK_BYTES = 16 * 1024


def _count_bitmap_bits(data: bytes | bytearray | memoryview) -> int:
    view = memoryview(data)
    total = 0
    for offset in range(0, len(view), _COVERAGE_BITMAP_CHUNK_BYTES):
        chunk = view[offset : offset + _COVERAGE_BITMAP_CHUNK_BYTES]
        total += int.from_bytes(chunk, "little").bit_count()
    return total


def _count_bitmap_delta_bits(
    old_data: bytes | bytearray | memoryview,
    new_data: bytes | bytearray | memoryview,
    *,
    overlap: int,
) -> int:
    old_view = memoryview(old_data)
    new_view = memoryview(new_data)
    delta = 0
    for offset in range(0, overlap, _COVERAGE_BITMAP_CHUNK_BYTES):
        end = min(overlap, offset + _COVERAGE_BITMAP_CHUNK_BYTES)
        old_int = int.from_bytes(old_view[offset:end], "little")
        new_int = int.from_bytes(new_view[offset:end], "little")
        delta += (new_int & ~old_int).bit_count()
    if len(new_view) > overlap:
        delta += _count_bitmap_bits(new_view[overlap:])
    return delta


class CoverageBitmap:
    """Track AFL bitmap buckets with sparse and chunked dense merge paths."""

    def __init__(self) -> None:
        self.data: bytearray | None = None
        self.edges: set[int] = set()
        # AFL 的一个 bitmap byte 可包含多个 hit-count bucket。边数用于展示，
        # feature_count 用于给在线调度器提供精确的增量奖励。
        self.feature_count = 0
        self._pending_delta: dict[int, int] = {}

    def consume_delta(self) -> list[tuple[int, int]]:
        """Return and clear bitmap bits accumulated since the previous call."""
        delta = sorted(self._pending_delta.items())
        self._pending_delta.clear()
        return delta

    def init_from_afl(
        self,
        afl_config: AflConfig,
        queue_dir: str,
        max_entries: int | None = None,
        time_budget: float = 5.0,
    ) -> "AflCoverageIngestResult":
        """Replay an AFL queue into this bitmap.

        The default is deliberately complete.  ``max_entries`` remains only
        for explicit diagnostic sampling; production master startup uses
        :class:`AflCoverageBridge`, which also tracks incremental queue growth.
        ``time_budget`` is retained for source compatibility but no longer
        permits a silently partial authoritative baseline.
        """
        del time_budget
        if not os.path.isdir(queue_dir):
            return AflCoverageIngestResult()
        paths = [
            os.path.join(queue_dir, name)
            for name in sorted(os.listdir(queue_dir))
            if os.path.isfile(os.path.join(queue_dir, name))
        ]
        if max_entries is not None:
            entry_limit = max(0, int(max_entries))
            paths = paths[-entry_limit:] if entry_limit else []
        bridge = AflCoverageBridge(
            afl_config,
            self,
            os.path.join(os.path.dirname(queue_dir), ".init_bitmap"),
        )
        try:
            return bridge.ingest(paths)
        finally:
            bridge.close()

    def merge(self, new_data: "bytes | list[tuple[int, int]]") -> bool:
        """Merge new bitmap data. Returns True if new coverage found.

        接受 bytes (完整 bitmap) 或 list[(index, value)] (稀疏边列表)。
        稀疏格式只需检查非零边，O(~500) 而非 O(8M)。
        """
        return self.merge_delta(new_data) > 0

    def count_delta(self, new_data: "bytes | list[tuple[int, int]]") -> int:
        """Return the number of unseen bitmap bits without mutating state."""
        if isinstance(new_data, list):
            delta = 0
            pending: dict[int, int] = {}
            for edge_id, hit in new_data:
                if edge_id < 0:
                    continue
                old = pending.get(edge_id)
                if old is None:
                    old = (
                        self.data[edge_id]
                        if self.data is not None and edge_id < len(self.data)
                        else 0
                    )
                bits = hit & 0xFF
                delta += (bits & ~old).bit_count()
                pending[edge_id] = old | bits
            return delta

        if self.data is None:
            return _count_bitmap_bits(new_data)
        overlap = min(len(self.data), len(new_data))
        delta = _count_bitmap_delta_bits(
            self.data,
            new_data,
            overlap=overlap,
        )
        return delta

    def merge_delta(self, new_data: "bytes | list[tuple[int, int]]") -> int:
        """Merge coverage and return the number of newly observed bitmap bits."""
        # 支持稀疏格式: [(edge_id, hit_count), ...]
        if isinstance(new_data, list):
            return self._merge_sparse_delta(new_data)

        if self.data is None:
            self.data = bytearray(new_data)
            for i, b in enumerate(new_data):
                if b:
                    self.edges.add(i)
                    self._pending_delta[i] = b
            delta = _count_bitmap_bits(new_data)
            self.feature_count = delta
            return delta

        if len(new_data) > len(self.data):
            self.data.extend(b"\x00" * (len(new_data) - len(self.data)))

        new_view = memoryview(new_data)
        overlap = len(new_view)
        delta = 0
        for offset in range(0, overlap, _COVERAGE_BITMAP_CHUNK_BYTES):
            end = min(overlap, offset + _COVERAGE_BITMAP_CHUNK_BYTES)
            old_int = int.from_bytes(self.data[offset:end], "little")
            new_int = int.from_bytes(new_view[offset:end], "little")
            diff = new_int & ~old_int
            if not diff:
                continue
            delta += diff.bit_count()
            merged = old_int | new_int
            self.data[offset:end] = merged.to_bytes(end - offset, "little")
            # 仅将新增边加入集合：从位差 diff 中提取置位所在字节索引。
            while diff:
                lsb = diff & -diff
                bit = lsb.bit_length() - 1
                index = offset + bit // 8
                self.edges.add(index)
                self._pending_delta[index] = self._pending_delta.get(index, 0) | (
                    1 << (bit % 8)
                )
                diff &= diff - 1
        if delta:
            self.feature_count += delta
        return delta

    def _merge_sparse(self, edges: list) -> bool:
        """合并稀疏边列表 [(edge_id, hit_count), ...]。极快。

        始终维护稠密 data（惰性分配 + 按需增长），data[edge_id] 累积各边的命中桶。
        这样 (1) master 能把全局 bitmap 写入共享文件供 worker 播种本地 dedup；(2) 已知
        边上的新命中桶（AFL 视为新覆盖）也能被判为 interesting。此前 data 为 None 时只按
        边"存在"去重，既漏掉新桶覆盖，又使共享 bitmap 永不写出（worker 无法播种全局边）。
        """
        return self._merge_sparse_delta(edges) > 0

    def _merge_sparse_delta(self, edges: list) -> int:
        if self.data is None:
            self.data = bytearray(_AFL_MAP_SIZE)
        delta = 0
        for edge_id, hit in edges:
            if edge_id < 0:
                continue
            hit &= 0xFF
            if hit == 0:
                continue
            if edge_id >= len(self.data):  # 目标 map 大于初值 → 增长以容纳
                self.data.extend(b"\x00" * (edge_id + 1 - len(self.data)))
            old = self.data[edge_id]
            new_bits = hit & ~old
            delta += new_bits.bit_count()
            if new_bits:
                self._pending_delta[edge_id] = (
                    self._pending_delta.get(edge_id, 0) | new_bits
                )
            # 用 data 判"是否见过"（byte==0 即未覆盖）而非 edges 集：worker 从共享 bitmap
            # 播种时只需批量拷贝 data，免去每次版本更新 O(map) 的 edges 集重建（showmap 桶
            # 恒 >=1，故 data==0 严格等价于未覆盖）。edges 仍维护，供 master 报告边数。
            if old == 0:
                self.edges.add(edge_id)
                self.data[edge_id] = hit
            elif old | hit != old:
                self.data[edge_id] = old | hit
        self.feature_count += delta
        return delta


@dataclass(frozen=True)
class AflCoverageIngestResult:
    """Observable outcome of one AFL-to-SymCC coverage synchronization."""

    examined: int = 0
    ingested: int = 0
    failed: int = 0
    globally_new_features: int = 0
    local_bitmap_changed: bool = False

    @property
    def complete(self) -> bool:
        return self.failed == 0


class AflCoverageBridge:
    """Maintain the master bitmap as the AFL and SymCC coverage union.

    The bridge remembers the stable identity of every successfully replayed AFL
    queue entry.  Startup performs a complete queue replay; later polls only
    replay newly admitted or replaced entries.  Failed entries remain eligible
    for retry instead of silently becoming part of the authoritative baseline.
    """

    def __init__(
        self,
        afl_config: AflConfig,
        coverage: CoverageBitmap,
        bitmap_path: str,
        claim_callback: "typing.Callable[[bytes | list[tuple[int, int]]], tuple[int, bool]] | None" = None,
    ) -> None:
        self.afl_config = afl_config
        self.coverage = coverage
        self.bitmap_path = bitmap_path
        self.claim_callback = claim_callback
        self._index_path = bitmap_path + ".afl-coverage-index.sqlite3"
        os.makedirs(os.path.dirname(self._index_path) or ".", exist_ok=True)
        self._index = sqlite3.connect(self._index_path, timeout=30.0)
        self._index.execute("PRAGMA journal_mode=WAL")
        self._index.execute("PRAGMA synchronous=FULL")
        self._index.execute(
            "CREATE TABLE IF NOT EXISTS ingested("
            "path TEXT PRIMARY KEY, device INTEGER NOT NULL, "
            "inode INTEGER NOT NULL, size INTEGER NOT NULL, "
            "mtime_ns INTEGER NOT NULL, ctime_ns INTEGER NOT NULL)"
        )
        self._index.execute(
            "CREATE TABLE IF NOT EXISTS coverage_baseline("
            "singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
            "schema TEXT NOT NULL, bitmap BLOB NOT NULL, "
            "bitmap_sha256 TEXT NOT NULL)"
        )
        self._index.execute(
            "CREATE TABLE IF NOT EXISTS coverage_retry("
            "retry_id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "path TEXT NOT NULL UNIQUE, attempts INTEGER NOT NULL, "
            "next_retry_ns INTEGER NOT NULL)"
        )
        self._index.execute(
            "CREATE INDEX IF NOT EXISTS coverage_retry_due "
            "ON coverage_retry(next_retry_ns,retry_id)"
        )
        self._index.commit()
        self._afl_baseline = CoverageBitmap()
        self.restored_baseline_features = 0
        self.invalidated_indexes = 0
        self._restore_baseline()
        self._tracked_entries = int(
            self._index.execute("SELECT COUNT(*) FROM ingested").fetchone()[0]
        )
        self._retry_entries = int(
            self._index.execute(
                "SELECT COUNT(*) FROM coverage_retry"
            ).fetchone()[0]
        )
        self._fair_retry_turn = False
        self._async_jobs = _bounded_env_int(
            os.environ, "SYMCC_AFL_COVERAGE_JOBS", 1, 1, 16)
        self._async_capacity = _bounded_env_int(
            os.environ, "SYMCC_AFL_COVERAGE_PENDING", 256, 1, 65_536)
        self._retry_capacity = _bounded_env_int(
            os.environ, "SYMCC_AFL_COVERAGE_RETRIES", 65_536, 1, 1_000_000)
        self._retry_base_delay = _bounded_env_float(
            os.environ,
            "SYMCC_AFL_COVERAGE_RETRY_BASE_SEC",
            0.1,
            0.0,
            3600.0,
        )
        self._retry_max_delay = _bounded_env_float(
            os.environ,
            "SYMCC_AFL_COVERAGE_RETRY_MAX_SEC",
            30.0,
            self._retry_base_delay,
            86_400.0,
        )
        self._executor = ThreadPoolExecutor(
            max_workers=self._async_jobs,
            thread_name_prefix="afl-coverage",
        )
        self._inflight: dict[
            str,
            tuple[
                StableRegularFileIdentity,
                Future[tuple[str, bytes | list[tuple[int, int]] | None]],
            ],
        ] = {}
        self.async_submitted = 0
        self.async_capacity_skips = 0
        self._queue_generation: tuple[int, int, int, int] | None = None
        self._unchanged_queue_polls = 0
        self._queue_audit_interval = _bounded_env_int(
            os.environ,
            "SYMCC_AFL_COVERAGE_RESCAN_POLLS",
            120,
            1,
            1_000_000,
        )
        self.sync_calls = 0
        self.queue_scans = 0
        self.queue_scan_skips = 0
        self.entries_ingested = 0
        self.entries_failed = 0
        self.globally_new_features = 0

    @property
    def compute_slots(self) -> int:
        """Return the configured concurrent showmap worker count."""
        return self._async_jobs

    def _restore_baseline(self) -> None:
        """Restore the AFL bitmap or invalidate identities that lack evidence."""
        row = self._index.execute(
            "SELECT schema,bitmap,bitmap_sha256 FROM coverage_baseline "
            "WHERE singleton=1"
        ).fetchone()
        tracked = int(
            self._index.execute("SELECT COUNT(*) FROM ingested").fetchone()[0]
        )
        bitmap: bytes | None = None
        if row is not None:
            raw_schema, raw_bitmap, raw_digest = row
            candidate = bytes(raw_bitmap)
            if (
                raw_schema == _AFL_COVERAGE_BASELINE_SCHEMA
                and 0 < len(candidate) <= StreamingShowmap.MAX_MAP_SIZE
                and isinstance(raw_digest, str)
                and hashlib.sha256(candidate).hexdigest() == raw_digest
            ):
                bitmap = candidate
        if row is not None and bitmap is None or tracked and bitmap is None:
            # This is a recomputable cache.  An index without its exact bitmap
            # cannot justify skipping queue replay after restart.
            self._index.execute("BEGIN IMMEDIATE")
            self._index.execute("DELETE FROM ingested")
            self._index.execute("DELETE FROM coverage_baseline")
            self._index.commit()
            self.invalidated_indexes += 1
            return
        if bitmap is not None:
            self._afl_baseline.merge_delta(bitmap)
            self.restored_baseline_features = self._afl_baseline.feature_count
            self._claim(bitmap)

    def _record_baseline(
        self, bitmap_data: bytes | list[tuple[int, int]]
    ) -> None:
        self._afl_baseline.merge_delta(bitmap_data)

    def _commit_index(self) -> None:
        bitmap = bytes(self._afl_baseline.data or b"")
        if bitmap:
            if len(bitmap) > StreamingShowmap.MAX_MAP_SIZE:
                self._index.rollback()
                raise ValueError("AFL coverage baseline exceeds the map-size budget")
            self._index.execute(
                "INSERT INTO coverage_baseline("
                "singleton,schema,bitmap,bitmap_sha256) VALUES(1,?,?,?) "
                "ON CONFLICT(singleton) DO UPDATE SET "
                "schema=excluded.schema,bitmap=excluded.bitmap,"
                "bitmap_sha256=excluded.bitmap_sha256",
                (
                    _AFL_COVERAGE_BASELINE_SCHEMA,
                    sqlite3.Binary(bitmap),
                    hashlib.sha256(bitmap).hexdigest(),
                ),
            )
        self._index.commit()

    @staticmethod
    def _regular_identity(path: str) -> StableRegularFileIdentity | None:
        try:
            metadata = os.stat(path, follow_symlinks=False)
        except OSError:
            return None
        if not stat.S_ISREG(metadata.st_mode):
            return None
        return StableRegularFileIdentity.from_stat(metadata)

    def _claim(
        self, bitmap_data: bytes | list[tuple[int, int]]
    ) -> tuple[int, bool]:
        if self.claim_callback is not None:
            return self.claim_callback(bitmap_data)
        delta = self.coverage.merge_delta(bitmap_data)
        return delta, delta > 0

    @staticmethod
    def _identity_tuple(identity: StableRegularFileIdentity) -> tuple[int, ...]:
        return (
            identity.device,
            identity.inode,
            identity.size,
            identity.mtime_ns,
            identity.ctime_ns,
        )

    def _stored_identity(self, path: str) -> StableRegularFileIdentity | None:
        row = self._index.execute(
            "SELECT device,inode,size,mtime_ns,ctime_ns FROM ingested "
            "WHERE path=?",
            (path,),
        ).fetchone()
        return StableRegularFileIdentity(*map(int, row)) if row is not None else None

    def _mark_ingested(
        self, path: str, identity: StableRegularFileIdentity
    ) -> None:
        if self._stored_identity(path) is None:
            self._tracked_entries += 1
        self._index.execute(
            "INSERT INTO ingested(path,device,inode,size,mtime_ns,ctime_ns) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
            "device=excluded.device,inode=excluded.inode,size=excluded.size,"
            "mtime_ns=excluded.mtime_ns,ctime_ns=excluded.ctime_ns",
            (path, *self._identity_tuple(identity)),
        )

    def _note_retry(self, path: str) -> None:
        row = self._index.execute(
            "SELECT attempts FROM coverage_retry WHERE path=?",
            (path,),
        ).fetchone()
        attempts = int(row[0]) + 1 if row is not None else 1
        exponent = min(attempts - 1, 30)
        delay = min(
            self._retry_max_delay,
            self._retry_base_delay * (2 ** exponent),
        )
        next_retry_ns = time.time_ns() + int(delay * 1_000_000_000)
        self._index.execute(
            "INSERT INTO coverage_retry(path,attempts,next_retry_ns) "
            "VALUES(?,?,?) ON CONFLICT(path) DO UPDATE SET "
            "attempts=excluded.attempts,next_retry_ns=excluded.next_retry_ns",
            (path, attempts, next_retry_ns),
        )
        if row is None:
            self._retry_entries += 1

    def _discard_retry(self, path: str) -> bool:
        deleted = self._index.execute(
            "DELETE FROM coverage_retry WHERE path=?", (path,)
        ).rowcount
        if deleted:
            self._retry_entries -= 1
        return bool(deleted)

    def _retry_candidates(
        self,
        *,
        respect_backoff: bool,
        limit: int | None = None,
    ) -> tuple[str, ...]:
        bounded_limit = self._retry_capacity if limit is None else max(0, limit)
        if bounded_limit == 0:
            return ()
        if respect_backoff:
            rows = self._index.execute(
                "SELECT path FROM coverage_retry WHERE next_retry_ns<=? "
                "ORDER BY next_retry_ns,retry_id LIMIT ?",
                (time.time_ns(), bounded_limit),
            ).fetchall()
        else:
            rows = self._index.execute(
                "SELECT path FROM coverage_retry ORDER BY retry_id LIMIT ?",
                (bounded_limit,),
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def _scan_queue_paths(self) -> tuple[list[str], bool]:
        try:
            before_stat = os.stat(self.afl_config.queue, follow_symlinks=False)
            if not stat.S_ISDIR(before_stat.st_mode):
                return [], True
            before = (
                before_stat.st_dev,
                before_stat.st_ino,
                before_stat.st_mtime_ns,
                before_stat.st_ctime_ns,
            )
        except OSError:
            return [], True
        if before == self._queue_generation:
            self._unchanged_queue_polls += 1
            if self._unchanged_queue_polls < self._queue_audit_interval:
                self.queue_scan_skips += 1
                return [], False
        self._unchanged_queue_polls = 0
        try:
            paths = sorted(
                entry.path
                for entry in os.scandir(self.afl_config.queue)
                if entry.is_file(follow_symlinks=False)
            )
        except OSError:
            return [], True
        self.queue_scans += 1
        try:
            after_stat = os.stat(self.afl_config.queue, follow_symlinks=False)
            after = (
                after_stat.st_dev,
                after_stat.st_ino,
                after_stat.st_mtime_ns,
                after_stat.st_ctime_ns,
            )
        except OSError:
            after = None
        # A concurrent append forces one more scan.  Queue entries already
        # observed in this pass remain identity-deduplicated by ingest().
        self._queue_generation = after if before == after else None
        return paths, False

    def ingest_queue(self) -> AflCoverageIngestResult:
        paths, failed = self._scan_queue_paths()
        if failed:
            return AflCoverageIngestResult(failed=1)
        return self.ingest(paths)

    def schedule_queue(self, limit: int | None = None) -> int:
        paths, failed = self._scan_queue_paths()
        if failed:
            self.entries_failed += 1
            return 0
        return self.schedule(paths, limit=limit, skip_retries=True)

    def retry_failed(self) -> AflCoverageIngestResult:
        return self.ingest(
            self._retry_candidates(respect_backoff=False)
        )

    def schedule_failed(self, limit: int | None = None) -> int:
        # Retry rows remain in the durable ledger while their showmap task is
        # in flight.  Read past those rows so they cannot hide later due work
        # at the front of a bounded SQL result.
        requested = (
            self._retry_capacity
            if limit is None
            else min(self._retry_capacity, max(0, limit))
        )
        candidate_limit = requested + len(self._inflight)
        candidates = (
            path
            for path in self._retry_candidates(
                respect_backoff=True,
                limit=candidate_limit,
            )
            if path not in self._inflight
        )
        return self.schedule(
            candidates,
            limit=requested,
        )

    def schedule_fair(self) -> int:
        """Fill async slots while reserving progress for both work classes."""
        available = max(0, self._async_capacity - len(self._inflight))
        if available == 0:
            return 0

        if available == 1:
            if self._fair_retry_turn:
                submitted = self.schedule_failed(limit=1)
                if not submitted:
                    submitted = self.schedule_queue(limit=1)
            else:
                submitted = self.schedule_queue(limit=1)
                if not submitted:
                    submitted = self.schedule_failed(limit=1)
            if submitted:
                self._fair_retry_turn = not self._fair_retry_turn
            return submitted

        # Larger pools reserve at least one quarter for retries without
        # wasting capacity when none are due.
        retry_reserve = min(available, max(1, (available + 3) // 4))
        queue_budget = available if available == 1 else available - retry_reserve
        submitted = self.schedule_queue(limit=queue_budget)
        remaining = max(0, available - submitted)
        retried = self.schedule_failed(limit=min(retry_reserve, remaining))
        submitted += retried
        remaining = max(0, available - submitted)
        if remaining:
            submitted += self.schedule_queue(limit=remaining)
        remaining = max(0, available - submitted)
        if remaining:
            submitted += self.schedule_failed(limit=remaining)
        return submitted

    def is_synchronized(self, path: str | os.PathLike[str]) -> bool:
        """Return whether this exact queue object is in the AFL baseline."""
        normalized = os.fspath(path)
        identity = self._regular_identity(normalized)
        return identity is not None and self._stored_identity(normalized) == identity

    def _run_showmap_async(
        self,
        path: str,
    ) -> tuple[str, bytes | list[tuple[int, int]] | None]:
        bitmap_path = (
            f"{self.bitmap_path}.async.{os.getpid()}."
            f"{time.monotonic_ns()}"
        )
        try:
            return self.afl_config.run_showmap(path, bitmap_path)
        finally:
            try:
                os.unlink(bitmap_path)
            except OSError:
                pass

    def schedule(
        self,
        paths: typing.Iterable[str],
        *,
        limit: int | None = None,
        skip_retries: bool = False,
    ) -> int:
        """Schedule bounded showmap work without mutating coverage state."""
        submitted = 0
        retry_state_changed = False
        for raw_path in paths:
            if limit is not None and submitted >= max(0, limit):
                self._queue_generation = None
                break
            if len(self._inflight) >= self._async_capacity:
                self.async_capacity_skips += 1
                # The current directory generation is not fully consumed.
                # Force the next queue poll to rescan immediately instead of
                # hiding the unscheduled suffix until the periodic audit.
                self._queue_generation = None
                break
            path = os.fspath(raw_path)
            retry = self._index.execute(
                "SELECT 1 FROM coverage_retry WHERE path=?", (path,)
            ).fetchone()
            if skip_retries and retry is not None:
                continue
            identity = self._regular_identity(path)
            if identity is None:
                self._note_retry(path)
                retry_state_changed = True
                continue
            if self._stored_identity(path) == identity:
                retry_state_changed = (
                    self._discard_retry(path) or retry_state_changed
                )
                continue
            if path in self._inflight:
                continue
            future = self._executor.submit(self._run_showmap_async, path)
            self._inflight[path] = (identity, future)
            submitted += 1
        self.async_submitted += submitted
        if retry_state_changed:
            self._commit_index()
        return submitted

    def poll(self) -> AflCoverageIngestResult:
        """Commit completed background observations on the master thread."""
        completed = [
            path
            for path, (_identity, future) in self._inflight.items()
            if future.done()
        ]
        if not completed:
            return AflCoverageIngestResult()
        self.sync_calls += 1
        ingested = 0
        failed = 0
        globally_new = 0
        local_changed = False
        for path in completed:
            identity, future = self._inflight.pop(path)
            try:
                result_type, bitmap_data = future.result()
            except Exception:
                result_type, bitmap_data = "error", None
            if (
                result_type != "success"
                or bitmap_data is None
                or self._regular_identity(path) != identity
            ):
                self._note_retry(path)
                failed += 1
                continue
            new_count, changed = self._claim(bitmap_data)
            globally_new += new_count
            local_changed = local_changed or changed
            self._record_baseline(bitmap_data)
            self._mark_ingested(path, identity)
            self._discard_retry(path)
            ingested += 1
        self._commit_index()
        self.entries_ingested += ingested
        self.entries_failed += failed
        self.globally_new_features += globally_new
        return AflCoverageIngestResult(
            examined=len(completed),
            ingested=ingested,
            failed=failed,
            globally_new_features=globally_new,
            local_bitmap_changed=local_changed,
        )

    def ingest(self, paths: typing.Iterable[str]) -> AflCoverageIngestResult:
        pending: list[tuple[str, StableRegularFileIdentity]] = []
        preflight_failed = 0
        retry_state_changed = False
        for raw_path in paths:
            path = os.fspath(raw_path)
            identity = self._regular_identity(path)
            if identity is None:
                self._note_retry(path)
                preflight_failed += 1
                retry_state_changed = True
                continue
            if self._stored_identity(path) == identity:
                retry_state_changed = (
                    self._discard_retry(path) or retry_state_changed
                )
                continue
            pending.append((path, identity))
        if not pending:
            if retry_state_changed:
                self._commit_index()
            self.entries_failed += preflight_failed
            return AflCoverageIngestResult(
                examined=preflight_failed,
                failed=preflight_failed,
            )

        examined = len(pending) + preflight_failed
        self.sync_calls += 1
        ingested = 0
        failed = preflight_failed
        globally_new = 0
        local_changed = False
        timeout_ms = _bounded_env_int(
            os.environ,
            "SYMCC_AFL_SHOWMAP_TIMEOUT_MS",
            int(SHOWMAP_TIMEOUT_MS),
            50,
            60_000,
        )

        # stdin targets can replay a corpus in one forkserver session.  Strict
        # staging prevents an I/O failure from being mistaken for a complete
        # baseline; filename targets retain status-preserving per-input replay.
        if self.afl_config.use_stdin:
            bitmap_data = corpus_showmap_edges(
                self.afl_config.show_map,
                self.afl_config.target_command,
                [path for path, _identity in pending],
                os.path.dirname(self.afl_config.queue),
                timeout_ms=timeout_ms,
                use_qemu=self.afl_config.use_qemu,
                require_all=True,
            )
            if bitmap_data is not None:
                new_count, changed = self._claim(bitmap_data)
                globally_new += new_count
                local_changed = local_changed or changed
                self._record_baseline(bitmap_data)
                for path, identity in pending:
                    if self._regular_identity(path) == identity:
                        self._mark_ingested(path, identity)
                        self._discard_retry(path)
                        ingested += 1
                    else:
                        self._note_retry(path)
                        failed += 1
                pending = []

        # Corpus mode may be unavailable or one staging operation may fail.
        # Retry each remaining entry so one transient file does not discard the
        # complete startup baseline.
        if pending:
            for path, identity in pending:
                result_type, bitmap_data = self.afl_config.run_showmap(
                    path, self.bitmap_path
                )
                if (
                    result_type != "success"
                    or bitmap_data is None
                    or self._regular_identity(path) != identity
                ):
                    self._note_retry(path)
                    failed += 1
                    continue
                new_count, changed = self._claim(bitmap_data)
                globally_new += new_count
                local_changed = local_changed or changed
                self._record_baseline(bitmap_data)
                self._mark_ingested(path, identity)
                self._discard_retry(path)
                ingested += 1

        self.entries_ingested += ingested
        self.entries_failed += failed
        self.globally_new_features += globally_new
        self._commit_index()
        return AflCoverageIngestResult(
            examined=examined,
            ingested=ingested,
            failed=failed,
            globally_new_features=globally_new,
            local_bitmap_changed=local_changed,
        )

    def snapshot(self) -> dict[str, int]:
        return {
            "sync_calls": self.sync_calls,
            "queue_scans": self.queue_scans,
            "queue_scan_skips": self.queue_scan_skips,
            "queue_audit_interval": self._queue_audit_interval,
            "tracked_entries": self._tracked_entries,
            "retry_entries": self._retry_entries,
            "retry_batch_limit": self._retry_capacity,
            "async_jobs": self._async_jobs,
            "async_pending": len(self._inflight),
            "async_capacity": self._async_capacity,
            "async_submitted": self.async_submitted,
            "async_capacity_skips": self.async_capacity_skips,
            "entries_ingested": self.entries_ingested,
            "entries_failed": self.entries_failed,
            "globally_new_features": self.globally_new_features,
            "restored_baseline_features": self.restored_baseline_features,
            "invalidated_indexes": self.invalidated_indexes,
        }

    def close(self) -> AflCoverageIngestResult:
        self._executor.shutdown(wait=True, cancel_futures=False)
        result = self.poll()
        self._index.close()
        return result


def _claim_coverage_transaction(
    coverage: CoverageBitmap,
    coverage_gossip: typing.Any,
    bitmap_data: bytes | list[tuple[int, int]],
) -> tuple[int, bool]:
    """Claim globally, then converge the caller's local coverage immediately."""
    globally_new = coverage_gossip.claim(bitmap_data)
    # A successful claim call establishes that every candidate bit is now in
    # the global shard state, whether this coordinator added it or lost a race.
    # Merge it locally at once so the pre-check cannot republish duplicates
    # until the next periodic gossip pull.
    locally_new = coverage.merge_delta(bitmap_data)
    committed = coverage_gossip.pull_recent_claims()
    if committed:
        locally_new += coverage.merge_delta(committed)
    return globally_new, locally_new > 0


def _claim_coverage_transactions(
    coverage: CoverageBitmap,
    coverage_gossip: typing.Any,
    bitmap_batch: list[bytes | list[tuple[int, int]]],
) -> tuple[list[int], bool]:
    """Batch global claims while preserving candidate-order attribution."""
    globally_new = coverage_gossip.claim_many(bitmap_batch)
    locally_changed = False
    for bitmap_data in bitmap_batch:
        locally_changed = coverage.merge_delta(bitmap_data) > 0 or locally_changed
    committed = coverage_gossip.pull_recent_claims()
    if committed:
        locally_changed = coverage.merge_delta(committed) > 0 or locally_changed
    return globally_new, locally_changed


def _preview_coverage_delta(
    coverage: CoverageBitmap,
    bitmap_data: bytes | list[tuple[int, int]],
    pending: dict[int, int],
) -> int:
    """Count and stage novelty against coverage plus earlier batch candidates."""
    return sum(
        bits.bit_count()
        for _index, bits in _stage_coverage_delta_rows(
            coverage, bitmap_data, pending
        )
    )


def _stage_coverage_delta_rows(
    coverage: CoverageBitmap,
    bitmap_data: bytes | list[tuple[int, int]],
    pending: dict[int, int],
) -> list[tuple[int, int]]:
    """Stage exact new bits without mutating the authoritative bitmap."""
    rows: typing.Iterable[tuple[int, int]]
    rows = enumerate(bitmap_data) if isinstance(bitmap_data, bytes) else bitmap_data
    delta_rows: dict[int, int] = {}
    for raw_index, raw_bits in rows:
        index = int(raw_index)
        bits = int(raw_bits) & 0xFF
        if index < 0 or not bits:
            continue
        if index in pending:
            old = pending[index]
        elif coverage.data is not None and index < len(coverage.data):
            old = coverage.data[index]
        else:
            old = 0
        new_bits = bits & ~old
        if new_bits:
            pending[index] = old | bits
            delta_rows[index] = delta_rows.get(index, 0) | new_bits
    return sorted(delta_rows.items())


class Stats:
    """Execution statistics."""

    def __init__(self) -> None:
        self.total_count = 0
        self.total_time = 0.0
        self.failed_count = 0
        self.failed_time = 0.0
        self.generated_count = 0
        self.interesting_count = 0
        self.quarantined_results = 0
        self.quarantined_result_reasons: dict[str, int] = {}
        self.quarantined_ready_messages = 0
        self.quarantined_ready_reasons: dict[str, int] = {}
        self.requeued_dispatches = 0
        self.deferred_dispatches = 0
        self.watchdog_timeouts = 0
        self.watchdog_worker_recoveries = 0
        self.generated_crashes = 0
        self.generated_hangs = 0

    def quarantine_result(self, reason: str) -> None:
        reason = str(reason or "unknown")
        self.quarantined_results += 1
        self.quarantined_result_reasons[reason] = (
            self.quarantined_result_reasons.get(reason, 0) + 1
        )

    def quarantine_ready(self, reason: str) -> None:
        reason = str(reason or "unknown")
        self.quarantined_ready_messages += 1
        self.quarantined_ready_reasons[reason] = (
            self.quarantined_ready_reasons.get(reason, 0) + 1
        )

    def add_execution(self, elapsed: float, killed: bool) -> None:
        if killed:
            self.failed_count += 1
            self.failed_time += elapsed
        else:
            self.total_count += 1
            self.total_time += elapsed

    def log(self, f: "typing.TextIO") -> None:
        f.write(f"Successful executions: {self.total_count}\n")
        f.write(f"Time in successful executions: {self.total_time * 1000:.0f}ms\n")
        if self.total_count > 0:
            avg = self.total_time / self.total_count * 1000
            f.write(f"Avg time per successful execution: {avg:.0f}ms\n")
        f.write(f"Failed executions: {self.failed_count}\n")
        f.write(f"Time in failed executions: {self.failed_time * 1000:.0f}ms\n")
        if self.failed_count > 0:
            avg = self.failed_time / self.failed_count * 1000
            f.write(f"Avg time per failed execution: {avg:.0f}ms\n")
        f.write(f"Total test cases generated: {self.generated_count}\n")
        f.write(f"Interesting test cases: {self.interesting_count}\n")
        reasons = ",".join(
            f"{reason}={count}"
            for reason, count in sorted(self.quarantined_result_reasons.items())
        )
        f.write(
            f"Quarantined worker results: {self.quarantined_results}"
            f"{f' ({reasons})' if reasons else ''}\n"
        )
        ready_reasons = ",".join(
            f"{reason}={count}"
            for reason, count in sorted(self.quarantined_ready_reasons.items())
        )
        f.write(
            f"Quarantined worker ready messages: "
            f"{self.quarantined_ready_messages}"
            f"{f' ({ready_reasons})' if ready_reasons else ''}\n"
        )
        f.write(f"Protocol-requeued dispatches: {self.requeued_dispatches}\n")
        f.write(f"Protocol-deferred dispatches: {self.deferred_dispatches}\n")
        f.write(f"Dispatch watchdog timeouts: {self.watchdog_timeouts}\n")
        f.write(
            f"Dispatch watchdog worker recoveries: {self.watchdog_worker_recoveries}\n"
        )
        f.write(f"Generated terminal crashes: {self.generated_crashes}\n")
        f.write(f"Generated terminal hangs: {self.generated_hangs}\n")
        f.write("-" * 80 + "\n")
        f.flush()


_STRING_SOLVER_BACKENDS: dict[tuple[str, str], typing.Any] = {}


def _env_enabled(env: dict[str, str], key: str, default: str = "0") -> bool:
    return str(env.get(key, default)).lower() not in {"0", "false", "off", "no"}


def _bounded_env_int(
    env: dict[str, str],
    key: str,
    default: int,
    lower: int,
    upper: int,
) -> int:
    try:
        value = int(env.get(key, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


def _bounded_env_float(
    env: dict[str, str],
    key: str,
    default: float,
    lower: float,
    upper: float,
) -> float:
    try:
        value = float(env.get(key, str(default)))
    except (TypeError, ValueError):
        value = default
    if not math.isfinite(value):
        value = default
    return max(lower, min(upper, value))


def _live_state_graph_limits(env: dict[str, str]) -> dict[str, int]:
    return {
        "max_graph_objects": _bounded_env_int(
            env,
            "SYMCC_LIVE_GRAPH_MAX_OBJECTS",
            _DEFAULT_LIVE_GRAPH_MAX_OBJECTS,
            1,
            _MAX_LIVE_GRAPH_OBJECTS,
        ),
        "max_graph_bytes": _bounded_env_int(
            env,
            "SYMCC_LIVE_GRAPH_MAX_BYTES",
            _DEFAULT_LIVE_GRAPH_MAX_BYTES,
            1,
            _MAX_HYBRID_RESULT_BYTES,
        ),
    }


def _returncode_indicates_timeout(retcode: int) -> bool:
    return retcode in (124, 137, -signal.SIGKILL)


_CRASH_SIGNALS = frozenset(
    int(signum)
    for signum in (
        getattr(signal, "SIGABRT", None),
        getattr(signal, "SIGBUS", None),
        getattr(signal, "SIGFPE", None),
        getattr(signal, "SIGILL", None),
        getattr(signal, "SIGSEGV", None),
        getattr(signal, "SIGTRAP", None),
    )
    if signum is not None
)


def _returncode_indicates_crash(retcode: int, killed: bool = False) -> bool:
    if killed or retcode in (0, -1, 124, 137, -signal.SIGKILL):
        return False
    if retcode < 0:
        return -retcode in _CRASH_SIGNALS
    return retcode > 128 and retcode - 128 in _CRASH_SIGNALS


@dataclass(frozen=True)
class _WorkerFileIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class _WorkerOutputCandidate:
    path: str
    name: str
    identity: _WorkerFileIdentity


class _WorkerResultBudgetExceeded(ValueError):
    """Fail-closed result admission with enough context for worker telemetry."""

    def __init__(
        self,
        resource: str,
        observed: int,
        limit: int,
        *,
        objects: int,
    ) -> None:
        super().__init__(
            f"hybrid worker result {resource} budget exceeded: "
            f"observed={observed}, limit={limit}"
        )
        self.resource = resource
        self.observed = observed
        self.limit = limit
        self.objects = objects
        self.retcode = -1
        self.elapsed = 0.0
        self.killed = False
        self.post_elapsed = 0.0

    def bind_execution(
        self,
        *,
        retcode: int,
        elapsed: float,
        killed: bool,
        post_elapsed: float,
    ) -> None:
        self.retcode = retcode
        self.elapsed = elapsed
        self.killed = killed
        self.post_elapsed = post_elapsed

    def payload(self) -> dict[str, int | str]:
        return {
            "resource": self.resource,
            "observed": self.observed,
            "limit": self.limit,
            "objects": self.objects,
        }


def _stage_hybrid_result_objects(
    candidates: list[dict[str, typing.Any]],
    object_store: ContentAddressedInputStore | None,
) -> int:
    """Replace candidate bytes with durable CAS references when available."""
    if object_store is None:
        return 0
    staged = 0
    for candidate in candidates:
        content = candidate.get("content")
        if not isinstance(content, bytes):
            continue
        try:
            object_id, _path = object_store.put(content)
        except (OSError, ValueError):
            # Preserve the already coverage-admitted candidate in the legacy
            # payload if shared storage is transiently unavailable.
            continue
        candidate.pop("content", None)
        candidate["object_id"] = object_id
        candidate["object_size"] = len(content)
        staged += 1
    return staged


def _hybrid_candidate_content(
    candidate: typing.Mapping[str, typing.Any],
    object_store: ContentAddressedInputStore | None,
    *,
    max_bytes: int,
) -> bytes:
    """Read and reverify one admitted result object at its point of use."""
    content = candidate.get("content")
    if isinstance(content, bytes):
        if len(content) > max_bytes:
            raise ValueError("hybrid result content exceeds its object budget")
        return content
    object_id = candidate.get("object_id")
    object_size = candidate.get("object_size")
    if (
        object_store is None
        or not isinstance(object_id, str)
        or isinstance(object_size, bool)
        or not isinstance(object_size, int)
        or object_size < 0
        or object_size > max_bytes
    ):
        raise ValueError("hybrid result object reference is unavailable")
    snapshot = object_store.snapshot(object_id, retain_content=True)
    if (
        snapshot.content is None
        or snapshot.sha256 != object_id
        or snapshot.identity.size != object_size
    ):
        raise ValueError("hybrid result object changed after admission")
    return snapshot.content


def _is_sha256_text(value: typing.Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _hybrid_result_object_ids(result: typing.Any) -> set[str]:
    if not isinstance(result, dict):
        return set()
    object_ids: set[str] = set()
    proposal_object_id = result.get("proposal_object_id")
    if _is_sha256_text(proposal_object_id):
        object_ids.add(proposal_object_id)
    for candidate in result.get("new_tests", ()):
        if not isinstance(candidate, dict):
            continue
        object_id = candidate.get("object_id")
        if _is_sha256_text(object_id):
            object_ids.add(object_id)
    return object_ids


def _collect_hybrid_result_objects(
    object_store: ContentAddressedInputStore,
    *,
    protected_object_ids: set[str],
    max_entries: int,
    min_age_seconds: float,
) -> dict[str, int | bool]:
    inventory = object_store.scan_objects(max_entries=max_entries)
    now_ns = time.time_ns()
    min_age_ns = int(max(0.0, min_age_seconds) * 1_000_000_000)
    deleted = 0
    failed = 0
    skipped_protected = 0
    skipped_young = 0
    for observation in inventory.objects:
        object_id = observation.object_id
        if object_id in protected_object_ids:
            skipped_protected += 1
            continue
        if now_ns - observation.identity.mtime_ns < min_age_ns:
            skipped_young += 1
            continue
        try:
            durable_unlink(object_store.object_path(object_id))
        except FileNotFoundError:
            continue
        except OSError:
            failed += 1
        else:
            deleted += 1
    return {
        "scanned": inventory.scanned_entries,
        "deleted": deleted,
        "failed": failed,
        "skipped_protected": skipped_protected,
        "skipped_young": skipped_young,
        "complete": inventory.complete,
    }


def _validate_hybrid_worker_result(
    result: typing.Any,
    *,
    max_objects: int,
    max_bytes: int,
    max_object_bytes: int,
    max_hints: int,
    max_timeout_sites: int = _DEFAULT_TIMEOUT_SITES_MAX,
    max_schedule_trace_bytes: int = _DEFAULT_SCHEDULE_TRACE_MAX_BYTES,
    result_object_store: ContentAddressedInputStore | None = None,
) -> dict[str, typing.Any]:
    """Validate one current-generation worker payload before consuming state."""
    limits = (
        max_objects,
        max_bytes,
        max_object_bytes,
        max_hints,
        max_timeout_sites,
        max_schedule_trace_bytes,
    )
    if any(
        isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
        for limit in limits
    ):
        raise ValueError("hybrid result admission limits must be positive integers")
    if not isinstance(result, dict):
        raise ValueError("hybrid worker result must be an object")
    if len(result) > 64 or any(
        not isinstance(key, str) or not key or len(key) > 64 for key in result
    ):
        raise ValueError("hybrid worker result has invalid protocol fields")
    new_tests = result.get("new_tests", ())
    if not isinstance(new_tests, (list, tuple)) or len(new_tests) > max_objects:
        raise ValueError("hybrid worker result has an invalid object count")

    def validate_bitmap(value: typing.Any) -> None:
        if value is None:
            return
        if (
            not isinstance(value, list)
            or len(value) > StreamingShowmap.MAX_EDGES
        ):
            raise ValueError("hybrid worker result has an invalid coverage map")
        seen: set[int] = set()
        for row in value:
            if not isinstance(row, (list, tuple)) or len(row) != 2:
                raise ValueError("hybrid worker result has a malformed coverage row")
            edge, hit = row
            if (
                isinstance(edge, bool)
                or not isinstance(edge, int)
                or edge < 0
                or edge >= StreamingShowmap.MAX_MAP_SIZE
                or edge in seen
                or isinstance(hit, bool)
                or not isinstance(hit, int)
                or hit < 1
                or hit > 255
            ):
                raise ValueError("hybrid worker result has an invalid coverage row")
            seen.add(edge)

    logical_bytes = 0
    coverage_rows = 0
    hint_count = 0
    for candidate in new_tests:
        if not isinstance(candidate, dict):
            raise ValueError("hybrid worker candidate must be an object")
        if not set(candidate).issubset(
            {
                "content",
                "object_id",
                "object_size",
                "bitmap",
                "hints",
                "terminal_status",
                "terminal_detail",
            }
        ):
            raise ValueError("hybrid worker candidate has unknown fields")
        content = candidate.get("content")
        object_id = candidate.get("object_id")
        object_size = candidate.get("object_size")
        if content is not None:
            if (
                not isinstance(content, bytes)
                or len(content) > max_object_bytes
                or object_id is not None
                or object_size is not None
            ):
                raise ValueError("hybrid worker candidate has invalid content")
            candidate_size = len(content)
        else:
            if (
                not isinstance(object_id, str)
                or len(object_id) != 64
                or any(char not in "0123456789abcdef" for char in object_id)
                or isinstance(object_size, bool)
                or not isinstance(object_size, int)
                or object_size < 0
                or object_size > max_object_bytes
            ):
                raise ValueError("hybrid worker candidate has invalid object reference")
            candidate_size = object_size
            if result_object_store is not None:
                try:
                    snapshot = result_object_store.snapshot(object_id)
                except (OSError, ValueError) as error:
                    raise ValueError(
                        "hybrid worker candidate object is unavailable"
                    ) from error
                if (
                    snapshot.sha256 != object_id
                    or snapshot.identity.size != object_size
                ):
                    raise ValueError(
                        "hybrid worker candidate object failed verification"
                    )
        logical_bytes += candidate_size
        if logical_bytes > max_bytes:
            raise ValueError("hybrid worker result exceeds its byte budget")
        bitmap = candidate.get("bitmap")
        validate_bitmap(bitmap)
        if bitmap is not None:
            coverage_rows += len(bitmap)
            if coverage_rows > StreamingShowmap.MAX_EDGES:
                raise ValueError(
                    "hybrid worker result exceeds its aggregate coverage budget"
                )
        terminal_status = candidate.get("terminal_status")
        terminal_detail = candidate.get("terminal_detail")
        if terminal_status is not None:
            if (
                terminal_status not in {"crash", "timeout"}
                or candidate.get("bitmap") is not None
                or isinstance(terminal_detail, bool)
                or not isinstance(terminal_detail, int)
                or not 0 <= terminal_detail <= 255
            ):
                raise ValueError("hybrid worker candidate has invalid terminal status")
        elif terminal_detail is not None:
            raise ValueError("hybrid worker candidate has incomplete terminal status")
        hints = candidate.get("hints")
        if hints is None:
            continue
        if not isinstance(hints, (list, tuple)):
            raise ValueError("hybrid worker candidate hints must be a list")
        hint_count += len(hints)
        if hint_count > max_hints:
            raise ValueError("hybrid worker result exceeds its hint budget")
        for hint in hints:
            if not isinstance(hint, (list, tuple)) or len(hint) != 3:
                raise ValueError("hybrid worker result has a malformed hint")
            offset, old, new = hint
            if (
                isinstance(offset, bool)
                or not isinstance(offset, int)
                or offset < 0
                or offset > (1 << 32) - 1
                or isinstance(old, bool)
                or not isinstance(old, int)
                or old < 0
                or old > 255
                or isinstance(new, bool)
                or not isinstance(new, int)
                or new < 0
                or new > 255
            ):
                raise ValueError("hybrid worker result has an invalid hint")

    budget_error = result.get("result_budget_error")
    if budget_error is not None:
        if (
            not isinstance(budget_error, dict)
            or set(budget_error) != {"resource", "observed", "limit", "objects"}
            or budget_error.get("resource")
            not in {
                "directory_entries",
                "hint_files",
                "objects",
                "object_bytes",
                "bytes",
                "hints",
                "coverage_rows",
            }
            or any(
                isinstance(budget_error.get(name), bool)
                or not isinstance(budget_error.get(name), int)
                or budget_error[name] < 0
                for name in ("observed", "limit", "objects")
            )
            or budget_error["limit"] < 1
            or budget_error["observed"] <= budget_error["limit"]
            or budget_error["objects"] > max_objects + 1
        ):
            raise ValueError("hybrid worker result has invalid budget telemetry")

    total_generated = result.get("total_generated", len(new_tests))
    generated_limit = max_objects + int(
        bool(budget_error and budget_error["resource"] == "objects")
    )
    if (
        isinstance(total_generated, bool)
        or not isinstance(total_generated, int)
        or total_generated < len(new_tests)
        or total_generated > generated_limit
        or (budget_error is not None and total_generated != budget_error["objects"])
    ):
        raise ValueError("hybrid worker result has invalid generation telemetry")
    retcode = result.get("retcode", 0)
    if (
        isinstance(retcode, bool)
        or not isinstance(retcode, int)
        or retcode < -(1 << 31)
        or retcode > (1 << 31) - 1
    ):
        raise ValueError("hybrid worker result has an invalid return code")
    elapsed = result.get("elapsed", 0.0)
    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)):
        raise ValueError("hybrid worker result has invalid elapsed time")
    elapsed = float(elapsed)
    if not math.isfinite(elapsed) or elapsed < 0.0 or elapsed > 7 * 24 * 3600:
        raise ValueError("hybrid worker result has invalid elapsed time")
    if not isinstance(result.get("killed", False), bool):
        raise ValueError("hybrid worker result has an invalid killed flag")
    proposal_bitmap = result.get("proposal_bitmap")
    validate_bitmap(proposal_bitmap)
    if proposal_bitmap is not None:
        coverage_rows += len(proposal_bitmap)
        if coverage_rows > StreamingShowmap.MAX_EDGES:
            raise ValueError(
                "hybrid worker result exceeds its aggregate coverage budget"
            )
    proposal_content = result.get("proposal_content")
    proposal_object_id = result.get("proposal_object_id")
    proposal_object_size = result.get("proposal_object_size")
    if proposal_content is not None:
        if (
            not isinstance(proposal_content, bytes)
            or len(proposal_content) > max_object_bytes
            or proposal_object_id is not None
            or proposal_object_size is not None
        ):
            raise ValueError("hybrid worker result has invalid proposal content")
        proposal_size = len(proposal_content)
    elif proposal_object_id is not None or proposal_object_size is not None:
        if (
            not isinstance(proposal_object_id, str)
            or len(proposal_object_id) != 64
            or any(
                char not in "0123456789abcdef" for char in proposal_object_id
            )
            or isinstance(proposal_object_size, bool)
            or not isinstance(proposal_object_size, int)
            or proposal_object_size < 0
            or proposal_object_size > max_object_bytes
        ):
            raise ValueError("hybrid worker result has invalid proposal object")
        proposal_size = proposal_object_size
        if result_object_store is not None:
            try:
                snapshot = result_object_store.snapshot(proposal_object_id)
            except (OSError, ValueError) as error:
                raise ValueError(
                    "hybrid worker proposal object is unavailable"
                ) from error
            if (
                snapshot.sha256 != proposal_object_id
                or snapshot.identity.size != proposal_object_size
            ):
                raise ValueError(
                    "hybrid worker proposal object failed verification"
                )
    else:
        proposal_size = 0
    if proposal_content is None and proposal_object_id is None and proposal_bitmap is not None:
        raise ValueError("hybrid worker result has an incomplete proposal")
    if proposal_size:
        logical_bytes += proposal_size
        if logical_bytes > max_bytes:
            raise ValueError("hybrid worker result exceeds its byte budget")

    def validate_short_text(name: str, maximum: int = 256) -> None:
        value = result.get(name)
        if value is not None and (
            not isinstance(value, str) or len(value) > maximum or "\x00" in value
        ):
            raise ValueError(f"hybrid worker result has invalid {name}")

    for name in (
        "lease_id",
        "lease_fence",
        "state_task_id",
        "parameter_token",
    ):
        validate_short_text(name)
    validate_short_text("error", 4096)
    for name in ("input_object_id", "proposal_id", "continuation_id"):
        validate_short_text(name, 64)
        value = result.get(name)
        if value and (
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
        ):
            raise ValueError(f"hybrid worker result has invalid {name}")

    strategy = result.get("strategy")
    if strategy is not None and (
        isinstance(strategy, bool)
        or not isinstance(strategy, int)
        or strategy < 0
        or strategy >= len(SYMCC_STRATEGY_PROFILES)
    ):
        raise ValueError("hybrid worker result has an invalid strategy")
    executor = result.get("executor")
    if executor is not None and executor not in {"exact", "tailored", "sampling"}:
        raise ValueError("hybrid worker result has an invalid executor")
    engine = result.get("engine")
    if engine is not None and engine not in {"symcc", "symsan"}:
        raise ValueError("hybrid worker result has an invalid engine")
    for name in ("tace_profiled", "tace_focused"):
        if name in result and not isinstance(result[name], bool):
            raise ValueError(f"hybrid worker result has invalid {name}")

    actions = result.get("s2f_actions")
    if actions is not None:
        if not isinstance(actions, (list, tuple)) or len(actions) > 64:
            raise ValueError("hybrid worker result has invalid S2F actions")
        seen_actions: set[int] = set()
        for row in actions:
            if not isinstance(row, (list, tuple)) or len(row) != 2:
                raise ValueError("hybrid worker result has invalid S2F actions")
            branch, action = row
            if (
                isinstance(branch, bool)
                or not isinstance(branch, int)
                or branch <= 0
                or branch > _MAX_BRANCH_ID
                or branch in seen_actions
                or action not in {"solve", "sample", "skip"}
            ):
                raise ValueError("hybrid worker result has invalid S2F actions")
            seen_actions.add(branch)

    schedule_prefix = result.get("schedule_prefix")
    if schedule_prefix is not None:
        if (
            not isinstance(schedule_prefix, (list, tuple))
            or len(schedule_prefix) > 256
            or any(
                isinstance(tid, bool)
                or not isinstance(tid, int)
                or tid < 0
                or tid > (1 << 32) - 1
                for tid in schedule_prefix
            )
        ):
            raise ValueError("hybrid worker result has invalid schedule prefix")
    schedule_trace = result.get("schedule_trace")
    if schedule_trace is not None and (
        not isinstance(schedule_trace, str)
        or len(schedule_trace) > max_schedule_trace_bytes
        or "\x00" in schedule_trace
    ):
        raise ValueError("hybrid worker result has invalid schedule trace")

    timeout_sites = result.get("timeout_sites")
    if timeout_sites is not None:
        if (
            not isinstance(timeout_sites, (list, tuple))
            or len(timeout_sites) > max_timeout_sites
        ):
            raise ValueError("hybrid worker result has invalid timeout sites")
        seen_sites: set[int] = set()
        for site in timeout_sites:
            if (
                isinstance(site, bool)
                or not isinstance(site, int)
                or site <= 0
                or site > _MAX_BRANCH_ID
                or site in seen_sites
            ):
                raise ValueError("hybrid worker result has invalid timeout sites")
            seen_sites.add(site)

    frontier = result.get("continuation_frontier")
    if frontier is not None:
        if not isinstance(frontier, (list, tuple)) or len(frontier) > max_objects:
            raise ValueError("hybrid worker result has invalid continuation frontier")
        seen_checkpoints: set[str] = set()
        for checkpoint in frontier:
            if (
                not isinstance(checkpoint, str)
                or len(checkpoint) != 64
                or any(char not in "0123456789abcdef" for char in checkpoint)
                or checkpoint in seen_checkpoints
            ):
                raise ValueError(
                    "hybrid worker result has invalid continuation frontier"
                )
            seen_checkpoints.add(checkpoint)
        if total_generated != len(frontier):
            raise ValueError("hybrid worker result has inconsistent continuation count")
    continuation_generated = result.get("continuation_generated")
    if continuation_generated is not None and (
        isinstance(continuation_generated, bool)
        or not isinstance(continuation_generated, int)
        or continuation_generated < 0
        or continuation_generated > max_objects
    ):
        raise ValueError("hybrid worker result has invalid continuation telemetry")

    overrides = result.get("parameter_overrides")
    if overrides is not None and (
        not isinstance(overrides, dict)
        or overrides != sanitize_parameter_overrides(overrides)
    ):
        raise ValueError("hybrid worker result has invalid parameter overrides")

    telemetry_raw = result.get("telemetry")
    if telemetry_raw is not None:
        if not isinstance(telemetry_raw, dict):
            raise ValueError("hybrid worker result has invalid telemetry")
        fields = SolverTelemetry.__dataclass_fields__
        if len(telemetry_raw) != len(fields) or set(telemetry_raw) != set(fields):
            raise ValueError("hybrid worker result has incomplete telemetry")
        sequence_limits = {
            "capabilities": 32,
            "missing_fields": len(fields),
            "open_branches": 256,
            "branch_trace": 512,
            "data_features": 512,
            "empirical_value_profiles": 512,
            "empirical_domain_feedback": 512,
            "static_data_features": 2048,
            "comparison_taints": 512,
        }
        for name, maximum in sequence_limits.items():
            value = telemetry_raw.get(name)
            if not isinstance(value, (list, tuple)) or len(value) > maximum:
                raise ValueError("hybrid worker result has oversized telemetry")
        normalized = asdict(SolverTelemetry.from_mapping(telemetry_raw))
        if telemetry_raw != normalized:
            raise ValueError("hybrid worker result has non-canonical telemetry")

    return result


class _HybridResultAdmissionService:
    """Bounded, non-blocking master-side validation pipeline."""

    def __init__(
        self,
        *,
        max_workers: int,
        capacity: int,
        max_objects: int,
        max_bytes: int,
        max_object_bytes: int,
        max_hints: int,
        max_timeout_sites: int,
        max_schedule_trace_bytes: int,
        result_object_store: ContentAddressedInputStore | None = None,
    ) -> None:
        if (
            type(max_workers) is not int
            or max_workers < 1
            or type(capacity) is not int
            or capacity < max_workers
        ):
            raise ValueError(
                "hybrid result admission capacity must cover all validators"
            )
        self.max_workers = max_workers
        self.capacity = capacity
        self._limits = {
            "max_objects": max_objects,
            "max_bytes": max_bytes,
            "max_object_bytes": max_object_bytes,
            "max_hints": max_hints,
            "max_timeout_sites": max_timeout_sites,
            "max_schedule_trace_bytes": max_schedule_trace_bytes,
            "result_object_store": result_object_store,
        }
        # Validate limit configuration before any result enters the executor.
        _validate_hybrid_worker_result(
            {},
            **self._limits,
        )
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="symcc-result-admission",
        )
        self._closed = False
        self._pending: deque[
            tuple[int, typing.Any, Future, float, set[str]]
        ] = deque()
        self._metrics_lock = threading.Lock()
        self._active_validations = 0
        self._active_validation_started = 0.0
        self._batches = 0
        self._submitted = 0
        self._invalid = 0
        self._maximum_batch = 0
        self._validation_service_seconds = 0.0
        self._validation_queue_seconds = 0.0
        self._validation_response_seconds = 0.0
        self._validation_parallel_wall_seconds = 0.0
        self._validation_ordered_commit_seconds = 0.0

    def _validate_timed(
        self,
        result: typing.Any,
        submitted_at: float,
    ) -> tuple[
        dict[str, typing.Any] | None,
        ValueError | None,
        float,
    ]:
        started_at = time.monotonic()
        with self._metrics_lock:
            self._validation_queue_seconds += max(0.0, started_at - submitted_at)
            if self._active_validations == 0:
                self._active_validation_started = started_at
            self._active_validations += 1
        try:
            try:
                validated = _validate_hybrid_worker_result(
                    result,
                    **self._limits,
                )
            except ValueError as error:
                return None, error, time.monotonic()
            return validated, None, time.monotonic()
        finally:
            finished_at = time.monotonic()
            with self._metrics_lock:
                self._validation_service_seconds += max(
                    0.0, finished_at - started_at
                )
                self._validation_response_seconds += max(
                    0.0, finished_at - submitted_at
                )
                self._active_validations -= 1
                if self._active_validations == 0:
                    self._validation_parallel_wall_seconds += max(
                        0.0, finished_at - self._active_validation_started
                    )
                    self._active_validation_started = 0.0

    @property
    def available_capacity(self) -> int:
        return self.capacity - len(self._pending)

    @property
    def pending(self) -> int:
        return len(self._pending)

    def submit_many(
        self,
        records: list[tuple[int, typing.Any, typing.Any]],
    ) -> None:
        if self._closed:
            raise RuntimeError("hybrid result admission service is closed")
        if len(records) > self.available_capacity:
            raise ValueError("hybrid result admission batch exceeds capacity")
        if not records:
            return
        submitted_at = time.monotonic()
        for worker, dispatch, result in records:
            future = self._executor.submit(
                self._validate_timed,
                result,
                submitted_at,
            )
            self._pending.append((
                worker,
                dispatch,
                future,
                submitted_at,
                _hybrid_result_object_ids(result),
            ))
        self._batches += 1
        self._submitted += len(records)
        self._maximum_batch = max(self._maximum_batch, len(records))

    def has_ready(self) -> bool:
        return any(
            future.done()
            for _worker, _dispatch, future, _submitted, _objects in self._pending
        )

    def protected_object_ids(self) -> set[str]:
        protected: set[str] = set()
        for _worker, _dispatch, _future, _submitted, object_ids in self._pending:
            protected.update(object_ids)
        return protected

    def collect_ready(
        self,
        *,
        wait: bool = False,
    ) -> list[
        tuple[int, typing.Any, dict[str, typing.Any] | None, ValueError | None]
    ]:
        """Return completed validations without waiting behind a slow head item."""
        admitted = []
        remaining: deque[
            tuple[int, typing.Any, Future, float, set[str]]
        ] = deque()
        while self._pending:
            worker, dispatch, future, submitted_at, object_ids = self._pending.popleft()
            if not wait and not future.done():
                remaining.append((
                    worker,
                    dispatch,
                    future,
                    submitted_at,
                    object_ids,
                ))
                continue
            result, error, finished_at = future.result()
            if error is not None:
                self._invalid += 1
                admitted.append((worker, dispatch, None, error))
            else:
                admitted.append((worker, dispatch, result, None))
            self._validation_ordered_commit_seconds += max(
                0.0, time.monotonic() - finished_at
            )
        self._pending = remaining
        return admitted

    def validate_many(
        self,
        records: list[tuple[int, typing.Any, typing.Any]],
    ) -> list[tuple[int, typing.Any, dict[str, typing.Any] | None, ValueError | None]]:
        if self._pending:
            raise RuntimeError(
                "blocking admission cannot join an active asynchronous batch"
            )
        self.submit_many(records)
        return self.collect_ready(wait=True)

    def snapshot(self) -> dict[str, int | float]:
        with self._metrics_lock:
            parallel_wall = self._validation_parallel_wall_seconds
            if self._active_validations:
                parallel_wall += max(
                    0.0, time.monotonic() - self._active_validation_started
                )
            service_seconds = self._validation_service_seconds
            queue_seconds = self._validation_queue_seconds
            response_seconds = self._validation_response_seconds
        return {
            "workers": self.max_workers,
            "capacity": self.capacity,
            "batches": self._batches,
            "submitted": self._submitted,
            "invalid": self._invalid,
            "pending": len(self._pending),
            "available_capacity": self.available_capacity,
            "maximum_batch": self._maximum_batch,
            # Backward-compatible key with corrected wall-clock semantics.
            "validation_seconds": parallel_wall,
            "validation_parallel_wall_seconds": parallel_wall,
            "validation_service_seconds": service_seconds,
            "validation_queue_seconds": queue_seconds,
            "validation_response_seconds": response_seconds,
            "validation_ordered_commit_seconds": (
                self._validation_ordered_commit_seconds
            ),
        }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=True)
        self._pending.clear()


def _receive_hybrid_result_batch(
    comm: typing.Any,
    generation_gate: _DispatchGenerationGate,
    active_dispatches: typing.Mapping[int, typing.Any],
    active_workers: typing.Container[int],
    capacity: int,
    *,
    status_factory: typing.Callable[[], typing.Any] = MPI.Status,
) -> tuple[
    list[tuple[int, typing.Any, typing.Any]],
    list[tuple[int, str]],
    int,
]:
    """Receive at most capacity messages, including quarantined generations."""
    if type(capacity) is not int or capacity < 1:
        raise ValueError("hybrid result receive capacity must be positive")
    current = []
    quarantined = []
    received = 0
    while (
        received < capacity
        and comm.iprobe(source=MPI.ANY_SOURCE, tag=TAG_RESULT)
    ):
        status = status_factory()
        result = comm.recv(source=MPI.ANY_SOURCE, tag=TAG_RESULT, status=status)
        received += 1
        worker = status.Get_source()
        dispatch = active_dispatches.get(worker)
        result_status = generation_gate.observe_result(
            worker,
            dispatch.dispatch_token
            if dispatch is not None and worker in active_workers
            else "",
            result,
        )
        if result_status != "current":
            quarantined.append((worker, result_status))
            continue
        current.append((worker, dispatch, result))
    return current, quarantined, received


def _load_timeout_sites(
    path: str,
    *,
    max_sites: int,
    max_bytes: int,
) -> list[int]:
    """Load a bounded, canonical timeout-site set emitted by the runtime."""
    if any(
        isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
        for limit in (max_sites, max_bytes)
    ):
        raise ValueError("timeout-site limits must be positive integers")
    with open(path, "r", encoding="ascii") as stream:
        raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("timeout-site artifact exceeds its byte budget")
    sites: list[int] = []
    seen: set[int] = set()
    for token in raw.split():
        try:
            site = int(token, 0)
        except ValueError as error:
            raise ValueError("timeout-site artifact is malformed") from error
        if site <= 0 or site > _MAX_BRANCH_ID or site in seen:
            raise ValueError("timeout-site artifact is not canonical")
        seen.add(site)
        sites.append(site)
        if len(sites) > max_sites:
            raise ValueError("timeout-site artifact exceeds its site budget")
    return sites


def _worker_file_identity(metadata: os.stat_result) -> _WorkerFileIdentity:
    return _WorkerFileIdentity(
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_ctime_ns),
    )


def _scan_worker_output_candidates(
    output_dir: str,
    *,
    max_objects: int,
    max_bytes: int,
    max_object_bytes: int,
) -> tuple[list[_WorkerOutputCandidate], list[_WorkerOutputCandidate]]:
    """Preflight a flat worker result namespace before reading any payload."""
    limits = (max_objects, max_bytes, max_object_bytes)
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 1
        for value in limits
    ):
        raise ValueError("hybrid worker result limits must be positive integers")

    candidates: list[_WorkerOutputCandidate] = []
    hints: list[_WorkerOutputCandidate] = []
    logical_bytes = 0
    scanned_entries = 0
    entry_limit = max_objects * 2 + _WORKER_RESULT_AUXILIARY_SLACK
    with os.scandir(output_dir) as entries:
        for entry in entries:
            scanned_entries += 1
            if scanned_entries > entry_limit:
                raise _WorkerResultBudgetExceeded(
                    "directory_entries",
                    scanned_entries,
                    entry_limit,
                    objects=len(candidates),
                )
            if entry.name.startswith("."):
                continue
            is_hint = entry.name.endswith(".hints")
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(metadata.st_mode):
                continue
            identity = _worker_file_identity(metadata)
            target = hints if is_hint else candidates
            target.append(_WorkerOutputCandidate(entry.path, entry.name, identity))
            if len(target) > max_objects:
                resource = "hint_files" if is_hint else "objects"
                raise _WorkerResultBudgetExceeded(
                    resource,
                    len(target),
                    max_objects,
                    objects=len(candidates),
                )
            if identity.size > max_object_bytes:
                raise _WorkerResultBudgetExceeded(
                    "object_bytes",
                    identity.size,
                    max_object_bytes,
                    objects=len(candidates),
                )
            logical_bytes += identity.size
            if logical_bytes > max_bytes:
                raise _WorkerResultBudgetExceeded(
                    "bytes",
                    logical_bytes,
                    max_bytes,
                    objects=len(candidates),
                )
    # ``os.scandir`` follows filesystem enumeration order, which is not a
    # reproducible scheduling policy once post-processing is time bounded.
    # Sorting here keeps both coverage admission and retry behavior stable.
    candidates.sort(key=lambda candidate: candidate.name)
    hints.sort(key=lambda candidate: candidate.name)
    return candidates, hints


def _read_worker_output_snapshot(
    candidate: _WorkerOutputCandidate,
    *,
    max_bytes: int,
) -> bytes | None:
    """Read one bounded regular output while closing fd/path identity races."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("worker output byte limit must be a positive integer")
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None or candidate.identity.size > max_bytes:
        return None
    flags = (
        os.O_RDONLY
        | no_follow
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = -1
    try:
        descriptor = os.open(candidate.path, flags)
        metadata_before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata_before.st_mode)
            or _worker_file_identity(metadata_before) != candidate.identity
        ):
            return None
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            content = stream.read(max_bytes + 1)
        metadata_after = os.fstat(descriptor)
        path_metadata = os.stat(candidate.path, follow_symlinks=False)
        if (
            len(content) != candidate.identity.size
            or len(content) > max_bytes
            or not stat.S_ISREG(path_metadata.st_mode)
            or _worker_file_identity(metadata_after) != candidate.identity
            or _worker_file_identity(path_metadata) != candidate.identity
        ):
            return None
        return content
    except OSError:
        return None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _configure_string_solver_capture(
    env: dict[str, str],
    output_dir: str,
) -> str:
    """Enable per-run string-constraint capture for worker-side materialization."""
    if not _env_enabled(env, "SYMCC_STRING_SOLVER_ENABLE", "1"):
        return ""
    artifact = os.path.join(output_dir, ".string_constraints.jsonl")
    env["SYMCC_STRING_CONSTRAINT_OUT"] = artifact
    env.setdefault("SYMCC_STRING_CONSTRAINT_MAX_BYTES", "256")
    env.setdefault("SYMCC_STRING_CONSTRAINT_MAX_RECORDS", "4096")
    return artifact


def _string_solver_backend(env: dict[str, str]) -> typing.Any:
    configured = str(env.get("SYMCC_STRING_SOLVER", "")).strip()
    portfolio = str(env.get("SYMCC_STRING_SOLVER_PORTFOLIO", "")).strip()
    try:
        command = tuple(shlex.split(configured)) if configured else ()
    except ValueError:
        command = ()
    key = (configured, portfolio)
    if key not in _STRING_SOLVER_BACKENDS:
        try:
            _STRING_SOLVER_BACKENDS[key] = (
                string_solver_backend_from_configuration(
                    portfolio,
                    fallback_command=command or None,
                )
                if portfolio
                else SymccJsonStringBackend(command or None)
            )
        except (
            OSError,
            ValueError,
            TypeError,
            FileNotFoundError,
            json.JSONDecodeError,
        ):
            _STRING_SOLVER_BACKENDS[key] = None
    return _STRING_SOLVER_BACKENDS[key]


def _write_worker_candidate(
    output_dir: str,
    prefix: str,
    index: int,
    content: bytes,
) -> bool:
    path = os.path.join(output_dir, f"{prefix}-{index:06d}")
    tmp = path + ".tmp"
    try:
        with open(tmp, "wb") as stream:
            stream.write(content)
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def _materialize_string_solver_outputs(
    artifact: str,
    input_file: str,
    output_dir: str,
    env: dict[str, str],
) -> dict[str, int]:
    """Write verified string-theory/string-patch candidates into output_dir."""
    metrics = {
        "enabled": 1 if artifact else 0,
        "records_loaded": 0,
        "records_scanned": 0,
        "written": 0,
        "errors": 0,
        "solver_queries": 0,
        "solver_sat": 0,
        "solver_verified": 0,
        "solver_rejected": 0,
        "solver_errors": 0,
        "solver_unsat": 0,
        "solver_unknown": 0,
        "solver_backend_runs": 0,
        "solver_disagreements": 0,
        "solver_duplicate_models": 0,
        "solver_backends_selected": 0,
        "solver_backends_skipped": 0,
        "solver_policy_updates": 0,
        "solver_policy_explorations": 0,
        "solver_policy_contexts": 0,
        "dual_view_queries": 0,
        "dual_view_verified": 0,
    }
    if not artifact or not os.path.isfile(artifact):
        return metrics
    try:
        query_limit = _bounded_env_int(
            env, "SYMCC_STRING_SOLVER_QUERY_LIMIT", 4, 0, 256
        )
        budget = _bounded_env_int(env, "SYMCC_STRING_SOLVER_CANDIDATES", 8, 0, 256)
        timeout_ms = _bounded_env_int(
            env, "SYMCC_STRING_SOLVER_TIMEOUT_MS", 250, 1, 60000
        )
        if query_limit <= 0 or budget <= 0:
            return metrics
        records = load_string_constraints(artifact)
        metrics["records_loaded"] = len(records)
        records = records[:query_limit]
        metrics["records_scanned"] = len(records)
        with open(input_file, "rb") as stream:
            witness = stream.read()
    except (OSError, ValueError, json.JSONDecodeError):
        metrics["errors"] += 1
        return metrics

    backend = _string_solver_backend(env)
    candidates, candidate_metrics = materialize_string_candidates(
        records,
        witness,
        budget,
        solver_backend=backend,
        solver_timeout_ms=timeout_ms,
    )
    for key in (
        "solver_queries",
        "solver_sat",
        "solver_verified",
        "solver_rejected",
        "solver_errors",
        "solver_unsat",
        "solver_unknown",
        "solver_backend_runs",
        "solver_disagreements",
        "solver_duplicate_models",
        "solver_backends_selected",
        "solver_backends_skipped",
        "solver_policy_updates",
        "solver_policy_explorations",
        "solver_policy_contexts",
        "dual_view_queries",
        "dual_view_verified",
    ):
        metrics[key] = int(candidate_metrics.get(key, 0))
    for index, content in enumerate(candidates):
        if _write_worker_candidate(output_dir, "string-solver", index, content):
            metrics["written"] += 1
    return metrics


def run_symcc_worker(
    target_cmd: list[str],
    input_file: str,
    output_dir: str,
    timeout_sec: int,
    use_stdin: bool,
    engine_name: str | None = None,
    base_env: "dict[str, str] | None" = None,
    streaming_showmap: "StreamingShowmap | None" = None,
    worker_coverage: "CoverageBitmap | None" = None,
    inflight: "dict | None" = None,
    redun: "dict | None" = None,
    worker_seen: "set[bytes] | None" = None,
    result_max_objects: int | None = None,
    result_max_bytes: int | None = None,
    result_max_hints: int | None = None,
    raw_save_all_dir: str | None = None,
    coverage_snapshot_path: str | None = None,
    coverage_snapshot_version_ref: list[int] | None = None,
) -> "tuple[list[dict], int, int, float, bool, float]":
    """在单个输入上运行 SymCC。

    返回 ``(new_tests, total_generated, retcode, elapsed, killed,
    post_elapsed)``：
      - new_tests: list[dict]，每项含 "content"（bytes），可选 "bitmap"（稀疏边列表）
        和 "hints"（约束提示）。
      - total_generated: int，SymCC 本次生成的测试用例总数（含被 dedup 过滤的）。
      - retcode: int，SymCC 进程返回码（超时/被杀为负）。
      - elapsed: float，执行耗时（秒）。
      - killed: bool，是否因超时被杀。
      - post_elapsed: float，输出准入、showmap 和去重耗时（秒）。

    若提供 streaming_showmap 与 worker_coverage，会在 worker 端为每个输出运行
    afl-showmap（流式 fork server）收集稀疏边，并用 worker_coverage 本地 dedup，
    仅回传发现新覆盖或触发终止状态的用例，master 只需内存中比较稀疏边列表。

    输出内容在任何 payload 读取前接受对象数、总逻辑字节和单对象预算检查；
    超限会抛出 ``_WorkerResultBudgetExceeded``，调用方必须整批拒绝而不能截断。
    """
    if base_env is not None:
        env = dict(base_env)  # 浅拷贝，避免修改调用方字典
    else:
        env = os.environ.copy()
    configured_limits = (
        (
            "objects",
            result_max_objects,
            "SYMCC_HYBRID_RESULT_MAX_OBJECTS",
            _DEFAULT_HYBRID_RESULT_MAX_OBJECTS,
            _MAX_HYBRID_RESULT_OBJECTS,
        ),
        (
            "bytes",
            result_max_bytes,
            "SYMCC_HYBRID_RESULT_MAX_BYTES",
            _DEFAULT_HYBRID_RESULT_MAX_BYTES,
            _MAX_HYBRID_RESULT_BYTES,
        ),
        (
            "hints",
            result_max_hints,
            "SYMCC_HYBRID_RESULT_MAX_HINTS",
            _DEFAULT_HYBRID_RESULT_MAX_HINTS,
            _MAX_HYBRID_RESULT_HINTS,
        ),
    )
    resolved_limits: dict[str, int] = {}
    for resource, explicit, key, default, upper in configured_limits:
        if explicit is None:
            resolved_limits[resource] = _bounded_env_int(env, key, default, 1, upper)
        elif (
            isinstance(explicit, bool)
            or not isinstance(explicit, int)
            or explicit < 1
            or explicit > upper
        ):
            raise ValueError(f"invalid hybrid worker result {resource} limit")
        else:
            resolved_limits[resource] = explicit
    result_max_objects = resolved_limits["objects"]
    result_max_bytes = resolved_limits["bytes"]
    result_max_hints = resolved_limits["hints"]
    result_max_object_bytes = min(
        result_max_bytes,
        _bounded_env_int(
            env,
            "SYMCC_MAX_TRANSPORT_INPUT",
            16 * 1024 * 1024,
            1,
            _MAX_HYBRID_RESULT_BYTES,
        ),
    )

    os.makedirs(output_dir, exist_ok=True)

    # 引擎专属环境变量(SymCC 的 SYMCC_OUTPUT_DIR / SymSan 的 TAINT_OPTIONS 等)由所选引擎在
    # 下方 wrap_run 中设置。此处仅保证输出目录已建好(上面 makedirs)。
    string_constraint_artifact = _configure_string_solver_capture(env, output_dir)

    # #10 拆分：在本次 concolic 运行【之前】快照 worker 端已覆盖位图，据此把冗余输出分为
    # "没打到任何新边(乐观求解不可行/冗余)"与"打到新边但本 item 内已被自己覆盖(新鲜度间隙)"。
    #
    # 快照必须取自 worker_coverage（AFL 边 ID 空间，由 master 经 bmsync 播种）。
    # 【勿改回读 SYMCC_AFL_COVERAGE_MAP】：那个变量指向 QSYM 自己的 qsym_bitmap
    # （XXH32(pc,taken) 哈希空间、131072B），与 afl-showmap 的边 ID 不是同一个空间，
    # 拿边 ID 去索引它得到的分类无意义；且 edges 是 [(edge_id, count)] 元组列表，
    # 旧代码 `e >= len(_snap)` 是 tuple>=int，会抛 TypeError 直接打死 worker
    # （只在 SYMCC_WORKER_PROFILE=1 且非首个 item 触发，故长期未被发现）。
    _snap = None
    if redun is not None:
        if worker_coverage is not None and worker_coverage.data is not None:
            _snap = bytes(worker_coverage.data)
        redun["items"] = redun.get("items", 0) + 1
        if _snap is None:
            redun["snap_none"] = redun.get("snap_none", 0) + 1

    # 由所选 concolic 引擎(SYMCC_ENGINE,默认 symcc)决定实际命令 + 环境 + 是否喂 stdin。
    # SymCC:自驱二进制写 SYMCC_OUTPUT_DIR;SymSan:经 fgtest driver 写 TAINT_OPTIONS 的 output_dir。
    _engine = get_engine(engine_name)
    cmd, env, feed_stdin = _engine.wrap_run(
        target_cmd, input_file, output_dir, env, use_stdin, timeout_sec
    )

    start = time.monotonic()
    python_timeout = timeout_sec + 15
    try:
        if feed_stdin:
            with open(input_file, "rb") as inf:
                proc = subprocess.run(
                    cmd,
                    stdin=inf,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=env,
                    timeout=python_timeout,
                )
        else:
            proc = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                timeout=python_timeout,
            )
        retcode = proc.returncode
    except subprocess.TimeoutExpired:
        print(
            f"[Worker {MPI.COMM_WORLD.Get_rank()}] Python-level timeout "
            f"({python_timeout}s)",
            file=sys.stderr,
            flush=True,
        )
        retcode = 124
    except (OSError, subprocess.SubprocessError) as e:
        print(f"[Worker {MPI.COMM_WORLD.Get_rank()}] Error: {e}", file=sys.stderr)
        retcode = -1

    elapsed = time.monotonic() - start
    killed = _returncode_indicates_timeout(retcode)

    # Coverage may advance while this worker is blocked inside the concolic
    # target.  Refresh immediately before showmap/dedup so those intervening
    # global discoveries do not become redundant MPI result payloads.
    _refresh_worker_coverage_snapshot(
        coverage_snapshot_path,
        worker_coverage,
        coverage_snapshot_version_ref,
    )

    # 相位 F 计时起点：输出收集 + afl-showmap 取边 + worker 端 coverage dedup。
    # 这段【不】计入上面的 elapsed（相位 E=concolic 执行），用于区分"执行慢"还是
    # "showmap/dedup 后处理慢"——瓶颈分析的关键。
    _post_start = time.monotonic()
    # 通知调用方相位已从 exec 转入 showmap_dedup：若此后被 SIGTERM 打断，在途时长归到正确相位，
    # 并记下已完成的 exec 时长（elapsed），供 flush 在 item 未跑完时补计到 exec（否则会丢失）。
    if inflight is not None:
        inflight["phase"] = "showmap_dedup"
        inflight["start"] = _post_start
        inflight["exec_done"] = elapsed

    string_solver_metrics = _materialize_string_solver_outputs(
        string_constraint_artifact, input_file, output_dir, env
    )
    if string_solver_metrics.get("records_loaded"):
        try:
            with open(
                os.path.join(output_dir, ".string_solver_metrics.json"),
                "w",
                encoding="ascii",
            ) as stream:
                json.dump(string_solver_metrics, stream, sort_keys=True)
                stream.write("\n")
        except OSError:
            pass

    # Collect test cases + worker 端 coverage dedup
    # Worker 有 master bitmap 副本，在本地做 coverage merge
    # 只传 interesting 的 TC（~3% 的输出），消息 323KB → 10KB
    new_tests = []
    reported_coverage_rows = 0
    staged_worker_coverage: dict[int, int] = {}
    staged_seen_content: set[bytes] = set()
    seen_content = worker_seen if worker_seen is not None else set()
    total_generated = 0
    postprocess_budget_sec = _bounded_env_float(
        env,
        "SYMCC_WORKER_POSTPROCESS_BUDGET_SEC",
        0.0,
        0.0,
        3600.0,
    )
    postprocess_deadline = (
        _post_start + postprocess_budget_sec
        if postprocess_budget_sec > 0.0
        else None
    )
    verify_batch_new = env.get("SYMCC_BATCH_VERIFY_NEW", "0").lower() not in {
        "0",
        "false",
        "off",
        "no",
    }
    verify_batch_status = env.get("SYMCC_BATCH_VERIFY_STATUS", "1").lower() not in {
        "0",
        "false",
        "off",
        "no",
    }

    def postprocess_budget_exhausted() -> bool:
        return (
            postprocess_deadline is not None
            and time.monotonic() >= postprocess_deadline
        )

    def remaining_postprocess_seconds() -> float | None:
        if postprocess_deadline is None:
            return None
        return max(0.0, postprocess_deadline - time.monotonic())

    def streaming_result(content: bytes) -> typing.Any:
        if postprocess_deadline is None:
            return streaming_showmap.get_result(content)
        return streaming_showmap.get_result(
            content,
            deadline=postprocess_deadline,
        )

    # (_snap 已在 SymCC 运行前从全局位图快照，见函数上方)
    if os.path.isdir(output_dir):
        try:
            candidates, hint_candidates = _scan_worker_output_candidates(
                output_dir,
                max_objects=result_max_objects,
                max_bytes=result_max_bytes,
                max_object_bytes=result_max_object_bytes,
            )
        except _WorkerResultBudgetExceeded as error:
            error.bind_execution(
                retcode=retcode,
                elapsed=elapsed,
                killed=killed,
                post_elapsed=time.monotonic() - _post_start,
            )
            raise
        except OSError:
            candidates, hint_candidates = [], []
        total_generated = len(candidates)

        # 先收集 hint 文件
        hint_map: dict[
            str, list[tuple[int, int, int]]
        ] = {}  # base_name -> [(offset, old, new)]
        hint_records = 0
        for candidate in hint_candidates:
            if postprocess_budget_exhausted():
                break
            base = candidate.name[:-6]  # 去掉 .hints 后缀
            try:
                encoded = _read_worker_output_snapshot(
                    candidate, max_bytes=result_max_object_bytes
                )
                if encoded is None:
                    continue
                hints = []
                for line in encoded.decode("utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split(":")
                    if len(parts) == 3:
                        hints.append(
                            (
                                int(parts[0]),
                                int(parts[1], 16),
                                int(parts[2], 16),
                            )
                        )
                        hint_records += 1
                        if hint_records > result_max_hints:
                            error = _WorkerResultBudgetExceeded(
                                "hints",
                                hint_records,
                                result_max_hints,
                                objects=total_generated,
                            )
                            error.bind_execution(
                                retcode=retcode,
                                elapsed=elapsed,
                                killed=killed,
                                post_elapsed=time.monotonic() - _post_start,
                            )
                            raise error
                if hints:
                    hint_map[base] = hints
            except _WorkerResultBudgetExceeded:
                raise
            except (UnicodeError, ValueError):
                continue

        # 内容级预去重(#1,跨 item):SymCC 常吐出【字节完全相同】的重复输出(实测约 23–28%);字节相同
        # → 边集必然相同 → dedup 结果必与首次相同(不可能为"新"),可跳过昂贵的 showmap。用 worker 生命
        # 周期的有界集(worker_seen)兼吃 item 内与 item 间重复;未提供则退回 per-item 集。
        if len(seen_content) >= _WORKER_SEEN_CAP:
            _trim_tracking_container(
                seen_content, _WORKER_SEEN_CAP, retain_ratio=0.875
            )

        # pass 1:逐对象稳定读取并只保留摘要。内容在 pass 2 按需重读，避免
        # showmap 前同时保留本轮全部唯一输出。摘要在覆盖判定完成前只属于
        # 本批 tentative 集，不能提前提交到跨 item 的 worker_seen；否则预算
        # 中断会把尚未执行 showmap 的候选永久标记为重复。
        uniq: "list[tuple[_WorkerOutputCandidate, bytes]]" = []
        tentative_content: set[bytes] = set()
        if redun is not None:
            redun["gen"] += len(candidates)
        for candidate_index, candidate in enumerate(candidates):
            if postprocess_budget_exhausted():
                if redun is not None:
                    redun["postprocess_budget_skipped"] = redun.get(
                        "postprocess_budget_skipped", 0
                    ) + len(candidates) - candidate_index
                break
            content = _read_worker_output_snapshot(
                candidate, max_bytes=result_max_object_bytes
            )
            if content is None:
                continue
            _save_worker_raw_candidate(raw_save_all_dir, content)
            _ckey = hashlib.blake2b(content, digest_size=16).digest()
            if _ckey in seen_content or _ckey in tentative_content:
                if redun is not None:
                    redun["byte_dup"] = redun.get("byte_dup", 0) + 1
                continue
            tentative_content.add(_ckey)
            uniq.append((candidate, _ckey))

        # pass 2a:批量 showmap(#2,afl-showmap -I 一次 C 侧 forkserver 循环跑完,实测 ~25us/输入,比逐个
        # 流式 get_edges(~600us,含 Python 每次管道往返)快约一个数量级)。afl-showmap -I 失败→退回逐个流式。
        batch_edges: "dict[str, list[tuple[int, int]]]" = {}
        use_batch = False
        if (
            worker_coverage is not None
            and uniq
            and streaming_showmap is not None
            and os.environ.get("SYMCC_BATCH_SHOWMAP", "1") != "0"
            and getattr(streaming_showmap, "_afl_showmap", None)
        ):
            batch_arguments: dict[str, float] = {}
            batch_wall_timeout = remaining_postprocess_seconds()
            if batch_wall_timeout is not None:
                batch_arguments["wall_timeout_seconds"] = batch_wall_timeout
            batch_edges = batch_showmap_edges(
                streaming_showmap._afl_showmap,
                streaming_showmap._target_cmd,
                [candidate.path for candidate, _digest in uniq],
                output_dir,
                **batch_arguments,
            )
            use_batch = bool(batch_edges)

        # pass 2b:合并 + 收集 interesting(批量命中用 batch_edges,否则退回流式 get_edges)
        for candidate_index, (candidate, content_digest) in enumerate(uniq):
            if postprocess_budget_exhausted():
                if redun is not None:
                    redun["postprocess_budget_skipped"] = redun.get(
                        "postprocess_budget_skipped", 0
                    ) + len(uniq) - candidate_index
                break
            content = _read_worker_output_snapshot(
                candidate, max_bytes=result_max_object_bytes
            )
            if content is None:
                seen_content.discard(content_digest)
                continue
            try:
                if worker_coverage is not None and (
                    use_batch or streaming_showmap is not None
                ):
                    terminal_status: str | None = None
                    terminal_detail = 0
                    if use_batch:
                        edges = batch_edges.get(candidate.path)
                        # -I does not expose a per-input terminal status.
                        # Production hybrid runs enable status verification for
                        # every batch candidate; the separate novelty-only knob
                        # is retained for diagnostic campaigns where replay
                        # cost dominates the measurement.
                        if (
                            (
                                edges is None
                                or verify_batch_status
                                or (
                                    verify_batch_new
                                    and worker_coverage.count_delta(edges) > 0
                                )
                            )
                            and streaming_showmap is not None
                            and not postprocess_budget_exhausted()
                        ):
                            result = streaming_result(content)
                            if result is not None and result.status != "ok":
                                terminal_status = result.status
                                terminal_detail = int(result.status_detail)
                            elif (
                                result is not None
                                and result.status == "ok"
                                and result.edges
                            ):
                                edges = list(result.edges)
                    else:
                        result = streaming_result(content)
                        if result is not None and result.status != "ok":
                            terminal_status = result.status
                            terminal_detail = int(result.status_detail)
                        edges = (
                            list(result.edges)
                            if result is not None
                            and result.status == "ok"
                            and result.edges
                            else None
                        )
                    if terminal_status is not None:
                        tc_entry = {
                            "content": content,
                            "terminal_status": terminal_status,
                            "terminal_detail": terminal_detail,
                        }
                        if candidate.name in hint_map:
                            tc_entry["hints"] = hint_map[candidate.name]
                        new_tests.append(tc_entry)
                        staged_seen_content.add(content_digest)
                        continue
                    if (
                        edges is None
                        and streaming_showmap is not None
                        and not use_batch
                        and not postprocess_budget_exhausted()
                    ):
                        edges = one_shot_showmap_edges(
                            streaming_showmap._afl_showmap,
                            streaming_showmap._target_cmd,
                            content,
                            output_dir,
                            wall_timeout_seconds=remaining_postprocess_seconds(),
                        )
                    if edges is None:  # 无 map(超时/崩溃)且隔离回退也失败
                        seen_content.discard(content_digest)
                        if redun is not None:
                            redun["showmap_none"] += 1
                        continue
                    has_new_vs_snap = True  # 相对本 item 起点是否有新边(边级)
                    if redun is not None and _snap is not None:
                        # edges 是 [(edge_id, count)]，必须取 [0]；直接 `e >= len(_snap)`
                        # 是 tuple>=int，会抛 TypeError 打死整个 worker。
                        has_new_vs_snap = any(
                            eid >= len(_snap) or _snap[eid] == 0 for eid, _ in edges
                        )
                    novel_edges = _stage_coverage_delta_rows(
                        worker_coverage,
                        edges,
                        staged_worker_coverage,
                    )
                    is_new = bool(novel_edges)
                    if redun is not None:
                        if is_new:
                            redun["reported"] += 1  # worker 判新 → 上报 master
                        elif not has_new_vs_snap:
                            redun["infeasible"] += (
                                1  # 没打到任何新边(乐观求解不可行/冗余)
                            )
                        else:
                            redun["worker_fresh"] += (
                                1  # 打到新边但本 item 内已被自己覆盖
                            )
                    if is_new:
                        reported_coverage_rows += len(novel_edges)
                        if reported_coverage_rows > StreamingShowmap.MAX_EDGES:
                            error = _WorkerResultBudgetExceeded(
                                "coverage_rows",
                                reported_coverage_rows,
                                StreamingShowmap.MAX_EDGES,
                                objects=len(new_tests) + 1,
                            )
                            error.bind_execution(
                                retcode=retcode,
                                elapsed=elapsed,
                                killed=killed,
                                post_elapsed=time.monotonic() - _post_start,
                            )
                            raise error
                        tc_entry = {
                            "content": content,
                            # Only transmit bits newly introduced by this
                            # candidate. Earlier candidates in the same RESULT
                            # already carry every older worker-local bit.
                            "bitmap": novel_edges,
                        }
                        if candidate.name in hint_map:  # 附加约束 hint 信息
                            tc_entry["hints"] = hint_map[candidate.name]
                        new_tests.append(tc_entry)
                else:
                    tc_entry = {"content": content}
                    if candidate.name in hint_map:
                        tc_entry["hints"] = hint_map[candidate.name]
                    new_tests.append(tc_entry)
                # Commit cross-item dedup only after this candidate reached a
                # terminal coverage/admission decision. Budget-skipped and
                # transiently unreadable candidates remain retryable.
                staged_seen_content.add(content_digest)
            except (IOError, OSError):
                # A transient showmap/read failure must remain retryable on a
                # later item instead of poisoning worker_seen.
                continue

    if staged_worker_coverage:
        worker_coverage.merge_delta(sorted(staged_worker_coverage.items()))
        worker_coverage.consume_delta()
    seen_content.update(staged_seen_content)

    post_elapsed = time.monotonic() - _post_start
    return new_tests, total_generated, retcode, elapsed, killed, post_elapsed


def _read_tace_density(path: str, input_size: int) -> list[int]:
    """Read a bounded density profile and return relevant input offsets."""
    offsets: list[int] = []
    try:
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                if not line.strip() or line.startswith("#"):
                    continue
                fields = line.split()
                if len(fields) != 2:
                    continue
                offset, density = int(fields[0]), int(fields[1])
                if 0 <= offset < input_size and density > 0:
                    offsets.append(offset)
    except (OSError, ValueError):
        return []
    return sorted(dict.fromkeys(offsets))


def _profile_tace_dependencies(
    target_cmd: list[str],
    input_file: str,
    profile_dir: str,
    timeout_sec: int,
    use_stdin: bool,
    base_env: dict[str, str],
) -> list[int]:
    """Run QSYM in no-solve density mode for a TACE dependency pass."""
    try:
        input_size = os.path.getsize(input_file)
    except OSError:
        return []
    os.makedirs(profile_dir, exist_ok=True)
    density_path = os.path.join(profile_dir, "density")
    try:
        os.unlink(density_path)
    except OSError:
        pass
    environment = dict(base_env)
    for key in (
        "SYMCC_FOCUS_BYTES",
        "SYMCC_FOCUS_SET",
        "SYMCC_TARGET_BRANCH",
        "SYMCC_S2F_ACTIONS",
        "SYMCC_DIRECTED_PRUNE",
        "SYMCC_SKIP_SITES",
        "SYMCC_TELEMETRY_OUT",
        "SYMCC_AFL_COVERAGE_MAP",
    ):
        environment.pop(key, None)
    environment["SYMCC_DENSITY_OUT"] = density_path
    environment["SYMCC_OUTPUT_DIR"] = profile_dir
    engine = get_engine("symcc")
    cmd, environment, feed_stdin = engine.wrap_run(
        target_cmd, input_file, profile_dir, environment, use_stdin, timeout_sec
    )
    try:
        if feed_stdin:
            with open(input_file, "rb") as stream:
                subprocess.run(
                    cmd,
                    stdin=stream,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=environment,
                    timeout=timeout_sec + 5,
                    check=False,
                )
        else:
            subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
                timeout=timeout_sec + 5,
                check=False,
            )
    except (OSError, subprocess.SubprocessError):
        return []
    return _read_tace_density(density_path, input_size)


def corpus_showmap_edges(
    afl_showmap: str,
    target_cmd: "list[str]",
    file_paths: "list[str]",
    work_dir: str,
    timeout_ms: int = 5000,
    use_qemu: bool = False,
    require_all: bool = False,
) -> "list[tuple[int, int]] | None":
    """Collect aggregate AFL edges for a small corpus in one showmap pass."""
    if not file_paths:
        return None
    try:
        tmp = tempfile.mkdtemp(prefix=".corpus_showmap_", dir=work_dir)
    except OSError:
        return None
    input_dir = os.path.join(tmp, "inputs")
    bitmap_path = os.path.join(tmp, "bitmap")
    try:
        os.makedirs(input_dir, exist_ok=True)
        for index, source in enumerate(file_paths):
            dest = os.path.join(input_dir, f"{index:06d}")
            try:
                os.link(source, dest)
            except OSError:
                try:
                    shutil.copy2(source, dest)
                except OSError:
                    if require_all:
                        return None
                    continue
        if not os.listdir(input_dir):
            return None
        cmd = [afl_showmap]
        if use_qemu:
            cmd.append("-Q")
        cmd.extend(
            [
                "-t",
                str(timeout_ms),
                "-m",
                "none",
                "-C",
                "-i",
                input_dir,
                "-o",
                bitmap_path,
                "-q",
                "--",
            ]
        )
        cmd.extend(target_cmd)
        completed = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=max(
                2.0,
                min(300.0, len(file_paths) * (timeout_ms / 1000.0 + 0.25) + 2.0),
            ),
            check=False,
        )
        if completed.returncode != 0:
            return None
        try:
            with open(bitmap_path, encoding="ascii") as stream:
                return list(
                    parse_sparse_edge_rows(
                        stream, map_size=StreamingShowmap.MAX_MAP_SIZE
                    )
                )
        except (OSError, UnicodeError, ValueError):
            return None
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def batch_showmap_edges(
    afl_showmap: str,
    target_cmd: "list[str]",
    file_paths: "list[str]",
    work_dir: str,
    timeout_ms: int = 5000,
    wall_timeout_seconds: float | None = None,
) -> "dict[str, list[tuple[int, int]]]":
    """批量 showmap:用 afl-showmap -I filelist 在【一次】C 侧 forkserver 循环里跑完所有输入。

    比逐个流式 get_edges(Python 每次管道往返，实测标称 ~600us/输入)快约一个数量级
    (afl-showmap -I 实测 ~25us/输入),因整个 forkserver 循环在 C 里跑、无 Python 逐次开销。
    返回 {文件路径: 稀疏边列表 [(edge_id, count)]};某输入超时/崩溃→其键缺失(调用方按 showmap_none 处理);
    afl-showmap 整体失败(不支持 -I/报错/无输出)→ 返回 {},调用方退回逐个流式。"""
    if not file_paths:
        return {}
    placeholder = os.environ.get("AFL_INPUT_PLACEHOLDER", "@@") or "@@"
    # AFL++ 4.40c -I cannot safely substitute a per-input target filename.
    # Returning {} makes the caller use status-preserving per-input handling.
    if any(placeholder in argument for argument in target_cmd):
        return {}
    # 自建专属临时目录(放 work_dir 下,多在 tmpfs)并在最后清理,避免污染/污读 output_dir
    try:
        tmp = tempfile.mkdtemp(prefix="_bsm_", dir=work_dir)
    except (OSError, IOError):
        return {}
    listf = os.path.join(tmp, "flist")
    mapdir = os.path.join(tmp, "maps")
    try:
        os.makedirs(mapdir, exist_ok=True)
        with open(listf, "w") as f:
            f.write("\n".join(file_paths) + "\n")
        cmd = [
            afl_showmap,
            "-I",
            listf,
            "-o",
            mapdir,
            "-t",
            str(timeout_ms),
            "-m",
            "none",
            "-q",
            "--",
        ]
        cmd.extend(target_cmd)
        # afl-showmap -I 内部逐输入处理并各自应用 -t 超时;整体给宽松上限,封顶 30min
        default_wall_timeout = min(1800.0, max(60.0, len(file_paths) + 30.0))
        wall_timeout = (
            default_wall_timeout
            if wall_timeout_seconds is None
            else min(default_wall_timeout, max(0.001, wall_timeout_seconds))
        )
        completed = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=wall_timeout,
            check=False,
        )
        if completed.returncode != 0:
            return {}
        # afl-showmap -I 以【输入文件名(basename)】命名各自 map;output_dir 内文件名唯一
        out: "dict[str, list[tuple[int, int]]]" = {}
        for p in file_paths:
            try:
                with open(
                    os.path.join(mapdir, os.path.basename(p)), encoding="ascii"
                ) as mf:
                    edges = list(
                        parse_sparse_edge_rows(
                            mf, map_size=StreamingShowmap.MAX_MAP_SIZE
                        )
                    )
            except (IOError, OSError, UnicodeError, ValueError):
                continue  # 无 map(超时/崩溃)→ 调用方视为 showmap_none
            out[p] = edges
        return out
    except (OSError, subprocess.SubprocessError):
        return {}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def one_shot_showmap_edges(
    afl_showmap: str,
    target_cmd: "list[str]",
    content: bytes,
    work_dir: str,
    timeout_ms: int = 5000,
    wall_timeout_seconds: float | None = None,
) -> "list[tuple[int, int]] | None":
    """Run one isolated afl-showmap query after streaming recovery fails."""
    try:
        tmp = tempfile.mkdtemp(prefix=".osm_", dir=work_dir)
    except OSError:
        return None
    bitmap_path = os.path.join(tmp, "map")
    try:
        placeholder = os.environ.get("AFL_INPUT_PLACEHOLDER", "@@") or "@@"
        uses_file = any(placeholder in argument for argument in target_cmd)
        input_path = os.path.join(tmp, "input")
        command_target = list(target_cmd)
        if uses_file:
            try:
                with open(input_path, "wb") as stream:
                    stream.write(content)
            except OSError:
                return None
            command_target = [
                argument.replace(placeholder, input_path) for argument in target_cmd
            ]
        cmd = [
            afl_showmap,
            "-o",
            bitmap_path,
            "-t",
            str(timeout_ms),
            "-m",
            "none",
            "-q",
            "--",
        ]
        cmd.extend(command_target)
        try:
            default_wall_timeout = max(10.0, timeout_ms / 1000.0 + 5.0)
            wall_timeout = (
                default_wall_timeout
                if wall_timeout_seconds is None
                else min(
                    default_wall_timeout,
                    max(0.001, wall_timeout_seconds),
                )
            )
            invocation = {
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "timeout": wall_timeout,
                "check": False,
            }
            if uses_file:
                completed = subprocess.run(cmd, stdin=subprocess.DEVNULL, **invocation)
            else:
                completed = subprocess.run(cmd, input=content, **invocation)
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        try:
            with open(bitmap_path, encoding="ascii") as stream:
                edges = list(
                    parse_sparse_edge_rows(
                        stream, map_size=StreamingShowmap.MAX_MAP_SIZE
                    )
                )
        except (OSError, UnicodeError, ValueError):
            return None
        return edges
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _sync_directory(directory: str) -> None:
    """Commit prior renames in one directory to stable storage."""
    try:
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    except (AttributeError, OSError):
        directory_fd = -1
    if directory_fd >= 0:
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def _acquire_master_queue_lock(path: str) -> typing.BinaryIO:
    """Hold the single-writer contract for one AFL-visible SymCC queue."""
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError(errno.EOPNOTSUPP, "O_NOFOLLOW is required for master lock")
    descriptor = os.open(path, flags | nofollow, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(errno.EINVAL, "master queue lock is not a regular file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                raise RuntimeError(
                    "another master already owns this SymCC queue"
                ) from error
            raise
        return os.fdopen(descriptor, "r+b")
    except BaseException:
        os.close(descriptor)
        raise


def _atomic_publish(
    path: str,
    content: bytes,
    *,
    sync_directory: bool = True,
) -> None:
    """Publish complete bytes without exposing an AFL-compatible temp name.

    ``sync_directory=False`` keeps the per-file data barrier but lets a caller
    group the namespace barrier for several same-batch publications.  The
    caller must invoke :func:`_sync_directory` before publishing any durable
    metadata that refers to those files.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, staged = tempfile.mkstemp(
        prefix=".symcc-publish-", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, path)
        staged = ""
        if sync_directory:
            _sync_directory(directory)
    finally:
        if fd >= 0:
            os.close(fd)
        if staged:
            try:
                os.unlink(staged)
            except OSError:
                pass


def _save_worker_raw_candidate(save_all_dir: str | None, content: bytes) -> bool:
    """Persist a raw SymCC candidate before coverage triage can block."""
    if not save_all_dir:
        return False
    digest = hashlib.sha256(content).hexdigest()
    dest = os.path.join(save_all_dir, digest)
    if os.path.exists(dest):
        return False
    try:
        _atomic_publish(dest, content)
        return True
    except OSError:
        return False


def _terminal_case_digests(directory: str) -> set[str]:
    digests: set[str] = set()
    try:
        names = os.listdir(directory)
    except OSError:
        return digests
    for name in names:
        marker = ",sha256:"
        if marker not in name:
            continue
        digest = name.rsplit(marker, 1)[1]
        if len(digest) == 64 and all(
            char in "0123456789abcdef" for char in digest
        ):
            digests.add(digest)
    return digests


def _batch_triage(
    batch_results: list[tuple],
    stats: "Stats",
    coverage: "CoverageBitmap",
    afl_config: "AflConfig",
    queue_dir: str,
    crashes_dir: str,
    hangs_dir: str,
    afl_sync_queue: str | None,
    save_all_dir: str | None,
    symcc_dir: str,
    bitmap_path_triage: str,
    symcc_feedback_queue: list[tuple[str, int]],
    queue_id_ref: list[int],
    crash_id_ref: list[int] | None = None,
    hang_id_ref: list[int] | None = None,
    file_generation: dict[str, int] | None = None,
    afl_extras_dir: str | None = None,
    hint_id_ref: list[int] | None = None,
    recent_byte_offsets: list[int] | None = None,
    focus_bytes_window: int = 200,
    yield_callback: "typing.Callable[[str, bool], None] | None" = None,
    observation_callback: "typing.Callable[[str, int, int, float, bool, int, SolverTelemetry | None, int, tuple[tuple[int, str], ...], str, dict[str, str]], None] | None" = None,
    analyzed_hashes_ref: "set[str] | None" = None,
    analyzed_hash_callback: "typing.Callable[[str], None] | None" = None,
    coverage_claim_callback: "typing.Callable[[bytes | list[tuple[int, int]]], tuple[int, bool]] | None" = None,
    coverage_claim_many_callback: "typing.Callable[[list[bytes | list[tuple[int, int]]]], tuple[list[int], bool]] | None" = None,
    proposal_retention_callback: "typing.Callable[[str, int], None] | None" = None,
    topseed_observation_callback: "typing.Callable[[int, str, tuple[int, ...], SolverTelemetry | None, bool, int], None] | None" = None,
    topseed_feature_limit: int = 65_536,
    agentic_observation_callback: "typing.Callable[[int, int, int], None] | None" = None,
    triage_profile: dict[str, float] | None = None,
    crash_digests_ref: set[str] | None = None,
    hang_digests_ref: set[str] | None = None,
    result_object_store: ContentAddressedInputStore | None = None,
    result_object_max_bytes: int = _MAX_HYBRID_RESULT_BYTES,
) -> bool:
    """批量 triage worker 返回的结果。返回 bitmap 是否有变化。"""
    batch_started = time.monotonic() if triage_profile is not None else 0.0
    queue_id = queue_id_ref[0]
    crash_id = crash_id_ref[0] if crash_id_ref is not None else 0
    hang_id = hang_id_ref[0] if hang_id_ref is not None else 0
    bitmap_changed = False
    topseed_feature_limit = max(1, min(1_000_000, int(topseed_feature_limit)))
    # A local coverage merge is memory-only until the caller journals the
    # returned delta, so all queue renames in one natural result batch can use
    # one directory durability barrier.  A distributed coverage claim may be
    # durable immediately; retain per-candidate directory barriers there so a
    # claim can never outlive its corresponding queue entry.
    group_directory_commits = (
        coverage_claim_callback is None or coverage_claim_many_callback is not None
    )
    deferred_commit_dirs: set[str] = set()

    crash_digests = (
        crash_digests_ref
        if crash_digests_ref is not None
        else _terminal_case_digests(crashes_dir)
    )
    hang_digests = (
        hang_digests_ref
        if hang_digests_ref is not None
        else _terminal_case_digests(hangs_dir)
    )

    for (
        worker_rank,
        input_path,
        new_tests,
        retcode,
        elapsed,
        killed,
        strategy,
        telemetry,
        s2f_actions,
        parameter_token,
        parameter_overrides,
    ) in batch_results:
        input_produced_interesting = False
        input_coverage_delta = 0
        input_interesting_cases = 0
        input_generated_features: set[int] = set()
        # 跳过 worker 端"非执行"错误结果（文件缺失/派发前异常：elapsed==0 且
        # retcode==-1 且无输出），否则会以 0 耗时的"成功执行"稀释平均耗时统计。
        # 注：run_symcc_worker 内部异常虽也置 retcode=-1，但 elapsed 已计量（>0）。
        if not (elapsed == 0 and retcode == -1 and not new_tests):
            stats.add_execution(elapsed, killed)
        # new_tests 现在只含 interesting 的 TC（worker 端已做 dedup）

        # In multi-master mode, stage locally novel candidates outside AFL's
        # visible queue, then persist the owner decision before promoting only
        # the winners. The transaction store conservatively recovers prepared
        # batches after an ambiguous crash, so a coverage claim cannot outlive
        # its corresponding corpus bytes.
        batched_claims: dict[int, dict[str, typing.Any]] = {}
        if coverage_claim_many_callback is not None and new_tests:
            claim_records: list[dict[str, typing.Any]] = []
            preview_bits: dict[int, int] = {}
            for tc in new_tests:
                if tc.get("terminal_status") is not None:
                    continue
                try:
                    content = _hybrid_candidate_content(
                        tc,
                        result_object_store,
                        max_bytes=result_object_max_bytes,
                    )
                except (OSError, ValueError):
                    continue
                content_hash = hashlib.sha256(content).hexdigest()
                bitmap_data = tc.get("bitmap")
                result_type = "success" if bitmap_data is not None else "error"
                if bitmap_data is None:
                    tc_path = os.path.join(
                        symcc_dir, f".tc_{content_hash[:16]}"
                    )
                    try:
                        with open(tc_path, "wb") as stream:
                            stream.write(content)
                        result_type, bitmap_data = afl_config.run_showmap(
                            tc_path, bitmap_path_triage
                        )
                    finally:
                        try:
                            os.unlink(tc_path)
                        except OSError:
                            pass
                tc["_triage_hash"] = content_hash
                tc["_triage_result_type"] = result_type
                tc["_triage_bitmap"] = bitmap_data
                if (
                    result_type != "success"
                    or not bitmap_data
                    or _preview_coverage_delta(
                        coverage, bitmap_data, preview_bits
                    ) <= 0
                ):
                    continue
                orig_name = os.path.basename(input_path)
                src_id = _afl_source_id(orig_name)
                record = {
                    "bitmap": bitmap_data,
                    "content": content,
                    "src_id": src_id,
                }
                claim_records.append(record)
                batched_claims[id(tc)] = record
            if claim_records:
                if queue_id > _MAX_AFL_ARTIFACT_ID - len(claim_records) + 1:
                    raise RuntimeError("AFL queue ID space is exhausted")
                transaction_store = CoverageQueueTransactionStore(
                    symcc_dir, queue_dir
                )
                transaction_id = transaction_store.prepare([
                    (record["content"], str(record["src_id"]))
                    for record in claim_records
                ])
                profile_started = (
                    time.monotonic() if triage_profile is not None else 0.0
                )
                claim_deltas, local_changed = coverage_claim_many_callback(
                    [record["bitmap"] for record in claim_records]
                )
                bitmap_changed = bitmap_changed or local_changed
                if len(claim_deltas) != len(claim_records):
                    raise RuntimeError("coverage claim batch length mismatch")
                for record, coverage_delta in zip(claim_records, claim_deltas):
                    record["coverage_delta"] = coverage_delta
                transaction_store.decide(
                    transaction_id,
                    claim_deltas,
                    first_queue_id=queue_id,
                )
                committed_queue = transaction_store.commit(transaction_id)
                # The queue namespace is already durable. Publish its next ID
                # immediately so a later callback failure in this batch cannot
                # make the same process reuse a committed AFL queue slot.
                queue_id_ref[0] = max(
                    queue_id_ref[0], committed_queue.next_queue_id
                )
                for record, destination in zip(
                    claim_records, committed_queue.destinations
                ):
                    record["retained"] = destination is not None
                    if destination is not None:
                        record["dest"] = destination
                        destination_id = _afl_artifact_id(
                            os.path.basename(destination)
                        )
                        if destination_id is None:
                            raise RuntimeError(
                                "coverage queue transaction returned invalid ID"
                            )
                        record["queue_id"] = destination_id
                redundant_entries = committed_queue.redundant
                if triage_profile is not None:
                    triage_profile["redundant_queue_entries_removed"] = (
                        triage_profile.get(
                            "redundant_queue_entries_removed", 0.0,
                        )
                        + redundant_entries
                    )
                if triage_profile is not None:
                    triage_profile["coverage_claim"] = (
                        triage_profile.get("coverage_claim", 0.0)
                        + time.monotonic() - profile_started
                    )

        for tc in new_tests:
            batched_claim = batched_claims.get(id(tc))
            if batched_claim is not None:
                tc_content = typing.cast(bytes, batched_claim["content"])
            else:
                try:
                    tc_content = _hybrid_candidate_content(
                        tc,
                        result_object_store,
                        max_bytes=result_object_max_bytes,
                    )
                except (OSError, ValueError):
                    continue
            tc_bitmap = tc.get("bitmap")  # 稀疏边列表 [(edge_id, count)]
            proposal_id = str(tc.get("proposal_id", "") or "")
            coverage_delta = 0
            # 内容哈希只算一次，供 save-all / 回退临时名 / AFL-sync 去重复用
            # （避免对同一内容重复 SHA-256 2~3 次）。
            _tc_hash = tc.pop(
                "_triage_hash", hashlib.sha256(tc_content).hexdigest()
            )

            # --save-all
            if save_all_dir is not None:
                profile_started = (
                    time.monotonic() if triage_profile is not None else 0.0
                )
                _save_worker_raw_candidate(save_all_dir, tc_content)
                if triage_profile is not None:
                    triage_profile["save_all"] = (
                        triage_profile.get("save_all", 0.0)
                        + time.monotonic() - profile_started
                    )

            terminal_status = tc.get("terminal_status")
            if terminal_status is not None:
                orig_name = os.path.basename(input_path)
                src_id = _afl_source_id(orig_name)
                if terminal_status == "crash":
                    terminal_dir = crashes_dir
                    terminal_id = crash_id
                    known_digests = crash_digests
                else:
                    terminal_dir = hangs_dir
                    terminal_id = hang_id
                    known_digests = hang_digests
                if _tc_hash not in known_digests:
                    if terminal_id > _MAX_AFL_ARTIFACT_ID:
                        raise RuntimeError(
                            f"AFL {terminal_status} ID space is exhausted"
                        )
                    terminal_name = (
                        f"id:{terminal_id:06d},src:{src_id},sha256:{_tc_hash}"
                    )
                    try:
                        _atomic_publish(
                            os.path.join(terminal_dir, terminal_name), tc_content
                        )
                    except OSError:
                        pass
                    else:
                        known_digests.add(_tc_hash)
                        if terminal_status == "crash":
                            crash_id += 1
                            stats.generated_crashes += 1
                        else:
                            hang_id += 1
                            stats.generated_hangs += 1
                if analyzed_hashes_ref is not None:
                    analyzed_hashes_ref.add(_tc_hash)
                if analyzed_hash_callback is not None:
                    analyzed_hash_callback(_tc_hash)
                if proposal_id and proposal_retention_callback is not None:
                    proposal_retention_callback(proposal_id, 0)
                continue

            # Triage：优先用 worker 端的稀疏边列表
            cached_result_type = tc.pop("_triage_result_type", None)
            cached_bitmap = tc.pop("_triage_bitmap", None)
            if cached_result_type is not None:
                result_type, bitmap_data = cached_result_type, cached_bitmap
            elif tc_bitmap is not None:
                bitmap_data = tc_bitmap
                result_type = "success"
            else:
                # 回退：写临时文件并运行 showmap
                tc_id = _tc_hash[:16]
                tc_path = os.path.join(symcc_dir, f".tc_{tc_id}")
                with open(tc_path, "wb") as f:
                    f.write(tc_content)
                result_type, bitmap_data = afl_config.run_showmap(
                    tc_path, bitmap_path_triage
                )
                try:
                    os.unlink(tc_path)
                except OSError:
                    pass

            if (
                batched_claim is not None
                and bool(batched_claim.get("retained"))
            ) or (
                batched_claim is None
                and result_type == "success"
                and bitmap_data
                and coverage.count_delta(bitmap_data) > 0
            ):
                if queue_id > _MAX_AFL_ARTIFACT_ID:
                    raise RuntimeError("AFL queue ID space is exhausted")
                orig_name = os.path.basename(input_path)
                src_id = _afl_source_id(orig_name)
                new_name = f"id:{queue_id:06d},src:{src_id}"
                dest = os.path.join(queue_dir, new_name)
                if batched_claim is not None:
                    src_id = str(batched_claim["src_id"])
                    dest = str(batched_claim["dest"])
                    new_name = os.path.basename(dest)

                # Persist a complete, AFL-visible queue entry before committing
                # its coverage bits. The staging name is hidden and never starts
                # with id:, so AFL's nine-byte peer cursor cannot consume it.
                if batched_claim is None:
                    try:
                        profile_started = (
                            time.monotonic() if triage_profile is not None else 0.0
                        )
                        _atomic_publish(
                            dest,
                            tc_content,
                            sync_directory=not group_directory_commits,
                        )
                        if group_directory_commits:
                            deferred_commit_dirs.add(queue_dir)
                        if triage_profile is not None:
                            triage_profile["queue_publish"] = (
                                triage_profile.get("queue_publish", 0.0)
                                + time.monotonic() - profile_started
                            )
                    except OSError as _e:
                        print(f"[Master] 队列写入失败，跳过 {new_name}: {_e}", flush=True)
                        continue

                if batched_claim is not None:
                    coverage_delta = int(batched_claim["coverage_delta"])
                elif coverage_claim_callback is not None:
                    profile_started = (
                        time.monotonic() if triage_profile is not None else 0.0
                    )
                    coverage_delta, local_changed = coverage_claim_callback(bitmap_data)
                    bitmap_changed = bitmap_changed or local_changed
                    if triage_profile is not None:
                        triage_profile["coverage_claim"] = (
                            triage_profile.get("coverage_claim", 0.0)
                            + time.monotonic() - profile_started
                        )
                else:
                    profile_started = (
                        time.monotonic() if triage_profile is not None else 0.0
                    )
                    coverage_delta = coverage.merge_delta(bitmap_data)
                    if triage_profile is not None:
                        triage_profile["coverage_merge"] = (
                            triage_profile.get("coverage_merge", 0.0)
                            + time.monotonic() - profile_started
                        )

                if coverage_delta:
                    bitmap_changed = True
                    input_produced_interesting = True
                    input_coverage_delta += coverage_delta
                    input_interesting_cases += 1
                    # 计算迭代代数：输入的代数 + 1
                    parent_gen = 0
                    if file_generation is not None:
                        parent_gen = file_generation.get(input_path, 0)
                    child_gen = parent_gen + 1
                    if file_generation is not None:
                        file_generation[dest] = child_gen
                    # 深度限制检查
                    if MAX_GENERATION_DEPTH <= 0 or child_gen <= MAX_GENERATION_DEPTH:
                        symcc_feedback_queue.append((dest, child_gen))
                    if afl_sync_queue and os.path.isdir(afl_sync_queue):
                        try:
                            _sdest = os.path.join(
                                afl_sync_queue, f"id:{queue_id:06d},src:{src_id}"
                            )
                            _atomic_publish(
                                _sdest,
                                tc_content,
                                sync_directory=not group_directory_commits,
                            )
                            if group_directory_commits:
                                deferred_commit_dirs.add(afl_sync_queue)
                        except OSError:
                            pass
                    stats.interesting_count += 1

                    # 将约束 hint 写入 AFL extras 目录（多字节聚合）
                    # 将连续偏移的 hint 聚合为多字节 token，
                    # AFL extras 期望多字节 token（如 "SELECT"）而非单字节
                    tc_hints = tc.get("hints")
                    if tc_hints and afl_extras_dir and hint_id_ref is not None:
                        sorted_hints = sorted(tc_hints, key=lambda h: h[0])
                        tokens: list[bytes] = []
                        cur_token = bytearray()
                        prev_off = -2
                        for _off, _old, _new in sorted_hints:
                            if _off == prev_off + 1:
                                cur_token.append(_new)
                            else:
                                if cur_token:
                                    tokens.append(bytes(cur_token))
                                cur_token = bytearray([_new])
                            prev_off = _off
                        if cur_token:
                            tokens.append(bytes(cur_token))
                        for token in tokens:
                            # 循环复用固定文件池，上限 MAX_HINT_FILES，避免 inode 耗尽
                            hint_path = os.path.join(
                                afl_extras_dir,
                                f"hint_{hint_id_ref[0] % MAX_HINT_FILES:06d}",
                            )
                            try:
                                with open(hint_path, "wb") as hf:
                                    hf.write(token)
                                hint_id_ref[0] += 1
                            except OSError:
                                pass

                    # 仅从 interesting TC 收集偏移用于 focus_bytes
                    if tc_hints and recent_byte_offsets is not None:
                        for _off, _old, _new in tc_hints:
                            recent_byte_offsets.append(_off)
                        # 滑动窗口：只保留最近的偏移
                        if len(recent_byte_offsets) > focus_bytes_window:
                            del recent_byte_offsets[:-focus_bytes_window]
                # Every published child was produced by this symbolic run.
                # Register it even when another coordinator won the global
                # claim, otherwise the local AFL scan may schedule it for
                # symbolic execution again.
                if analyzed_hashes_ref is not None:
                    analyzed_hashes_ref.add(_tc_hash)
                if analyzed_hash_callback is not None:
                    analyzed_hash_callback(_tc_hash)
                queue_id += 1
            elif batched_claim is not None:
                # The candidate was produced locally but another coordinator
                # won the global coverage claim. Its staged queue entry was
                # removed before becoming AFL-visible; retaining the digest prevents local
                # symbolic rediscovery if the same bytes arrive via AFL sync.
                if analyzed_hashes_ref is not None:
                    analyzed_hashes_ref.add(_tc_hash)
                if analyzed_hash_callback is not None:
                    analyzed_hash_callback(_tc_hash)
            if proposal_id and proposal_retention_callback is not None:
                profile_started = (
                    time.monotonic() if triage_profile is not None else 0.0
                )
                proposal_retention_callback(proposal_id, coverage_delta)
                if triage_profile is not None:
                    triage_profile["proposal_retention"] = (
                        triage_profile.get("proposal_retention", 0.0)
                        + time.monotonic() - profile_started
                    )
            if (
                topseed_observation_callback is not None
                and result_type == "success"
                and bitmap_data
            ):
                remaining = max(
                    0, topseed_feature_limit - len(input_generated_features)
                )
                if remaining:
                    input_generated_features.update(
                        TopSeedSelector.coverage_features_from_bitmap(
                            bitmap_data, maximum=remaining
                        )
                    )

        if recent_byte_offsets is not None:
            recent_byte_offsets.extend(_comparison_taint_offsets(telemetry))
            if len(recent_byte_offsets) > focus_bytes_window:
                del recent_byte_offsets[:-focus_bytes_window]

        # 更新种子类型产出率
        if yield_callback is not None:
            profile_started = (
                time.monotonic() if triage_profile is not None else 0.0
            )
            yield_callback(input_path, input_produced_interesting)
            if triage_profile is not None:
                triage_profile["yield_callback"] = (
                    triage_profile.get("yield_callback", 0.0)
                    + time.monotonic() - profile_started
                )
        if observation_callback is not None:
            profile_started = (
                time.monotonic() if triage_profile is not None else 0.0
            )
            observation_callback(
                input_path,
                input_coverage_delta,
                input_interesting_cases,
                elapsed,
                killed,
                strategy,
                telemetry,
                worker_rank,
                s2f_actions,
                parameter_token,
                parameter_overrides,
            )
            if triage_profile is not None:
                triage_profile["observation_callback"] = (
                    triage_profile.get("observation_callback", 0.0)
                    + time.monotonic() - profile_started
                )
        if topseed_observation_callback is not None:
            profile_started = (
                time.monotonic() if triage_profile is not None else 0.0
            )
            topseed_observation_callback(
                worker_rank,
                input_path,
                tuple(sorted(input_generated_features)),
                telemetry,
                killed,
                retcode,
            )
            if triage_profile is not None:
                triage_profile["topseed_callback"] = (
                    triage_profile.get("topseed_callback", 0.0)
                    + time.monotonic() - profile_started
                )
        if agentic_observation_callback is not None:
            agentic_observation_callback(
                worker_rank, input_coverage_delta, input_interesting_cases)

        if killed:
            if hang_id > _MAX_AFL_ARTIFACT_ID:
                raise RuntimeError("AFL hang ID space is exhausted")
            orig_name = os.path.basename(input_path)
            src_id = _afl_source_id(orig_name)
            hang_name = f"id:{hang_id:06d},src:{src_id}"
            try:
                _hdest = os.path.join(hangs_dir, hang_name)
                shutil.copy2(input_path, _hdest + ".tmp")
                os.replace(_hdest + ".tmp", _hdest)  # 原子落盘，避免外部观察者读到半截
                hang_id += 1
            except (IOError, OSError):
                pass
        elif _returncode_indicates_crash(retcode, killed):
            # 目标在 timeout 包装下被致命信号终止（SIGSEGV=139/SIGABRT=134/
            # SIGFPE=136 等；排除超时的 SIGKILL=137，那已由 killed 归入 hangs）
            # → 保存触发崩溃的输入供分析，否则 concolic 发现的崩溃种子被静默丢弃。
            if crash_id > _MAX_AFL_ARTIFACT_ID:
                raise RuntimeError("AFL crash ID space is exhausted")
            orig_name = os.path.basename(input_path)
            src_id = _afl_source_id(orig_name)
            crash_name = f"id:{crash_id:06d},src:{src_id}"
            try:
                _cdest = os.path.join(crashes_dir, crash_name)
                shutil.copy2(input_path, _cdest + ".tmp")
                os.replace(_cdest + ".tmp", _cdest)  # 原子落盘
                crash_id += 1
            except (IOError, OSError):
                pass

    # This barrier precedes the caller's bitmap journal update.  A failure is
    # intentionally fatal to this batch: continuing with an in-memory coverage
    # claim after an uncommitted rename could suppress rediscovery on the same
    # process lifetime.
    for directory in sorted(deferred_commit_dirs):
        profile_started = (
            time.monotonic() if triage_profile is not None else 0.0
        )
        _sync_directory(directory)
        if triage_profile is not None:
            triage_profile["directory_commit"] = (
                triage_profile.get("directory_commit", 0.0)
                + time.monotonic() - profile_started
            )

    queue_id_ref[0] = queue_id
    if crash_id_ref is not None:
        crash_id_ref[0] = crash_id
    if hang_id_ref is not None:
        hang_id_ref[0] = hang_id
    # 注：不再每批 print 三元组统计（热路径去除 f-string 格式化 + stdout I/O，
    # 与 mpi_concolic_execution 的 master 修复一致）；进度由 master 循环中每 2s 的
    # 轻量汇总行 + 周期性完整 Stats 行输出，聚合计数走全局 stats。
    if triage_profile is not None:
        triage_profile["batch_core"] = (
            triage_profile.get("batch_core", 0.0)
            + time.monotonic() - batch_started
        )
    return bitmap_changed


def _admit_hybrid_master_input(
    object_store: ContentAddressedInputStore,
    file_cache: dict[str, dict],
    input_path: str,
) -> tuple[str, str, bytes]:
    """Bind a dispatch to the exact bounded bytes selected from the AFL queue."""
    object_id, stored_path, content = object_store.import_path(input_path)
    cached = file_cache.get(input_path)
    if cached is not None and cached.get("hash") != object_id:
        file_cache.pop(input_path, None)
        raise ValueError("input changed after queue scoring")
    return object_id, stored_path, content


def _admit_hybrid_master_work(
    object_store: ContentAddressedInputStore,
    file_cache: dict[str, dict],
    input_path: str,
    continuation: dict | None,
) -> tuple[str | None, str, bytes, int]:
    """Admit a byte input or identify one self-contained continuation task."""
    if continuation is not None:
        descriptor = LiveContinuationDescriptor.from_mapping(continuation)
        if descriptor is None:
            raise ValueError("invalid live continuation descriptor")
        return None, descriptor.checkpoint_id(), b"", 0
    object_id, _stored_path, content = _admit_hybrid_master_input(
        object_store,
        file_cache,
        input_path,
    )
    return object_id, object_id, content, len(content)


def _materialize_hybrid_worker_input(
    object_store: ContentAddressedInputStore,
    message: dict,
    input_path: str,
) -> tuple[str, str]:
    """Materialize exactly the digest admitted by the master."""
    expected = message.get("sha256")
    if (
        not isinstance(expected, str)
        or len(expected) != 64
        or any(char not in "0123456789abcdef" for char in expected)
    ):
        raise ValueError("hybrid input is missing a valid SHA-256 fence")

    object_id = message.get("object_id")
    if object_id is not None:
        if object_id != expected:
            raise ValueError("hybrid input object id does not match its fence")
        content = message.get("object_content")
        if content is not None and not isinstance(content, bytes):
            raise ValueError("invalid input object payload")
        return object_store.materialize(object_id, content), object_id

    observed, path, _content = object_store.import_path(input_path)
    if observed != expected:
        raise ValueError("path input changed after master admission")
    return path, observed


def _acknowledge_worker_input_object(
    worker_objects: dict[int, set[str]],
    worker_rank: int,
    expected: str,
    observed: object,
) -> bool:
    """Record worker CAS residency only after an exact result acknowledgement."""
    valid_expected = (
        bool(expected)
        and len(expected) == 64
        and not any(char not in "0123456789abcdef" for char in expected)
    )
    if not valid_expected:
        return False
    if observed != expected:
        worker_objects.setdefault(worker_rank, set()).discard(expected)
        return False
    cached = worker_objects.setdefault(worker_rank, set())
    if expected not in cached and len(cached) >= _WORKER_OBJECT_CACHE_CAP:
        # Residency is an optimization hint.  Gradual eviction avoids a burst
        # in which the coordinator retransmits every previously acknowledged
        # CAS object after one hard-limit crossing.
        _trim_tracking_container(
            cached, _WORKER_OBJECT_CACHE_CAP - 1, retain_ratio=0.875
        )
    cached.add(expected)
    return True


def master(comm: "MPI.Intracomm", args: argparse.Namespace) -> bool:
    """Master process: monitors AFL queue, distributes work, triages results."""
    size = comm.Get_size()
    num_workers = size - 1
    shutdown_grace_default = float(max(120, TIMEOUT_SEC * 4))
    shutdown_grace_sec = _bounded_mpi_timeout(
        os.environ.get(
            "SYMCC_SHUTDOWN_GRACE_SEC",
            str(shutdown_grace_default),
        ),
        shutdown_grace_default,
    )

    if num_workers == 0:
        print("Error: need at least 2 MPI processes.", file=sys.stderr)
        return False

    # Setup
    afl_queue_dir = os.path.join(args.output_dir, args.fuzzer_name)
    symcc_dir = os.path.join(args.output_dir, args.name)
    resume_enabled = os.environ.get("SYMCC_RESUME", "0").lower() not in {
        "0",
        "false",
        "off",
        "no",
    }

    if os.path.exists(symcc_dir):
        if not resume_enabled:
            print(
                f"Error: {symcc_dir} already exists. "
                f"Set SYMCC_RESUME=1 to recover unfinished work.",
                file=sys.stderr,
            )
            shutdown = _cooperative_shutdown_workers(
                comm,
                range(1, size),
                grace=shutdown_grace_sec,
            )
            return False
        print(f"[Master] Resuming existing SymCC output: {symcc_dir}")
    else:
        os.makedirs(symcc_dir)
    queue_dir = os.path.join(symcc_dir, "queue")
    hangs_dir = os.path.join(symcc_dir, "hangs")
    crashes_dir = os.path.join(symcc_dir, "crashes")
    os.makedirs(queue_dir, exist_ok=True)
    os.makedirs(hangs_dir, exist_ok=True)
    os.makedirs(crashes_dir, exist_ok=True)
    try:
        master_queue_lock = _acquire_master_queue_lock(
            os.path.join(symcc_dir, ".master-queue.lock")
        )
    except (OSError, RuntimeError) as error:
        print(f"Error: cannot own {queue_dir}: {error}", file=sys.stderr)
        shutdown = _cooperative_shutdown_workers(
            comm,
            range(1, size),
            grace=shutdown_grace_sec,
        )
        return False
    known_crash_digests = _terminal_case_digests(crashes_dir)
    known_hang_digests = _terminal_case_digests(hangs_dir)

    shared_fs_probe_enabled = os.environ.get(
        "SYMCC_SHARED_STATE_FS_PROBE", "1"
    ).lower() not in {"0", "false", "off", "no"}
    shared_fs_probe_timeout = _bounded_finite_float(
        os.environ.get("SYMCC_SHARED_STATE_FS_PROBE_TIMEOUT", "5"),
        default=5.0,
        minimum=0.001,
        maximum=60.0,
    )
    probed_shared_roots: dict[str, list[typing.Any]] = {}

    def _probe_shared_root(
        path: str,
        requirements: SharedFilesystemRequirementProfile,
    ):
        if not shared_fs_probe_enabled:
            return None
        key = os.path.realpath(os.path.abspath(path))
        required = set(requirements.required_operations)
        for capability in probed_shared_roots.get(key, ()):
            if required.issubset(capability.required_operations):
                return capability
        capability = probe_shared_state_filesystem(
            key,
            timeout=shared_fs_probe_timeout,
            requirements=requirements,
        )
        probed_shared_roots.setdefault(key, []).append(capability)
        print(
            f"[Master] Shared filesystem capabilities: {capability.snapshot()}",
            flush=True,
        )
        return capability

    try:
        for shared_root, requirements in _shared_filesystem_preflight_contracts(
            symcc_dir
        ):
            _probe_shared_root(shared_root, requirements)
    except (OSError, RuntimeError, ValueError) as error:
        print(
            "[Master] Shared filesystem capability failure before service "
            f"startup: {error}",
            file=sys.stderr,
            flush=True,
        )
        shutdown = _cooperative_shutdown_workers(
            comm,
            range(1, size),
            grace=shutdown_grace_sec,
        )
        print(
            "[Master] Preflight shutdown: "
            f"acked={len(shutdown['acknowledged'])}/{num_workers} "
            f"pending={list(shutdown['pending'])} "
            f"elapsed={shutdown['elapsed']:.3f}s",
            flush=True,
        )
        master_queue_lock.close()
        # main() converts False into MPI Abort(70), preserving a non-zero
        # configuration-failure result even when every worker acknowledged.
        return False

    # Admit the AFL campaign before constructing executors, policies, or an
    # asynchronous query-service command.  A malformed campaign is a
    # configuration failure even when every worker acknowledges shutdown; it
    # must not be reported as a successful MPI run or retain the queue lock.
    try:
        afl_config = AflConfig(afl_queue_dir)
    except (OSError, RuntimeError, ValueError, IndexError) as error:
        print(f"Error loading AFL config: {error}", file=sys.stderr)
        shutdown = _cooperative_shutdown_workers(
            comm,
            range(1, size),
            grace=shutdown_grace_sec,
        )
        master_queue_lock.close()
        return False

    query_service_process = None
    query_service_log = None
    query_service_command: list[str] | None = None
    query_candidate_dir = os.path.join(symcc_dir, ".query_candidates")
    query_candidate_seen: set[str] = set()
    last_query_candidate_scan = 0.0
    query_store = os.environ.get(
        "SYMCC_QUERY_STORE", os.path.join(symcc_dir, ".query_store")
    )
    try:
        async_query_workers = max(
            0, int(os.environ.get("SYMCC_ASYNC_QUERY_WORKERS", "0"))
        )
    except ValueError:
        async_query_workers = 0
    query_compute_slots = min(256, async_query_workers)
    if query_compute_slots:
        query_spool = os.environ.get(
            "SYMCC_QUERY_SPOOL", os.path.join(symcc_dir, ".query_spool")
        )
        os.makedirs(query_candidate_dir, exist_ok=True)
        service = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "symcc_query_service.py"
        )
        command = [
            sys.executable,
            service,
            "--store",
            query_store,
            "--spool",
            query_spool,
            "--jobs",
            str(query_compute_slots),
            "--lease-seconds",
            str(max(60, TIMEOUT_SEC * 2)),
            "--traversal",
            os.environ.get("SYMCC_QUERY_TRAVERSAL", "structural"),
        ]
        schedule_constraints = os.environ.get(
            "SYMCC_SCHEDULE_CONSTRAINT_OUT", ""
        ).strip()
        if schedule_constraints:
            command.extend(["--schedule-constraints", schedule_constraints])
            schedule_validation_out = os.environ.get(
                "SYMCC_SCHEDULE_QUERY_VALIDATION_OUT", ""
            ).strip()
            if schedule_validation_out:
                command.extend(
                    [
                        "--schedule-validation-out",
                        schedule_validation_out,
                    ]
                )
        configured_solver = os.environ.get("SYMCC_QUERY_SOLVER", "")
        if configured_solver:
            command.extend(["--solver", configured_solver])
        # Starting this process before the remainder of master initialization
        # made a later setup exception leak a live service.  Keep the validated
        # command inert until every coordinator component is ready.
        query_service_command = command
    max_object_bytes = _bounded_env_int(
        os.environ,
        "SYMCC_MAX_TRANSPORT_INPUT",
        16 * 1024 * 1024,
        1,
        _MAX_HYBRID_RESULT_BYTES,
    )
    master_result_max_objects = _bounded_env_int(
        os.environ,
        "SYMCC_HYBRID_RESULT_MAX_OBJECTS",
        _DEFAULT_HYBRID_RESULT_MAX_OBJECTS,
        1,
        _MAX_HYBRID_RESULT_OBJECTS,
    )
    master_result_max_bytes = _bounded_env_int(
        os.environ,
        "SYMCC_HYBRID_RESULT_MAX_BYTES",
        _DEFAULT_HYBRID_RESULT_MAX_BYTES,
        1,
        _MAX_HYBRID_RESULT_BYTES,
    )
    master_result_max_hints = _bounded_env_int(
        os.environ,
        "SYMCC_HYBRID_RESULT_MAX_HINTS",
        _DEFAULT_HYBRID_RESULT_MAX_HINTS,
        1,
        _MAX_HYBRID_RESULT_HINTS,
    )
    master_timeout_sites_max = _bounded_env_int(
        os.environ,
        "SYMCC_TIMEOUT_SITES_MAX",
        _DEFAULT_TIMEOUT_SITES_MAX,
        1,
        _MAX_TIMEOUT_SITES,
    )
    master_schedule_trace_max_bytes = _bounded_env_int(
        os.environ,
        "SYMCC_SCHEDULE_TRACE_MAX",
        _DEFAULT_SCHEDULE_TRACE_MAX_BYTES,
        1,
        _MAX_SCHEDULE_TRACE_MAX_BYTES,
    )
    master_result_max_object_bytes = min(
        max_object_bytes,
        master_result_max_bytes,
    )
    result_admission_jobs = _bounded_env_int(
        os.environ,
        "SYMCC_MASTER_ADMISSION_JOBS",
        1,
        1,
        8,
    )
    result_admission_capacity = _bounded_env_int(
        os.environ,
        "SYMCC_MASTER_ADMISSION_CAPACITY",
        max(16, result_admission_jobs * 8),
        result_admission_jobs,
        4096,
    )
    result_object_store = ContentAddressedInputStore(
        os.path.join(symcc_dir, ".result_objects"),
        master_result_max_object_bytes,
    )
    result_admission = _HybridResultAdmissionService(
        max_workers=result_admission_jobs,
        capacity=result_admission_capacity,
        max_objects=master_result_max_objects,
        max_bytes=master_result_max_bytes,
        max_object_bytes=master_result_max_object_bytes,
        max_hints=master_result_max_hints,
        max_timeout_sites=master_timeout_sites_max,
        max_schedule_trace_bytes=master_schedule_trace_max_bytes,
        result_object_store=result_object_store,
    )
    result_object_gc_interval = _bounded_env_float(
        os.environ,
        "SYMCC_RESULT_OBJECT_GC_INTERVAL_SEC",
        30.0,
        0.0,
        3600.0,
    )
    result_object_gc_grace = _bounded_env_float(
        os.environ,
        "SYMCC_RESULT_OBJECT_GC_GRACE_SEC",
        300.0,
        0.0,
        30 * 24 * 3600.0,
    )
    result_object_gc_max_entries = _bounded_env_int(
        os.environ,
        "SYMCC_RESULT_OBJECT_GC_MAX_ENTRIES",
        4096,
        1,
        1_000_000,
    )
    object_store = ContentAddressedInputStore(
        os.path.join(symcc_dir, ".objects"), max_object_bytes
    )
    object_transport_enabled = os.environ.get("SYMCC_OBJECT_TRANSPORT", "1") != "0"
    bitmap_deltas_enabled = os.environ.get("SYMCC_BITMAP_DELTAS", "1") != "0"

    adaptive_scheduler_enabled = os.environ.get("SYMCC_ADAPTIVE_SCHEDULER", "1") != "0"
    adaptive_policy = (
        AdaptiveHybridScheduler(
            len(SYMCC_STRATEGY_PROFILES),
            os.path.join(symcc_dir, ".scheduler_state.json"),
        )
        if adaptive_scheduler_enabled
        else None
    )
    topseed_enabled = adaptive_scheduler_enabled and os.environ.get(
        "SYMCC_TOPSEED", "1"
    ).lower() not in {"0", "false", "off", "no"}
    topseed_state_path = os.path.join(symcc_dir, ".topseed_state.json")
    topseed_program_context = hashlib.sha256(
        _self_config_program_key(args.target).encode("utf-8")
    ).hexdigest()

    def _new_topseed_selector() -> TopSeedSelector:
        return TopSeedSelector(
            program_context=topseed_program_context,
            seed=_bounded_env_int(
                os.environ, "SYMCC_TOPSEED_SEED", 0, 0, (1 << 63) - 1
            ),
            explore_ratio=_bounded_finite_float(
                os.environ.get("SYMCC_TOPSEED_EXPLORE_RATIO", "0.75"),
                default=0.75,
                minimum=0.0,
                maximum=1.0,
            ),
            learn_interval=_bounded_env_int(
                os.environ, "SYMCC_TOPSEED_LEARN_INTERVAL", 20, 1, 1_000_000
            ),
            max_candidates=_bounded_env_int(
                os.environ, "SYMCC_TOPSEED_CANDIDATES", 65_536, 2, 1_000_000
            ),
            max_runs=_bounded_env_int(
                os.environ, "SYMCC_TOPSEED_RUNS", 262_144, 2, 1_000_000
            ),
            max_features=_bounded_env_int(
                os.environ, "SYMCC_TOPSEED_FEATURES", 65_536, 1, 1_000_000
            ),
        )

    topseed_selector: TopSeedSelector | None = None
    if topseed_enabled:
        try:
            if resume_enabled and os.path.exists(topseed_state_path):
                restored_topseed = TopSeedSelector.load(topseed_state_path)
                if restored_topseed.program_context != topseed_program_context:
                    raise ValueError("TopSeed state belongs to another target")
                orphaned_topseed_runs = restored_topseed.fail_pending_runs()
                if orphaned_topseed_runs:
                    restored_topseed.save(topseed_state_path)
                    print(
                        "[Master] TopSeed retired "
                        f"{orphaned_topseed_runs} orphaned run(s) after restart",
                        flush=True,
                    )
                topseed_selector = restored_topseed
            else:
                topseed_selector = _new_topseed_selector()
        except (OSError, TypeError, ValueError) as error:
            print(
                f"[Master] TopSeed state reset after admission failure: {error}",
                file=sys.stderr,
                flush=True,
            )
            topseed_selector = _new_topseed_selector()
    online_value_profiles_enabled = adaptive_scheduler_enabled and os.environ.get(
        "SYMCC_VALUE_PROFILE_ONLINE", "1"
    ).lower() not in {"0", "false", "off", "no"}
    online_value_profiles = (
        OnlineValueProfileCoordinator(
            os.path.join(symcc_dir, ".empirical_value_profiles"),
            window=os.environ.get("SYMCC_VALUE_PROFILE_WINDOW", "256"),
            min_observations=os.environ.get(
                "SYMCC_VALUE_PROFILE_MIN_OBSERVATIONS", "8"
            ),
            max_distinct_values=os.environ.get("SYMCC_VALUE_PROFILE_MAX_DISTINCT", "4"),
            publish_interval_seconds=os.environ.get(
                "SYMCC_VALUE_PROFILE_PUBLISH_INTERVAL", "1"
            ),
            feedback_min_solver_queries=os.environ.get(
                "SYMCC_VALUE_PROFILE_FEEDBACK_MIN_QUERIES", "8"
            ),
            feedback_min_validated_ratio_ppm=os.environ.get(
                "SYMCC_VALUE_PROFILE_FEEDBACK_MIN_VALIDATED_PPM", "125000"
            ),
            feedback_min_solver_time_us=os.environ.get(
                "SYMCC_VALUE_PROFILE_FEEDBACK_MIN_SOLVER_US", "1000"
            ),
        )
        if online_value_profiles_enabled
        else None
    )
    self_config_enabled = adaptive_scheduler_enabled and os.environ.get(
        "SYMCC_SELF_CONFIG", "1"
    ).lower() not in {"0", "false", "off", "no"}
    try:
        self_config_parameters = max(
            1, int(os.environ.get("SYMCC_SELF_CONFIG_PARAMETERS", "4"))
        )
    except ValueError:
        self_config_parameters = 4
    try:
        self_config_seed = (
            int(os.environ["SYMCC_SELF_CONFIG_SEED"])
            if "SYMCC_SELF_CONFIG_SEED" in os.environ
            else None
        )
    except ValueError:
        self_config_seed = None
    parameter_policy = (
        ParaSuitSelfConfiguringPolicy(
            os.path.join(symcc_dir, ".self_config_state.json"),
            SYMCC_STRATEGY_PROFILES,
            custom_space=os.environ.get("SYMCC_SELF_CONFIG_SPACE"),
            schema_space=os.environ.get("SYMCC_SELF_CONFIG_SCHEMA"),
            prior_path=os.environ.get("SYMCC_SELF_CONFIG_PRIOR"),
            max_parameters=self_config_parameters,
            seed=self_config_seed,
            program_key=_self_config_program_key(args.target),
        )
        if self_config_enabled
        else None
    )
    algorithm_scheduler_enabled = adaptive_scheduler_enabled and os.environ.get(
        "SYMCC_SMT_ALGORITHM_SCHEDULER", "1"
    ).lower() not in {"0", "false", "off", "no"}
    algorithm_policy = (
        SMTAlgorithmScheduler(
            os.path.join(symcc_dir, ".smt_algorithm_state.json"),
            sequence_space=os.environ.get("SYMCC_SMT_ALGORITHM_SPACE"),
            prior_path=os.environ.get("SYMCC_SMT_ALGORITHM_PRIOR"),
            timeout_sec=TIMEOUT_SEC,
            seed=self_config_seed,
        )
        if algorithm_scheduler_enabled
        else None
    )
    offline_policy_enabled = algorithm_policy is not None and os.environ.get(
        "SYMCC_OFFLINE_POLICY", "1"
    ).lower() not in {"0", "false", "off", "no"}
    offline_policy = (
        OfflinePolicyController(
            os.environ.get(
                "SYMCC_OFFLINE_TRAJECTORY",
                os.path.join(symcc_dir, ".offline_trajectory.jsonl"),
            ),
            os.path.join(symcc_dir, ".offline_policy.json"),
            (sequence.name for sequence in algorithm_policy.sequences),
        )
        if offline_policy_enabled and algorithm_policy is not None
        else None
    )
    component_adaptation_enabled = adaptive_scheduler_enabled and os.environ.get(
        "SYMCC_COMPONENT_ADAPTATION", "1"
    ).lower() not in {"0", "false", "off", "no"}
    try:
        component_switch_interval = max(
            1.0, float(os.environ.get("SYMCC_COMPONENT_INTERVAL", "15"))
        )
    except ValueError:
        component_switch_interval = 15.0
    component_policy = (
        ComponentPortfolio(
            os.path.join(symcc_dir, ".component_state.json"),
            switch_interval=component_switch_interval,
            seed=self_config_seed,
        )
        if component_adaptation_enabled
        else None
    )
    configured_solver_component = _configured_solver_component(
        os.environ.get("SYMCC_SOLVER_COMPONENT", "learned")
    )
    component_choices = (
        component_policy.select(now=time.monotonic())
        if component_policy is not None
        else {
            "seed": "contextual",
            "splitter": "density"
            if os.environ.get("SYMCC_DENSITY_BALANCE") == "1"
            else (
                "focus" if os.environ.get("SYMCC_WORKER_DIVERSITY") == "1" else "whole"
            ),
            "solver": configured_solver_component,
            "replay": "balanced",
        }
    )
    try:
        replay_cooldown = max(1.0, float(os.environ.get("SYMCC_REPLAY_COOLDOWN", "30")))
    except ValueError:
        replay_cooldown = 30.0
    try:
        dag_replay_share = min(
            0.75, max(0.0, float(os.environ.get("SYMCC_DAG_REPLAY_SHARE", "0.25")))
        )
    except ValueError:
        dag_replay_share = 0.25

    # 主反馈路径是本实例的 symcc01/queue；AFL -M 会把它作为同campaign peer按
    # 连续ID同步。该参数只保留给显式配置的独立 -F producer，且永不指向
    # fuzzer01/queue或当前SymCC peer queue。
    afl_sync_queue = args.afl_sync_dir
    if afl_sync_queue:
        os.makedirs(afl_sync_queue, exist_ok=True)

    # 约束 hint 目录：写到 symcc_dir/extras，与 run_hybrid 传给 grimoire_gen 的 --extras
    # 路径一致（此前写到 output_dir/extras 与之不符 → GRIMOIRE 读空、hint 同步静默失效）。
    # 注：AFL 不会自动加载此目录，需显式 -x 才作字典；GRIMOIRE 经 --extras 消费这些 token。
    afl_extras_dir = os.path.join(symcc_dir, "extras")
    os.makedirs(afl_extras_dir, exist_ok=True)
    hint_id_ref = [0]

    # 选择性符号化：跟踪近期产出 interesting 结果的字节偏移范围
    # 使用滑动窗口避免范围无限膨胀（只保留最近 200 个偏移）
    recent_byte_offsets: list[int] = []
    FOCUS_BYTES_WINDOW = 200
    focus_bytes_str = ""
    compact_focus_enabled = os.environ.get(
        "SYMCC_COMPACT_FOCUS_SET", "1"
    ).lower() not in {"0", "false", "off", "no"}
    focus_set_path = os.path.join(symcc_dir, ".compact_focus_set")
    focus_set_str = ""

    stats_file = open(os.path.join(symcc_dir, "stats"), "a" if resume_enabled else "w")
    bitmap_path_triage = os.path.join(symcc_dir, ".triage_bitmap")

    print("[Master] SymCC MPI Fuzzing Helper")
    print(f"[Master] Workers: {num_workers}")
    print(f"[Master] AFL queue: {afl_config.queue}")
    print(f"[Master] AFL showmap: {afl_config.show_map}")
    print(f"[Master] SymCC output: {symcc_dir}")
    print(
        f"[Master] Adaptive scheduler: "
        f"{'LinUCB + strategy portfolio' if adaptive_policy else 'disabled'}"
    )
    print(
        "[Master] TopSeed campaign selector: "
        + (
            f"enabled ({len(topseed_selector.candidates)} candidates, "
            f"{topseed_selector.selections} completed dispatches)"
            if topseed_selector is not None
            else "disabled"
        )
    )
    print(
        f"[Master] Online empirical domains: "
        f"{'enabled' if online_value_profiles is not None else 'disabled'}"
    )
    print(
        f"[Master] Self configuration: {len(parameter_policy.parameters)} parameters"
        if parameter_policy is not None
        else "[Master] Self configuration: disabled"
    )
    print(
        f"[Master] Hot-swappable components: "
        f"{component_choices if component_policy is not None else 'disabled'}"
    )
    if adaptive_policy is not None and adaptive_policy.structural_tasks.enabled:
        _task_graph = adaptive_policy.structural_tasks.graph
        print(
            f"[Master] Structural tasks: {len(_task_graph.regions)} regions / "
            f"{len(_task_graph.site_regions)} branch sites from "
            f"{_task_graph.source_path}"
        )
    directed_sites = _parse_site_set(os.environ.get("SYMCC_DIRECTED_SITES"))
    directed_distances = load_directed_distance_map(
        os.environ.get("SYMCC_DIRECTED_DISTANCE")
    )
    concurrency_distances = load_directed_distance_map(
        os.environ.get("SYMCC_CONCURRENCY_GUIDANCE")
    )
    static_dependencies = load_static_dependency_map(
        os.environ.get("SYMCC_STATIC_DEPENDENCE")
    )
    static_branch_dependencies: dict[int, tuple[tuple[int, int], ...]] = {}
    directed_scores: dict[str, float] = {}
    if directed_sites:
        print(f"[Master] Directed sites: {sorted(directed_sites)}")
    if directed_distances:
        print(f"[Master] Directed distance map: {len(directed_distances)} sites")
    if concurrency_distances:
        print(f"[Master] Concurrency guidance map: {len(concurrency_distances)} sites")
    if static_dependencies:
        print(
            f"[Master] Static input dependencies: "
            f"{len(static_dependencies)} branch sites"
        )
        static_focus_enabled = os.environ.get(
            "SYMCC_STATIC_FOCUS", "0"
        ).lower() not in {"0", "false", "off", "no"}
        if static_focus_enabled:
            static_offsets: set[int] = set()
            static_complete = True
            for intervals in static_dependencies.values():
                for lower, upper in intervals:
                    if upper - lower > 4096 or len(static_offsets) > 65536:
                        static_complete = False
                        break
                    static_offsets.update(range(lower, upper + 1))
                if not static_complete:
                    break
            if (
                static_complete
                and static_offsets
                and _write_compact_focus_set(
                    focus_set_path, list(static_offsets), max_entries=65536
                )
            ):
                focus_set_str = focus_set_path
                print(
                    f"[Master] Static zero-execution focus: "
                    f"{len(static_offsets)} input bytes"
                )
    dpor_enabled = os.environ.get("SYMCC_DPOR", "0").lower() not in {
        "0",
        "false",
        "off",
        "no",
    }
    dpor_explorer = None
    dpor_preload = ""
    if dpor_enabled:
        dpor_preload = _find_schedule_preload()
        if not os.path.isfile(dpor_preload):
            print(
                f"[Master] DPOR schedule exploration requested but preload "
                f"runtime is missing: {dpor_preload}"
            )
            dpor_enabled = False
        else:
            try:
                dpor_depth = max(1, int(os.environ.get("SYMCC_DPOR_MAX_DEPTH", "64")))
            except ValueError:
                dpor_depth = 64
            try:
                dpor_window = max(1, int(os.environ.get("SYMCC_DPOR_WINDOW", "32")))
            except ValueError:
                dpor_window = 32
            try:
                dpor_per_input = max(
                    1, int(os.environ.get("SYMCC_DPOR_PREFIXES", "256"))
                )
            except ValueError:
                dpor_per_input = 256
            try:
                dpor_pending = max(1, int(os.environ.get("SYMCC_DPOR_PENDING", "4096")))
            except ValueError:
                dpor_pending = 4096
            dpor_constraint_out = os.environ.get(
                "SYMCC_SCHEDULE_CONSTRAINT_OUT", ""
            ).strip()
            dpor_smt_out = os.environ.get("SYMCC_SCHEDULE_SMT_OUT", "").strip()
            try:
                dpor_smt_events = min(
                    512,
                    max(2, int(os.environ.get("SYMCC_SCHEDULE_SMT_MAX_EVENTS", "128"))),
                )
            except ValueError:
                dpor_smt_events = 128
            try:
                dpor_smt_queries = min(
                    512,
                    max(1, int(os.environ.get("SYMCC_SCHEDULE_SMT_MAX_QUERIES", "64"))),
                )
            except ValueError:
                dpor_smt_queries = 64
            try:
                dpor_smt_memory_events = min(
                    64,
                    max(
                        0,
                        int(
                            os.environ.get("SYMCC_SCHEDULE_SMT_MAX_MEMORY_EVENTS", "32")
                        ),
                    ),
                )
            except ValueError:
                dpor_smt_memory_events = 32
            dpor_smt_sync_state = os.environ.get(
                "SYMCC_SCHEDULE_SMT_SYNC_STATE", "1"
            ).strip().lower() not in {"0", "false", "off", "no"}
            dpor_smt_order_encoding = (
                os.environ.get("SYMCC_SCHEDULE_SMT_ORDER_ENCODING", "partial")
                .strip()
                .lower()
            )
            if dpor_smt_order_encoding not in {
                "partial",
                "permutation",
            }:
                dpor_smt_order_encoding = "partial"
            dpor_smt_memory_model = (
                os.environ.get("SYMCC_SCHEDULE_MEMORY_MODEL", "SC")
                .strip()
                .upper()
                .replace("-", "_")
            )
            dpor_smt_memory_model = {
                "SEQUENTIAL_CONSISTENCY": "SC",
                "X86_TSO": "TSO",
                "RELEASE_ACQUIRE": "RA",
                "C11_RA": "RA",
            }.get(dpor_smt_memory_model, dpor_smt_memory_model)
            if dpor_smt_memory_model not in {"SC", "TSO", "RA"}:
                dpor_smt_memory_model = "SC"
            dpor_wakeup_tree = os.environ.get(
                "SYMCC_DPOR_WAKEUP_TREE", "1"
            ).strip().lower() not in {"0", "false", "off", "no"}
            dpor_condpor_graph = os.environ.get(
                "SYMCC_DPOR_CONDPOR_GRAPH", "1"
            ).strip().lower() not in {"0", "false", "off", "no"}
            dpor_explorer = DporScheduleExplorer(
                os.path.join(symcc_dir, ".dpor_schedule.json"),
                max_depth=dpor_depth,
                max_window=dpor_window,
                max_prefixes_per_input=dpor_per_input,
                max_pending=dpor_pending,
                constraint_path=dpor_constraint_out,
                smt_path=dpor_smt_out,
                smt_max_events=dpor_smt_events,
                smt_max_memory_events=dpor_smt_memory_events,
                smt_max_queries=dpor_smt_queries,
                smt_sync_state=dpor_smt_sync_state,
                smt_order_encoding=dpor_smt_order_encoding,
                smt_memory_model=dpor_smt_memory_model,
                wakeup_tree=dpor_wakeup_tree,
                condpor_graph=dpor_condpor_graph,
            )
            print(
                f"[Master] DPOR schedule exploration: {dpor_preload} "
                f"(pending {dpor_explorer.pending_count()}, "
                f"wakeup tree {int(dpor_wakeup_tree)}, "
                f"ConDPOR graph {int(dpor_condpor_graph)})"
            )
            if dpor_constraint_out:
                print(
                    f"[Master] DPOR schedule constraint artifact: {dpor_constraint_out}"
                )
            if dpor_smt_out:
                print(
                    f"[Master] DPOR schedule SMT artifact: "
                    f"{dpor_smt_out} (max events {dpor_smt_events}, "
                    f"max queries {dpor_smt_queries}, "
                    f"max memory events {dpor_smt_memory_events}, "
                    f"memory model {dpor_smt_memory_model}, "
                    f"sync state {int(dpor_smt_sync_state)}, "
                    f"order {dpor_smt_order_encoding})"
                )
    agentic_tasks_out = os.environ.get("SYMCC_AGENTIC_OUT", "")
    agentic_hints = load_hints(os.environ.get("SYMCC_AGENTIC_HINTS"))
    agentic_cmd = os.environ.get("SYMCC_AGENTIC_CMD", "")
    try:
        agentic_timeout = float(os.environ.get("SYMCC_AGENTIC_TIMEOUT", "2.0"))
    except ValueError:
        agentic_timeout = 2.0
    if agentic_tasks_out:
        print(f"[Master] Agentic task export: {agentic_tasks_out}")
    if agentic_hints:
        print(f"[Master] Agentic hints loaded: {len(agentic_hints)}")
    structured_agentic = StructuredAgenticController.from_environment(
        len(SYMCC_STRATEGY_PROFILES),
        os.path.join(symcc_dir, ".structured_agentic_ledger.jsonl"),
        _self_config_program_key(args.target),
    )
    agentic_backends = (
        None
        if structured_agentic is not None
        else AgenticBackendManager.from_environment(
            len(SYMCC_STRATEGY_PROFILES),
            legacy_command=agentic_cmd,
            timeout=agentic_timeout,
        )
    )
    if structured_agentic is not None:
        print(
            f"[Master] Structured agentic loop: "
            f"{structured_agentic.snapshot()}"
        )
    if agentic_backends is not None:
        print(
            f"[Master] Agentic backends: "
            f"{[state.backend.name for state in agentic_backends.backends]} "
            f"timeout={agentic_timeout:.2f}s"
        )
    builtin_agentic_enabled = os.environ.get(
        "SYMCC_AGENTIC_BUILTIN", "1" if adaptive_policy is not None else "0"
    ).lower() not in {"0", "false", "off", "no"}
    builtin_agentic = None
    if builtin_agentic_enabled:
        planner_path = os.environ.get(
            "SYMCC_AGENTIC_STATE", os.path.join(symcc_dir, ".agentic_planner.json")
        )
        builtin_agentic = BuiltinAgenticPlanner(
            planner_path, len(SYMCC_STRATEGY_PROFILES)
        )
        print(f"[Master] Built-in agentic planner: {planner_path}")
    semantic_fallback_enabled = adaptive_scheduler_enabled and os.environ.get(
        "SYMCC_SEMANTIC_FALLBACK", "1"
    ).lower() not in {"0", "false", "off", "no"}
    semantic_fallback = None
    if semantic_fallback_enabled:
        try:
            semantic_action_cap = max(
                1, int(os.environ.get("SYMCC_SEMANTIC_ACTIONS", "8"))
            )
        except ValueError:
            semantic_action_cap = 8
        try:
            semantic_exact_bytes = max(
                1, int(os.environ.get("SYMCC_SEMANTIC_EXACT_BYTES", "16"))
            )
        except ValueError:
            semantic_exact_bytes = 16
        try:
            semantic_focus_span = max(
                1, int(os.environ.get("SYMCC_SEMANTIC_FOCUS_SPAN", "128"))
            )
        except ValueError:
            semantic_focus_span = 128
        semantic_fallback = SemanticFallbackPlanner(
            os.path.join(symcc_dir, ".semantic_fallback.json"),
            strategy_count=len(SYMCC_STRATEGY_PROFILES),
            action_cap=semantic_action_cap,
            exact_bytes=semantic_exact_bytes,
            focus_span=semantic_focus_span,
        )
        print(f"[Master] Semantic fallback planner: {semantic_fallback.snapshot()}")
    proposal_source = os.environ.get("SYMCC_VERIFIED_PROPOSALS", "")
    semantic_proposals_enabled = adaptive_scheduler_enabled and os.environ.get(
        "SYMCC_SEMANTIC_PROPOSALS", "1"
    ).lower() not in {"0", "false", "off", "no"}
    verified_proposals = None
    semantic_proposals = None
    if proposal_source or semantic_proposals_enabled or structured_agentic is not None:
        try:
            proposal_max_bytes = max(
                1,
                int(os.environ.get("SYMCC_PROPOSAL_MAX_BYTES", str(16 * 1024 * 1024))),
            )
        except ValueError:
            proposal_max_bytes = 16 * 1024 * 1024
        try:
            proposal_patch_bytes = max(
                1, int(os.environ.get("SYMCC_PROPOSAL_PATCH_BYTES", str(64 * 1024)))
            )
        except ValueError:
            proposal_patch_bytes = 64 * 1024
        parser_command_raw = os.environ.get("SYMCC_PROPOSAL_PARSER", "")
        try:
            parser_command = (
                tuple(shlex.split(parser_command_raw)) if parser_command_raw else ()
            )
        except ValueError:
            parser_command = ()
        try:
            parser_timeout = max(
                0.01,
                min(
                    60.0, float(os.environ.get("SYMCC_PROPOSAL_PARSER_TIMEOUT", "1.0"))
                ),
            )
        except ValueError:
            parser_timeout = 1.0
        parser_cache_enabled = os.environ.get(
            "SYMCC_PROPOSAL_PARSER_CACHE", "1"
        ).lower() not in {"0", "false", "off", "no"}
        verified_proposals = VerifiedProposalManager(
            proposal_source,
            os.path.join(symcc_dir, ".verified_proposals"),
            max_candidate_bytes=proposal_max_bytes,
            max_patch_bytes=proposal_patch_bytes,
            parser_command=parser_command,
            parser_timeout=parser_timeout,
            parser_cache_enabled=parser_cache_enabled,
        )
        loaded_proposals = verified_proposals.scan()
        if os.environ.get("SYMCC_VERBOSE_STARTUP", "0").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            print(
                f"[Master] Verified semantic proposals: "
                f"{verified_proposals.snapshot()} (+{loaded_proposals})"
            )
        else:
            status_counts: dict[str, int] = {}
            for record in verified_proposals.records.values():
                status_counts[record.status] = (
                    status_counts.get(record.status, 0) + 1
                )
            print(
                "[Master] Verified semantic proposals: "
                f"records={len(verified_proposals.records)}, "
                f"loaded={loaded_proposals}, status={status_counts}"
            )
        if semantic_proposals_enabled:
            token_paths = tuple(
                filter(
                    None,
                    (
                        os.environ.get("SYMCC_STRING_HINT_DIR", ""),
                        os.path.join(symcc_dir, "extras"),
                        os.environ.get("SYMCC_SEMANTIC_TOKEN_DIR", ""),
                    ),
                )
            )
            try:
                grammar_rules = max(
                    32, int(os.environ.get("SYMCC_GRAMMAR_RULES", "2048"))
                )
            except ValueError:
                grammar_rules = 2048
            try:
                grammar_span = max(
                    8, int(os.environ.get("SYMCC_GRAMMAR_MAX_SPAN", "128"))
                )
            except ValueError:
                grammar_span = 128
            try:
                grammar_derivation_depth = max(
                    1,
                    min(8, int(os.environ.get("SYMCC_GRAMMAR_DERIVATION_DEPTH", "3"))),
                )
            except ValueError:
                grammar_derivation_depth = 3
            grammar_pareto = os.environ.get(
                "SYMCC_GRAMMAR_PARETO", "1"
            ).lower() not in {"0", "false", "off", "no"}
            query_grammar_holes = os.environ.get(
                "SYMCC_QUERY_GRAMMAR_HOLES", "1"
            ).lower() not in {"0", "false", "off", "no"}
            try:
                query_grammar_hole_max = max(
                    0,
                    min(64, int(os.environ.get("SYMCC_QUERY_GRAMMAR_HOLE_MAX", "16"))),
                )
            except ValueError:
                query_grammar_hole_max = 16
            try:
                history_plateau = max(
                    1,
                    min(
                        65536,
                        int(os.environ.get("SYMCC_HISTORY_ACQUISITION_PLATEAU", "64")),
                    ),
                )
                history_seeds = max(
                    1,
                    min(
                        4096,
                        int(os.environ.get("SYMCC_HISTORY_ACQUISITION_SEEDS", "128")),
                    ),
                )
                history_seed_bytes = max(
                    64,
                    min(
                        65536,
                        int(os.environ.get("SYMCC_HISTORY_ACQUISITION_BYTES", "4096")),
                    ),
                )
            except ValueError:
                history_plateau = 64
                history_seeds = 128
                history_seed_bytes = 4096
            pcfg_context_order_raw = os.environ.get(
                "SYMCC_PCFG_CONTEXT_ORDER", "history"
            )
            try:
                pcfg_context_order = (
                    SemanticProposalGenerator.normalize_pcfg_context_order(
                        pcfg_context_order_raw
                    )
                )
            except ValueError:
                pcfg_context_order = (
                    len(SemanticProposalGenerator.PCFG_CONTEXT_LEVELS) - 1
                )
                print(
                    "[Master] Invalid SYMCC_PCFG_CONTEXT_ORDER="
                    f"{pcfg_context_order_raw!r}; using history",
                    file=sys.stderr,
                )
            semantic_proposals = SemanticProposalGenerator(
                os.path.join(symcc_dir, ".semantic_proposal_generator.json"),
                token_paths=token_paths,
                max_input_bytes=proposal_max_bytes,
                max_grammar_rules=grammar_rules,
                max_grammar_span=grammar_span,
                max_cfg_derivation_depth=grammar_derivation_depth,
                pareto_scheduling=grammar_pareto,
                query_store_root=(
                    query_store
                    if (async_query_workers and query_grammar_holes)
                    else None
                ),
                query_holes=query_grammar_holes,
                max_query_holes=query_grammar_hole_max,
                plateau_observations=history_plateau,
                max_history_seeds=history_seeds,
                max_history_seed_bytes=history_seed_bytes,
                pcfg_context_order=pcfg_context_order,
                # The coordinator already checkpoints this state on the stats
                # cadence and during orderly shutdown.  Rewriting the growing
                # grammar/PCFG snapshot for every result serializes all workers
                # behind the master triage loop during sustained campaigns.
                autosave=False,
            )
            if os.environ.get("SYMCC_VERBOSE_STARTUP", "0").lower() in {
                "1",
                "true",
                "yes",
                "on",
            }:
                print(
                    "[Master] Built-in semantic proposals: "
                    f"{semantic_proposals.generated} generated, "
                    f"grammar={semantic_proposals.grammar_snapshot()}"
                )
            else:
                print(
                    "[Master] Built-in semantic proposals: "
                    f"{semantic_proposals.generated} generated, "
                    f"rules={len(semantic_proposals.grammar_rules)}"
                )

    research_run_dir = os.environ.get("SYMCC_RESEARCH_RUN_DIR", "")
    pcfg_research_artifact = os.environ.get("SYMCC_PCFG_RESEARCH_ARTIFACT", "")
    if not pcfg_research_artifact and research_run_dir:
        pcfg_research_artifact = os.path.join(
            research_run_dir, "pcfg_research_artifact.json"
        )
    parser_research_artifact = os.environ.get("SYMCC_PARSER_RESEARCH_ARTIFACT", "")
    if not parser_research_artifact and research_run_dir:
        parser_research_artifact = os.path.join(
            research_run_dir, "parser_research_artifact.json"
        )

    def _research_metadata() -> dict[str, str]:
        return {
            "experiment_id": os.environ.get("SYMCC_EXPERIMENT_ID", ""),
            "run_id": os.environ.get("SYMCC_RUN_ID", ""),
            "pair_id": os.environ.get("SYMCC_PAIR_ID", ""),
            "phase": os.environ.get("SYMCC_RESEARCH_PHASE", ""),
            "configuration": os.environ.get("SYMCC_RESEARCH_CONFIGURATION", ""),
            "target": args.target[0] if args.target else "",
            "random_seed": os.environ.get("SYMCC_RANDOM_SEED", ""),
            "cpu_budget_seconds": os.environ.get("SYMCC_CPU_BUDGET_SECONDS", ""),
            "cpu_cores": os.environ.get("SYMCC_CPU_CORES", ""),
            "parser_forest_mode": os.environ.get("SYMCC_PARSER_FOREST_MODE", ""),
            "parser_cross_mode": os.environ.get("SYMCC_PARSER_CROSS_MODE", ""),
        }

    def _write_pcfg_research_artifact() -> None:
        if semantic_proposals is None or not pcfg_research_artifact:
            return
        if not semantic_proposals.write_research_artifact(
            pcfg_research_artifact, _research_metadata()
        ):
            print(
                "[Master] Failed to write PCFG research artifact "
                f"{pcfg_research_artifact}",
                file=sys.stderr,
            )

    def _write_parser_research_artifact() -> None:
        if verified_proposals is None or not parser_research_artifact:
            return
        if not verified_proposals.write_research_artifact(
            parser_research_artifact, _research_metadata()
        ):
            print(
                "[Master] Failed to write parser research artifact "
                f"{parser_research_artifact}",
                file=sys.stderr,
            )

    _write_pcfg_research_artifact()
    _write_parser_research_artifact()

    def _record_proposal_retention(
        proposal_id: str,
        coverage_features: int,
    ) -> None:
        if verified_proposals is None:
            return
        verified_proposals.record_retention(proposal_id, coverage_features)
        if semantic_proposals is None:
            return
        record = verified_proposals.records.get(proposal_id)
        if record is None:
            return
        if coverage_features > 0:
            semantic_proposals.observe_history_seed(
                record.candidate_path, coverage_features
            )
        if record.grammar_rule_id:
            semantic_proposals.observe_grammar_retention(
                record.grammar_rule_id,
                coverage_features,
                context_id=(record.parser_context_id or record.grammar_context_id),
            )
        if record.history_seed_id:
            semantic_proposals.observe_history_retention(
                record.history_seed_id, coverage_features
            )

    coverage = CoverageBitmap()
    # 跳过耗时的 bitmap 初始化 — 前几个 triage 结果会自然建立 bitmap，
    # 代价是初期可能有少量假阳性 (interesting)，但不影响正确性
    stats = Stats()
    processed_files = set()
    processed_content_hashes = set()  # SHA-256 of already-analyzed file contents
    try:
        state_shards = max(
            1,
            int(
                os.environ.get("SYMCC_STATE_SHARDS", str(min(64, max(1, num_workers))))
            ),
        )
    except ValueError:
        state_shards = min(64, max(1, num_workers))
    hash_ledger = None
    if os.environ.get("SYMCC_PERSISTENT_STATE", "1").lower() not in {
        "0",
        "false",
        "off",
        "no",
    }:
        hash_ledger = PersistentShardLedger(
            os.path.join(symcc_dir, ".state_shards"), state_shards, "processed"
        )
        processed_content_hashes.update(hash_ledger.load_recent(MAX_DEDUP_ENTRIES))
        print(
            f"[Master] Persistent state shards: {state_shards} "
            f"({len(processed_content_hashes)} analyzed hashes restored)"
        )
    lease_ttl_default = max(120.0, float(TIMEOUT_SEC * 4))
    lease_ttl = _bounded_finite_float(
        os.environ.get("SYMCC_WORK_LEASE_TTL", str(lease_ttl_default)),
        default=lease_ttl_default,
        minimum=1.0,
        maximum=86400.0,
    )
    work_leases = (
        WorkLeaseJournal(
            os.path.join(symcc_dir, ".work_leases.jsonl"),
            lease_ttl=lease_ttl,
            compact_after=16384,
        )
        if os.environ.get("SYMCC_WORK_LEASES", "1").lower()
        not in {"0", "false", "off", "no"}
        else None
    )
    try:
        dispatch_protocol_retries = min(
            16,
            max(0, int(os.environ.get("SYMCC_DISPATCH_PROTOCOL_RETRIES", "1"))),
        )
    except ValueError:
        dispatch_protocol_retries = 1
    dispatch_watchdog_default = float(max(120, TIMEOUT_SEC * 4))
    dispatch_watchdog_sec = _dispatch_watchdog_timeout(
        os.environ.get(
            "SYMCC_DISPATCH_WATCHDOG_SEC",
            str(dispatch_watchdog_default),
        ),
        dispatch_watchdog_default,
    )
    deferred_dispatches = WorkLeaseJournal(
        os.path.join(symcc_dir, ".dispatch_protocol_deferred.jsonl"),
        lease_ttl=1.0,
        compact_after=4096,
    )
    shared_work_leases = None
    shared_target_leases = None
    shared_target_lease_ttl = lease_ttl
    shared_master_id = ""
    if os.environ.get("SYMCC_MULTI_MASTER_LEASES", "0").lower() not in {
        "0",
        "false",
        "off",
        "no",
    }:
        try:
            shared_lease_shards = max(
                1, int(os.environ.get("SYMCC_MULTI_MASTER_LEASE_SHARDS", "256"))
            )
        except ValueError:
            shared_lease_shards = 256
        shared_lock_ttl = _bounded_finite_float(
            os.environ.get("SYMCC_MULTI_MASTER_LOCK_TTL", "30"),
            default=30.0,
            minimum=1.0,
            maximum=3600.0,
        )
        shared_lock_acquire_timeout = _bounded_finite_float(
            os.environ.get("SYMCC_MULTI_MASTER_LOCK_ACQUIRE_TIMEOUT", "60"),
            default=60.0,
            minimum=0.001,
            maximum=3600.0,
        )
        node_name = os.uname().nodename if hasattr(os, "uname") else "node"
        shared_master_id = os.environ.get(
            "SYMCC_MASTER_ID", f"{node_name}:{os.getpid()}"
        )
        shared_work_lease_root = os.environ.get(
            "SYMCC_MULTI_MASTER_LEASE_DIR", os.path.join(symcc_dir, ".work_lease_table")
        )
        _probe_shared_root(shared_work_lease_root, LEASE_SHARED_FILESYSTEM_REQUIREMENTS)
        shared_work_leases = FencedWorkLeaseTable(
            shared_work_lease_root,
            shard_count=shared_lease_shards,
            lease_ttl=lease_ttl,
            lock_ttl=shared_lock_ttl,
            lock_acquire_timeout=shared_lock_acquire_timeout,
        )
        if os.environ.get("SYMCC_MULTI_MASTER_TARGET_LEASES", "1").lower() not in {
            "0",
            "false",
            "off",
            "no",
        }:
            shared_target_lease_ttl = _bounded_finite_float(
                os.environ.get(
                    "SYMCC_MULTI_MASTER_TARGET_LEASE_TTL",
                    str(lease_ttl),
                ),
                default=lease_ttl,
                minimum=1.0,
                maximum=86400.0,
            )
            shared_target_lease_root = os.environ.get(
                "SYMCC_MULTI_MASTER_TARGET_LEASE_DIR",
                os.path.join(symcc_dir, ".target_lease_table"),
            )
            _probe_shared_root(
                shared_target_lease_root,
                LEASE_SHARED_FILESYSTEM_REQUIREMENTS,
            )
            shared_target_leases = FencedTargetLeaseTable(
                shared_target_lease_root,
                shard_count=shared_lease_shards,
                lease_ttl=shared_target_lease_ttl,
                lock_ttl=shared_lock_ttl,
                lock_acquire_timeout=shared_lock_acquire_timeout,
            )
        print(
            f"[Master] Multi-master fenced leases: "
            f"{shared_lease_shards} shards as {shared_master_id}; "
            f"target groups={'on' if shared_target_leases else 'off'}"
        )
    state_coordinator = None
    state_tasks_path = os.path.join(symcc_dir, ".state_tasks.json")
    if os.environ.get("SYMCC_STATE_PARALLEL", "1").lower() not in {
        "0",
        "false",
        "off",
        "no",
    }:
        try:
            state_task_shards = max(
                1,
                int(
                    os.environ.get(
                        "SYMCC_STATE_TASK_SHARDS",
                        str(min(256, max(1, num_workers * 4))),
                    )
                ),
            )
        except ValueError:
            state_task_shards = min(256, max(1, num_workers * 4))
        try:
            state_steal_window = max(
                1, int(os.environ.get("SYMCC_STATE_STEAL_WINDOW", "64"))
            )
        except ValueError:
            state_steal_window = 64
        state_coordinator = StateShardCoordinator(
            shard_count=state_task_shards,
            worker_count=max(1, num_workers),
            steal_window=state_steal_window,
            lease_ttl=lease_ttl,
        )
        if resume_enabled:
            try:
                with open(state_tasks_path, encoding="utf-8") as stream:
                    state_coordinator.restore(json.load(stream))
            except (OSError, ValueError, TypeError):
                pass
        print(
            f"[Master] State-level task shards: {state_task_shards} "
            f"(steal window {state_steal_window})"
        )

    live_program = None
    live_state_store = None
    live_initializer = None
    # LiveContinuationExecutor currently accepts at most 4 MiB of concrete
    # input. Reuse the already clamped transport admission limit so a malformed
    # environment cannot make this pre-execution path allocate without bound.
    max_live_input_bytes = min(max_object_bytes, 4 * 1024 * 1024)
    live_program_path = os.environ.get("SYMCC_LIVE_PROGRAM", "")
    live_llvm_path = os.environ.get("SYMCC_LIVE_LLVM", "")
    lower_live_llvm = bool(live_llvm_path and not live_program_path)
    if lower_live_llvm:
        live_program_path = os.path.join(symcc_dir, ".lowered_live_program.json")
    if live_program_path:
        try:
            if lower_live_llvm:
                lowering = lower_llvm_to_program(
                    live_llvm_path,
                    live_program_path,
                    entry=os.environ.get("SYMCC_LIVE_ENTRY", "main"),
                    plugin=os.environ.get("SYMCC_LIVE_PLUGIN", ""),
                    compiler_args=shlex.split(
                        os.environ.get("SYMCC_LIVE_COMPILER_ARGS", "")
                    ),
                )
                if lowering["status"] != "lowered":
                    raise ValueError(
                        "LLVM lowering rejected live-state module: "
                        + "; ".join(lowering.get("diagnostics", ()))
                    )
            with open(live_program_path, encoding="utf-8") as stream:
                candidate_program = json.load(stream)
            if not isinstance(candidate_program, dict):
                raise ValueError("live continuation program is not a mapping")
            live_state_store = LiveStateStore(
                os.environ.get(
                    "SYMCC_LIVE_STATE_STORE", os.path.join(symcc_dir, ".live_states")
                ),
                page_size=max(64, int(os.environ.get("SYMCC_LIVE_PAGE_SIZE", "4096"))),
                **_live_state_graph_limits(os.environ),
            )
            live_program = candidate_program
            live_initializer = LiveContinuationExecutor(live_state_store)
            source = f" from {live_llvm_path}" if lower_live_llvm else ""
            print(f"[Master] Executable continuation-IR state mode enabled{source}")
        except (OSError, TypeError, ValueError, RuntimeError) as exc:
            print(
                f"[Master] WARNING: live continuation mode disabled: {exc}", flush=True
            )
            live_program = None
            live_state_store = None
            live_initializer = None

    def _save_state_tasks() -> None:
        if state_coordinator is None:
            return
        tmp = f"{state_tasks_path}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as stream:
                json.dump(
                    state_coordinator.to_mapping(),
                    stream,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            os.replace(tmp, state_tasks_path)
        except (OSError, TypeError, ValueError):
            try:
                os.unlink(tmp)
            except OSError:
                pass

    recovered_work_items: "list[tuple]" = []
    if resume_enabled:
        deferred_recovered = 0
        for payload in deferred_dispatches.recover_expired(lease_ttl=0.0):
            item = _recoverable_work_item_from_lease_payload(payload)
            if item is None:
                continue
            recovered_work_items.append(item)
            deferred_recovered += 1
        if deferred_recovered:
            print(f"[Master] Recovered protocol-deferred work: {deferred_recovered}")
    if resume_enabled and work_leases is not None:
        lease_recovered = 0
        for payload in work_leases.recover_expired(lease_ttl=0.0):
            item = _recoverable_work_item_from_lease_payload(payload)
            if item is None:
                continue
            recovered_work_items.append(item)
            lease_recovered += 1
        if lease_recovered:
            print(f"[Master] Recovered unfinished work leases: {lease_recovered}")
    if shared_work_leases is not None:
        shared_recovered = 0
        for payload in shared_work_leases.recover_expired(limit=4096):
            item = _recoverable_work_item_from_lease_payload(payload)
            if item is None:
                continue
            recovered_work_items.append(item)
            shared_recovered += 1
        if shared_recovered:
            print(f"[Master] Recovered/stealable fenced leases: {shared_recovered}")
    active_workers = {}  # rank -> input_path
    active_strategies: dict[int, int] = {}
    active_hashes: dict[int, str] = {}
    active_leases: dict[int, str] = {}
    active_lease_fences: dict[int, str] = {}
    active_target_leases: dict[int, tuple[tuple[int, ...], str]] = {}
    pending_target_leases: dict[tuple[int, ...], str] = {}
    dispatch_epoch = os.urandom(32).hex()
    dispatch_sequence = 0
    dispatch_generation_gate = _DispatchGenerationGate()
    retired_dispatch_gate = _RetiredDispatchGate()
    active_work_items: dict[int, tuple] = {}
    dispatch_protocol_attempts: dict[str, int] = {}
    preparing_dispatches: dict[int, _DispatchReservationTransaction] = {}
    active_dispatches: dict[int, _DispatchReservationTransaction] = {}
    committing_dispatches: dict[int, _DispatchReservationTransaction] = {}
    active_state_tasks: dict[int, str] = {}
    active_schedule_prefixes: dict[int, tuple[int, ...]] = {}
    active_agentic_tasks: dict[int, dict] = {}
    structured_agentic_decisions: dict[str, str] = {}
    active_component_choices: dict[int, dict[str, str]] = {}
    active_topseed_runs: dict[int, str] = {}
    completed_topseed_runs: dict[int, str] = {}
    topseed_item_proposals: dict[int, str] = {}
    completed_component_choices: dict[int, dict[str, str]] = {}
    completed_generated: dict[int, int] = {}
    active_parallel_cohorts: dict[int, int] = {}
    completed_parallel_cohorts: dict[int, int] = {}
    completed_hashes: dict[int, str] = {}
    executor_pulls: dict[str, int] = {"exact": 0, "tailored": 0}

    def _consume_structured_decisions(
        decisions: typing.Iterable[StructuredDecision],
    ) -> None:
        proposals_changed = False
        for decision in decisions:
            if decision.hint:
                for alias in decision.aliases:
                    agentic_hints[alias] = decision.hint
                    structured_agentic_decisions[alias] = decision.decision_id
            for proposal in decision.proposals:
                if verified_proposals is None or structured_agentic is None:
                    continue
                proposal_id = str(proposal.get("id", "") or "")
                admitted_id = verified_proposals.ingest(proposal)
                record = (
                    verified_proposals.records.get(admitted_id)
                    if admitted_id else None
                )
                if record is None:
                    structured_agentic.record_candidate_rejection(
                        decision.decision_id,
                        proposal_id,
                        "verified_proposal_admission",
                    )
                    continue
                structured_agentic.record_candidate_admission(
                    decision.decision_id,
                    record.candidate_sha256,
                    admitted_id,
                )
                proposals_changed = True
        if proposals_changed and verified_proposals is not None:
            verified_proposals.save()

    def _admit_shared_target(job: typing.Any) -> bool:
        if shared_target_leases is None:
            return True
        actions = _normalize_s2f_actions(getattr(job, "actions", ()))
        target = _normalize_branch_id(getattr(job, "target_branch", 0))
        group = _target_group(target, actions)
        if not group or group in pending_target_leases:
            return not group
        payload = {
            "path": str(getattr(job, "path", "") or ""),
            "target_branch": target,
            "s2f_actions": [[branch, action] for branch, action in actions],
        }
        try:
            token = shared_target_leases.claim_group(
                group,
                payload,
                owner=shared_master_id,
            )
        except OSError:
            token = None
        if not token:
            return False
        pending_target_leases[group] = token
        return True

    def _release_shared_target(
        lease: tuple[tuple[int, ...], str] | None,
    ) -> None:
        if shared_target_leases is None or lease is None:
            return
        targets, token = lease
        try:
            shared_target_leases.release_group(targets, token)
        except OSError:
            # The fencing TTL still bounds a lease if shared storage is
            # temporarily unavailable during release.
            pass

    worker_objects: dict[int, set[str]] = {rank: set() for rank in range(1, size)}
    worker_bitmap_versions: dict[int, int] = {rank: -1 for rank in range(1, size)}
    worker_profile_versions: dict[int, str] = {rank: "" for rank in range(1, size)}

    queue_id_ref = [_next_afl_artifact_id(queue_dir)]
    recovered_coverage_queue = CoverageQueueTransactionStore(
        symcc_dir, queue_dir
    ).recover(queue_id_ref[0])
    queue_id_ref[0] = recovered_coverage_queue.next_queue_id
    if recovered_coverage_queue.destinations:
        print(
            "[Master] Recovered coverage queue transactions: "
            f"published={sum(path is not None for path in recovered_coverage_queue.destinations)}, "
            f"redundant={recovered_coverage_queue.redundant}, "
            "conservative="
            f"{recovered_coverage_queue.conservatively_recovered}",
            flush=True,
        )
    crash_id_ref = [_next_afl_artifact_id(crashes_dir)]
    hang_id_ref = [_next_afl_artifact_id(hangs_dir)]
    last_stats_time = time.monotonic()
    # 轻量进度汇总节流（替代每批 triage print）：每 2s 一行，聚合计数走全局 stats
    last_progress_time = time.monotonic()
    prog_prev_generated = 0
    PROGRESS_INTERVAL = 2.0
    shared_heartbeat_interval = _lease_heartbeat_interval(
        lease_ttl, shared_target_lease_ttl
    )
    last_shared_heartbeat = 0.0
    last_proposal_scan = 0.0
    last_result_object_gc = time.monotonic()

    # SymCC 产生的有趣测试用例队列，会被重新分发给 workers
    # 每个元素是 (path, generation_depth)，depth=0 为 AFL 种子，depth=N 为第 N 代 SymCC 输出
    symcc_feedback_queue: list[tuple[str, int]] = []
    dpor_replay_items: list[tuple] = []
    # 记录每个文件的迭代代数
    file_generation: dict[str, int] = {}
    max_generation_reached = 0

    # GRIMOIRE 高价值输入直连 SymCC：master 扫描 grimoire-feed 目录，把新文件注入
    # 反馈队列，让 concolic 直接从结构有效的深层输入继续挖（结构合成 × 约束求解协同）。
    grimoire_feed_dir = args.grimoire_feed
    grimoire_seen: set[str] = set()
    last_grimoire_scan = 0.0

    # 细粒度并行分解 / 动态工作窃取（opt-in）：空闲产能出现时把种子细分为不相交字节区间
    # 子项填补 worker（见 _build_work_items）。worker 侧还按 rank 分配不同求解策略。
    _diversity = os.environ.get("SYMCC_WORKER_DIVERSITY") == "1"
    _focus_parts = _bounded_env_int(
        os.environ, "SYMCC_FOCUS_PARTITIONS", 8, 1, 65_536
    )
    # 按分支密度均衡划分（opt-in SYMCC_DENSITY_BALANCE=1，需密度剖析版 runtime）：细分前
    # 先用无求解的密度剖析 profile 种子，把热点字节隔离到窄区间，使各子项工作量更均衡。
    _density_balance = _diversity and os.environ.get("SYMCC_DENSITY_BALANCE") == "1"
    _target_cmd = args.target
    _use_stdin = "@@" not in _target_cmd
    _density_cache: "dict[str, list[int] | None]" = {}
    # 每轮只提交有限的新剖析；未完成时本轮退回等宽分片，不阻塞 master。
    _profile_budget = [8]
    _prof_dir = (
        tempfile.mkdtemp(prefix="symcc_dprof_")
        if (_density_balance or component_policy is not None)
        else None
    )
    _density_profile_jobs = (
        _bounded_env_int(
            os.environ, "SYMCC_DENSITY_PROFILE_JOBS", 1, 1, 8
        )
        if _prof_dir is not None
        else 0
    )
    _density_executor = (
        ThreadPoolExecutor(
            max_workers=_density_profile_jobs,
            thread_name_prefix="symcc-density",
        )
        if _prof_dir is not None
        else None
    )
    _density_pending_capacity = _bounded_env_int(
        os.environ, "SYMCC_DENSITY_PROFILE_PENDING", 32, 1, 1024
    )
    _density_futures: dict[str, Future[list[int] | None]] = {}

    def _compute_density(content: bytes) -> "list[int] | None":
        assert _prof_dir is not None
        task_dir = tempfile.mkdtemp(prefix="task-", dir=_prof_dir)
        staged_input = os.path.join(task_dir, "input.bin")
        dfile = os.path.join(task_dir, "density.txt")
        try:
            with open(staged_input, "wb") as stream:
                stream.write(content)
            penv = dict(os.environ)
            penv["SYMCC_DENSITY_OUT"] = dfile
            penv["SYMCC_OUTPUT_DIR"] = task_dir
            penv["SYMCC_ENABLE_LINEARIZATION"] = "1"
            penv.pop("SYMCC_WORKER_DIVERSITY", None)
            stdin_arg: "typing.Any" = subprocess.DEVNULL
            if _use_stdin:
                cmd = ["timeout", "-k", "2", "10"] + _target_cmd
            else:
                penv["SYMCC_INPUT_FILE"] = staged_input
                cmd = ["timeout", "-k", "2", "10"] + [
                    argument.replace("@@", staged_input)
                    for argument in _target_cmd
                ]
            try:
                if _use_stdin:
                    stdin_arg = open(staged_input, "rb")
                subprocess.run(
                    cmd,
                    env=penv,
                    stdin=stdin_arg,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                )
            finally:
                if _use_stdin and hasattr(stdin_arg, "close"):
                    stdin_arg.close()
            density = [0] * len(content)
            with open(dfile) as stream:
                for line in stream:
                    if line.startswith("#") or not line.strip():
                        continue
                    fields = line.split()
                    if len(fields) == 2:
                        offset = int(fields[0])
                        if 0 <= offset < len(density):
                            density[offset] = int(fields[1])
            return density if any(density) else None
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        finally:
            shutil.rmtree(task_dir, ignore_errors=True)

    def _profile_density(path: str) -> "list[int] | None":
        """Return cached density and asynchronously profile cache misses."""
        try:
            snapshot = stable_regular_file_snapshot(
                path,
                max_bytes=max_object_bytes,
                retain_content=True,
            )
        except (OSError, ValueError):
            return None
        assert snapshot.content is not None
        content = snapshot.content
        h = snapshot.sha256
        if h in _density_cache:
            return _density_cache[h]
        pending = _density_futures.get(h)
        if pending is not None:
            if not pending.done():
                return None
            try:
                result = pending.result()
            except Exception:
                result = None
            _density_futures.pop(h, None)
            _density_cache[h] = result
            return result
        if (
            _profile_budget[0] <= 0
            or _density_executor is None
            or len(_density_futures) >= _density_pending_capacity
        ):
            return None
        _profile_budget[0] -= 1
        _density_futures[h] = _density_executor.submit(
            _compute_density, content
        )
        return None

    # 边产出率在线学习（CoFuzz + T-Scheduler 风格）：
    # 跟踪每种种子类型被 concolic 分析后产出 interesting 结果的概率
    # 使用 Beta-Bernoulli Thompson Sampling（T-Scheduler AsiaCCS'24）
    edge_yield_counts: dict[str, list[int]] = {
        "cov": [1, 1],  # [alpha (successes+1), beta (failures+1)]，先验 Beta(1,1)
        "symcc": [1, 1],
        "normal": [1, 1],
    }
    _triage_profile_enabled = os.environ.get("SYMCC_MASTER_PROFILE") == "1"
    _triage_detail: dict[str, float] = {}

    def _note_triage_detail(name: str, started: float) -> None:
        if not _triage_profile_enabled:
            return
        _triage_detail[name] = (
            _triage_detail.get(name, 0.0) + time.monotonic() - started
        )

    def _update_edge_yield(input_path: str, produced_interesting: bool) -> None:
        """更新种子类型的 Beta 分布参数。"""
        name = os.path.basename(input_path)
        if "+cov" in name:
            seed_type = "cov"
        elif "symcc_" in name:
            seed_type = "symcc"
        else:
            seed_type = "normal"
        if produced_interesting:
            edge_yield_counts[seed_type][0] += 1  # alpha++
        else:
            edge_yield_counts[seed_type][1] += 1  # beta++

    def _get_edge_yield() -> dict[str, float]:
        """Thompson Sampling：从各类型的 Beta 分布中采样，作为优先级分数。

        比 Laplace 平滑更优：自动在探索（数据少时高方差）
        和利用（数据多时收敛到真实率）之间平衡。
        """
        result = {}
        for k, (alpha, beta) in edge_yield_counts.items():
            result[k] = random.betavariate(alpha, beta)
        return result

    _topseed_save_failures = [0]

    def _save_topseed() -> None:
        if topseed_selector is None:
            return
        try:
            topseed_selector.save(topseed_state_path)
        except (OSError, TypeError, ValueError) as error:
            _topseed_save_failures[0] += 1
            if _topseed_save_failures[0] <= 3:
                print(
                    f"[Master] TopSeed snapshot publication failed: {error}",
                    file=sys.stderr,
                    flush=True,
                )

    def _observe_topseed(
        worker_rank: int,
        _path: str,
        generated_coverage: tuple[int, ...],
        telemetry: SolverTelemetry | None,
        killed: bool,
        retcode: int,
    ) -> None:
        if topseed_selector is None:
            return
        run = completed_topseed_runs.pop(worker_rank, "")
        if not run:
            return
        condition = TopSeedSelector.path_condition_from_branch_trace(
            telemetry.branch_trace if telemetry is not None else (),
            maximum=topseed_selector.max_features,
        )
        topseed_selector.observe(
            run,
            generated_coverage[:topseed_selector.max_features],
            path_condition=condition,
            triggers_bug=_returncode_indicates_crash(retcode, killed),
            failed=bool(killed or retcode == -1),
        )
        _save_topseed()

    def _adaptive_score(
        path: str, attrs: dict, type_yield: float, frontier: float
    ) -> float:
        """Build a contextual seed score from cheap AFL and learned features."""
        assert adaptive_policy is not None
        directed = directed_scores.get(path, 0.0)
        base_score = (
            attrs["static"] + type_yield * 30.0 + frontier * 40.0 + directed * 120.0
        )
        context = adaptive_policy.context(
            path,
            name=attrs["name"],
            size=attrs["size"],
            seed_type=attrs["type"],
            generation=file_generation.get(path, 0),
            base_score=base_score,
            frontier=frontier,
            type_yield=type_yield,
        )
        return adaptive_policy.score(context)

    def _observe_adaptive(
        path: str,
        coverage_delta: int,
        interesting_cases: int,
        elapsed: float,
        killed: bool,
        strategy: int,
        telemetry: SolverTelemetry | None,
        worker_rank: int,
        s2f_actions: tuple[tuple[int, str], ...],
        parameter_token: str,
        parameter_overrides: dict[str, str],
    ) -> None:
        if adaptive_policy is None:
            return
        instance_id = completed_hashes.get(worker_rank, "")
        if (
            directed_sites or directed_distances or concurrency_distances
        ) and telemetry is not None:
            sites = {entry[3] for entry in telemetry.branch_trace}
            score = 0.0
            if directed_sites:
                hits = len(sites & directed_sites)
                score = max(score, min(1.0, hits / max(1, len(directed_sites))))
            distances = [
                directed_distances[site] for site in sites if site in directed_distances
            ]
            if distances:
                score = max(score, 1.0 / (1.0 + min(distances)))
            concurrency = [
                concurrency_distances[site]
                for site in sites
                if site in concurrency_distances
            ]
            if concurrency:
                score = max(score, 1.0 / (1.0 + min(concurrency)))
            if score > 0.0:
                directed_scores[path] = max(directed_scores.get(path, 0.0), score)
        if telemetry is not None and static_dependencies:
            for (
                _parent,
                _actual,
                open_branch,
                site,
                _taken,
                _interesting,
            ) in telemetry.branch_trace:
                intervals = static_dependencies.get(site)
                if open_branch and intervals:
                    static_branch_dependencies[open_branch] = intervals
        if semantic_fallback is not None and telemetry is not None:
            profile_started = (
                time.monotonic() if _triage_profile_enabled else 0.0
            )
            semantic_fallback.observe(
                path,
                telemetry,
                sha256=instance_id,
                coverage_delta=coverage_delta,
                interesting_cases=interesting_cases,
                elapsed=elapsed,
                killed=killed,
            )
            _note_triage_detail("semantic_fallback", profile_started)
        if (
            semantic_proposals is not None
            and verified_proposals is not None
            and telemetry is not None
        ):
            profile_started = (
                time.monotonic() if _triage_profile_enabled else 0.0
            )
            semantic_proposals.generate_into(
                verified_proposals,
                path,
                telemetry,
                coverage_delta=coverage_delta,
            )
            _note_triage_detail("semantic_proposals", profile_started)
        profile_started = time.monotonic() if _triage_profile_enabled else 0.0
        adaptive_detail: dict[str, float] | None = (
            {} if _triage_profile_enabled else None)
        reward = adaptive_policy.observe(
            path,
            coverage_delta=coverage_delta,
            interesting_cases=interesting_cases,
            elapsed=elapsed,
            killed=killed,
            strategy=strategy,
            telemetry=telemetry,
            worker=worker_rank,
            s2f_actions=s2f_actions,
            profile=adaptive_detail,
        )
        if adaptive_detail:
            for detail_name, elapsed_s in adaptive_detail.items():
                _triage_detail[f"adaptive.{detail_name}"] = (
                    _triage_detail.get(f"adaptive.{detail_name}", 0.0)
                    + elapsed_s
                )
        _note_triage_detail("adaptive_scheduler", profile_started)
        profile_started = time.monotonic() if _triage_profile_enabled else 0.0
        task_components = completed_component_choices.pop(worker_rank, {})
        if component_policy is not None:
            component_policy.observe(
                task_components,
                reward=reward,
                elapsed=elapsed,
                killed=killed,
            )
        generated_count = completed_generated.pop(worker_rank, interesting_cases)
        if parallel_controller is not None:
            parallel_controller.observe(
                cohort=completed_parallel_cohorts.pop(worker_rank, None),
                reward=reward,
                coverage_delta=coverage_delta,
                generated=generated_count,
                interesting=interesting_cases,
                elapsed=elapsed,
                killed=killed,
            )
        if parameter_policy is not None and parameter_token:
            parameter_policy.observe(
                parameter_token,
                reward=reward,
                elapsed=elapsed,
                killed=killed,
                coverage_features=branch_outcome_features(
                    telemetry.branch_trace if telemetry is not None else (),
                    maximum=parameter_policy.max_coverage_features,
                ),
            )
        if algorithm_policy is not None:
            algorithm_policy.observe(
                parameter_overrides.get("SYMCC_ALGORITHM_TOKEN", ""),
                path=path,
                telemetry=telemetry,
                reward=reward,
                elapsed=elapsed,
                killed=killed,
            )
        if offline_policy is not None:
            try:
                algorithm_propensity = float(
                    parameter_overrides.get("SYMCC_ALGORITHM_PROPENSITY", "1")
                )
            except ValueError:
                algorithm_propensity = 1.0
            try:
                algorithm_budget = float(
                    parameter_overrides.get("SYMCC_ALGORITHM_BUDGET_SEC", TIMEOUT_SEC)
                )
            except (TypeError, ValueError, OverflowError):
                algorithm_budget = float(TIMEOUT_SEC)
            offline_policy.observe(
                action=parameter_overrides.get("SYMCC_ALGORITHM_SEQUENCE", ""),
                propensity=algorithm_propensity,
                telemetry=telemetry,
                reward=reward,
                elapsed=elapsed,
                coverage_delta=coverage_delta,
                generated=generated_count,
                interesting=interesting_cases,
                concurrent_workers=len(active_workers) + 1,
                killed=killed,
                instance_id=instance_id,
                budget_sec=algorithm_budget,
            )
        _note_triage_detail("policy_updates", profile_started)
        completed_hashes.pop(worker_rank, None)

    def _rank_source_paths(paths: list[str]) -> list[str]:
        if adaptive_policy is None or component_choices.get("seed") != "contextual":
            return paths
        ranked: list[tuple[float, str]] = []
        for path in paths:
            context = adaptive_policy.contexts.get(path)
            if context is None:
                attrs = afl_config._file_cache.get(path)
                seed_type = (
                    attrs["type"]
                    if attrs
                    else ("symcc" if path.startswith(symcc_dir) else "normal")
                )
                alpha, beta = edge_yield_counts.get(seed_type, [1, 1])
                context = adaptive_policy.context(
                    path,
                    name=attrs["name"] if attrs else os.path.basename(path),
                    size=attrs["size"] if attrs else None,
                    seed_type=seed_type,
                    generation=file_generation.get(path, 0),
                    base_score=(attrs["static"] if attrs else 0.0)
                    + directed_scores.get(path, 0.0) * 120.0,
                    type_yield=alpha / (alpha + beta),
                )
            ranked.append((adaptive_policy.score(context), path))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [path for _, path in ranked]

    # --save-all: 保存所有生成的测试用例（不经过滤）
    save_all_dir = None
    if args.save_all:
        save_all_dir = args.save_all
        os.makedirs(save_all_dir, exist_ok=True)
        print(f"[Master] Saving all test cases to: {save_all_dir}")

    # #10 重复求解拆分：master 侧落 accepted(=interesting_count,真正判新纳入的)与 generated,
    # 供跨-worker 冗余 = reported(worker 上报) - accepted 计算。
    _wprof = os.environ.get("SYMCC_WORKER_PROFILE") == "1"
    _wprof_dir = os.environ.get("SYMCC_WPROF_DIR") or symcc_dir

    def _flush_master_redun() -> None:
        if not _wprof:
            return
        try:
            os.makedirs(_wprof_dir, exist_ok=True)
            with open(os.path.join(_wprof_dir, "redun_master.csv"), "w") as _f:
                _f.write(
                    f"generated,accepted\n{stats.generated_count},{stats.interesting_count}\n"
                )
        except (IOError, OSError):
            pass

    # 信号处理：收到 SIGTERM/SIGINT 时优雅退出
    shutdown_requested = False

    def _signal_handler(signum: int, frame: object) -> None:
        nonlocal shutdown_requested
        shutdown_requested = True
        _flush_master_redun()
        print(
            f"\n[Master] Received signal {signum}, shutting down...",
            file=sys.stderr,
            flush=True,
        )

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # AFL bitmap 版本日志：仅传播新增 bit；落后超过日志窗口的 worker 接收完整快照。
    bitmap_version = 0
    try:
        bitmap_history = max(1, int(os.environ.get("SYMCC_BITMAP_HISTORY", "64")))
    except ValueError:
        bitmap_history = 64
    bitmap_shards = _bounded_env_int(
        os.environ, "SYMCC_BITMAP_SHARDS", 1, 1, 4096
    )
    bitmap_journal = (
        ShardedBitmapDeltaJournal(bitmap_history, bitmap_shards)
        if bitmap_shards > 1
        else BitmapDeltaJournal(bitmap_history)
    )
    if bitmap_shards > 1:
        print(f"[Master] Bitmap delta journal shards: {bitmap_shards}")
    coverage_gossip = None
    coverage_gossip_enabled = os.environ.get(
        "SYMCC_COVERAGE_GOSSIP",
        "1" if shared_work_leases is not None else "0",
    ).lower() not in {"0", "false", "off", "no"}
    if coverage_gossip_enabled:
        coverage_owner_shards = _bounded_env_int(
            os.environ,
            "SYMCC_COVERAGE_OWNER_SHARDS",
            max(16, bitmap_shards),
            1,
            4096,
        )
        coordinator_index = _bounded_env_int(
            os.environ, "SYMCC_COORDINATOR_INDEX", 0, 0, 4095
        )
        coordinator_count = _bounded_env_int(
            os.environ,
            "SYMCC_COORDINATOR_COUNT",
            coordinator_index + 1,
            coordinator_index + 1,
            4096,
        )
        coverage_owner_ttl = _bounded_finite_float(
            os.environ.get("SYMCC_COVERAGE_OWNER_TTL", "30"),
            default=30.0,
            minimum=3.0,
            maximum=3600.0,
        )
        coverage_lock_acquire_timeout = _bounded_finite_float(
            os.environ.get("SYMCC_COVERAGE_OWNER_LOCK_ACQUIRE_TIMEOUT", "60"),
            default=60.0,
            minimum=0.001,
            maximum=3600.0,
        )
        coverage_owner_root = os.environ.get(
            "SYMCC_COVERAGE_OWNER_DIR", os.path.join(symcc_dir, ".coverage_owner")
        )
        _probe_shared_root(coverage_owner_root, COVERAGE_SHARED_FILESYSTEM_REQUIREMENTS)
        coverage_gossip = CoverageOwnerShardGossip(
            coverage_owner_root,
            shard_count=coverage_owner_shards,
            coordinator_id=shared_master_id,
            coordinator_index=coordinator_index,
            coordinator_count=coordinator_count,
            heartbeat_ttl=coverage_owner_ttl,
            lock_acquire_timeout=coverage_lock_acquire_timeout,
        )
        initial_gossip = coverage_gossip.pull()
        if initial_gossip and coverage.merge_delta(initial_gossip):
            bitmap_version = 1
            bitmap_journal.record(bitmap_version, coverage.consume_delta())
        print(f"[Master] Coverage-owner gossip: {coverage_gossip.snapshot()}")

    def _claim_global_coverage(
        bitmap_data: bytes | list[tuple[int, int]],
    ) -> tuple[int, bool]:
        if coverage_gossip is None:
            delta = coverage.merge_delta(bitmap_data)
            return delta, delta > 0
        return _claim_coverage_transaction(coverage, coverage_gossip, bitmap_data)

    def _claim_global_coverage_many(
        bitmap_batch: list[bytes | list[tuple[int, int]]],
    ) -> tuple[list[int], bool]:
        if coverage_gossip is None:
            deltas = [coverage.merge_delta(bitmap) for bitmap in bitmap_batch]
            return deltas, any(deltas)
        return _claim_coverage_transactions(
            coverage, coverage_gossip, bitmap_batch
        )

    # Establish an AFL+SymCC union before the first symbolic task is sent.  A
    # resumed campaign must replay its own durable queue as well as AFL's: the
    # persisted bridge baseline may predate the last SymCC queue transaction.
    afl_coverage_bridge = AflCoverageBridge(
        afl_config,
        coverage,
        bitmap_path_triage,
        claim_callback=_claim_global_coverage,
    )
    initial_afl_coverage = afl_coverage_bridge.ingest_queue()
    try:
        initial_symcc_paths = sorted(
            entry.path
            for entry in os.scandir(queue_dir)
            if entry.is_file(follow_symlinks=False)
        )
    except OSError:
        initial_symcc_paths = []
    initial_symcc_coverage = afl_coverage_bridge.ingest(initial_symcc_paths)
    initial_delta = coverage.consume_delta()
    if initial_delta:
        bitmap_version += 1
        bitmap_journal.record(bitmap_version, initial_delta)
    print(
        "[Master] AFL+SymCC coverage baseline: "
        f"afl_examined={initial_afl_coverage.examined}, "
        f"afl_ingested={initial_afl_coverage.ingested}, "
        f"symcc_examined={initial_symcc_coverage.examined}, "
        f"symcc_ingested={initial_symcc_coverage.ingested}, "
        f"failed={initial_afl_coverage.failed + initial_symcc_coverage.failed}, "
        f"features={coverage.feature_count}",
        flush=True,
    )
    if not initial_afl_coverage.complete or not initial_symcc_coverage.complete:
        print(
            "[Master] Coverage baseline is incomplete; failed queue entries "
            "remain eligible for retry and are not counted as synchronized",
            file=sys.stderr,
            flush=True,
        )

    shared_coverage_snapshot_enabled = os.environ.get(
        "SYMCC_SHARED_COVERAGE_SNAPSHOT", "1"
    ).lower() not in {"0", "false", "off", "no"}
    shared_coverage_snapshot_path = (
        os.path.join(symcc_dir, ".shared_coverage_snapshot")
        if shared_coverage_snapshot_enabled
        else ""
    )
    shared_coverage_snapshot_interval = _bounded_env_float(
        os.environ,
        "SYMCC_SHARED_COVERAGE_SNAPSHOT_INTERVAL",
        0.1,
        0.0,
        60.0,
    )
    last_shared_coverage_publish = 0.0
    shared_coverage_dirty = True

    def _publish_current_coverage(
        *, changed: bool = False, force: bool = False
    ) -> bool:
        nonlocal last_shared_coverage_publish, shared_coverage_dirty
        if not shared_coverage_snapshot_enabled:
            return False
        shared_coverage_dirty = shared_coverage_dirty or changed
        if not shared_coverage_dirty:
            return False
        now = time.monotonic()
        if (
            not force
            and shared_coverage_snapshot_interval > 0.0
            and now - last_shared_coverage_publish
            < shared_coverage_snapshot_interval
        ):
            return False
        bitmap = bytes(coverage.data or bytearray(_AFL_MAP_SIZE))
        if not _publish_coverage_snapshot(
            shared_coverage_snapshot_path, bitmap_version, bitmap
        ):
            return False
        shared_coverage_dirty = False
        last_shared_coverage_publish = now
        return True

    _publish_current_coverage(force=True)

    # 性能计时器（环境变量 SYMCC_MASTER_PROFILE=1 时输出）
    _prof = os.environ.get("SYMCC_MASTER_PROFILE") == "1"
    _t_scan = 0.0  # AFL queue 扫描耗时
    _t_dispatch = 0.0  # MPI send (dispatch) 耗时
    _t_recv = 0.0  # MPI recv (result) 耗时
    _t_triage = 0.0  # batch_triage 耗时
    _t_idle = 0.0  # sleep 耗时
    _n_scan = 0
    _n_dispatch = 0
    _n_recv = 0
    _n_triage = 0
    _n_recv_bytes = 0  # 估算 MPI recv 数据量

    # 注：曾尝试"多样性调度"（避免并发下发相似种子以降低冗余），但 A/B 实测
    # 14 workers 下 useful 比率 18.0%(off) vs 17.9%(on) 无差异——并行 concolic 冗余
    # 主要是结构性的（不同种子翻转分支后仍产出覆盖公共下游代码的输入），
    # 与文献一致（concolic 并行本质亚线性扩展）。故不采用调度层去冗余，
    # 转而通过 SYMCC_WORKER_CAP 限制 concolic worker 数、把富余核心给扩展性更好的 AFL。

    # 运行时自适应分配（KRAKEN/Boian 风格）：
    #  - control_file (.active_workers)：run_hybrid 控制器写入期望活跃 worker 数 K，
    #    master 只向 rank 1..K 派发，rank K+1..N 被"停泊"（消费其 READY 后不派发，
    #    worker 阻塞在 recv 上，~0 CPU，释放核心给 AFL）。K 增大时主动直接派发唤醒。
    #    停泊可逆、不丢弃任何种子 → 不丢覆盖率。
    #  - stats_out_file (.symcc_stats)：master 周期性写出累计产出，供控制器读取产出率。
    control_file = os.path.join(symcc_dir, ".active_workers")
    stats_out_file = os.path.join(symcc_dir, ".symcc_stats")
    max_active_workers = num_workers  # 默认全部活跃
    try:
        parallel_minimum = max(1, int(os.environ.get("SYMCC_MIN_ACTIVE_WORKERS", "1")))
    except ValueError:
        parallel_minimum = 1
    try:
        parallel_interval = max(
            1.0, float(os.environ.get("SYMCC_PARALLEL_INTERVAL", "20"))
        )
    except ValueError:
        parallel_interval = 20.0
    try:
        parallel_step = max(
            1, int(os.environ.get("SYMCC_PARALLEL_STEP", max(1, num_workers // 8)))
        )
    except ValueError:
        parallel_step = max(1, num_workers // 8)
    parallel_controller = (
        AdaptiveParallelismController(
            parallel_minimum,
            num_workers,
            initial=num_workers,
            interval=parallel_interval,
            step=parallel_step,
        )
        if component_adaptation_enabled
        else None
    )
    last_queue_depth = 0
    # idle_ranks：已发 READY、正阻塞在 recv 等待工作的 worker。
    # 其中 rank > max_active_workers 者被"停泊"（不派发 → ~0 CPU）；
    # K 增大时它们自动变为可派发（无需额外唤醒逻辑）。
    idle_ranks: set[int] = set()
    last_control_check = 0.0
    last_stats_out = 0.0  # .symcc_stats 快速写出节流（供控制器）
    last_scan_time = 0.0  # AFL queue 扫描节流
    last_afl_coverage_poll = time.monotonic()
    # Worker 忙时没有即时派发容量，100 ms 轮询只会反复遍历不断增长的 AFL
    # queue。真实 profile 中它消耗了约三分之一 wall time。空闲 worker 仍会
    # 绕过该间隔立即扫描，因此提高 busy-path 间隔不会增加饥饿 worker 延迟。
    queue_poll_interval = _bounded_env_float(
        os.environ,
        "SYMCC_QUEUE_POLL_INTERVAL",
        0.5,
        0.01,
        60.0,
    )
    # BSFuzz 跨-worker 超时分支共享聚合
    branch_share_master = os.environ.get("SYMCC_BRANCH_SHARE") == "1"
    skip_sites_master_path = os.path.join(symcc_dir, ".skip_sites")
    global_timeout_sites: set[int] = set()

    def _read_active_workers() -> int:
        try:
            with open(control_file) as cf:
                k = int(cf.read().strip())
            return max(1, min(num_workers, k))
        except (IOError, OSError, ValueError):
            return max_active_workers  # 无文件/无效 → 保持

    def _write_symcc_stats() -> None:
        try:
            tmp = stats_out_file + ".tmp"
            with open(tmp, "w") as sf:
                sf.write(
                    "%d %d %d %d\n"
                    % (
                        stats.interesting_count,
                        stats.generated_count,
                        len(coverage.edges),
                        max_active_workers,
                    )
                )
            os.replace(tmp, stats_out_file)
        except OSError:
            pass

    # 动态工作窃取的"未派发工作项"跨轮结转：一轮内因 worker 全忙而未及派发的（细分）
    # 工作项原样保留到下一轮，避免按路径去重误将同种子的其余字节区间丢弃（否则该种子
    # 只分析了首个区间就再不复访）。carried_paths 用于把已在结转队列中的路径排除出本轮
    # 重新细分，避免与 AFL 队列重扫产生重复项。
    carried_items: "list[tuple]" = list(recovered_work_items)
    carried_paths: set[str] = {item[0] for item in carried_items}

    # K-Scheduler 风格前沿调度（opt-in，SYMCC_KSCHED=1）：按"覆盖当前稀有边（≈覆盖前沿/
    # CFG 中心性）"给 AFL 候选种子加权，把 concolic 预算投向最可能触达未探索区域处
    # （She et al. S&P'22）。用 afl-showmap 取每种子边集（按内容哈希缓存、每轮限流预算），
    # rarity=Σ 1/(freq+1)。默认关闭：零成本、不影响既有调度。失败一律返回 0（优雅退化）。
    _ksched = os.environ.get("SYMCC_KSCHED") == "1"
    _edge_freq: dict[int, int] = {}
    _frontier_cache: dict[str, float] = {}
    _frontier_budget = [16]
    _frontier_bm = os.path.join(symcc_dir, ".frontier_bm")
    _topseed_profile_cache: dict[str, tuple[int, ...]] = {}
    _topseed_profile_budget = [0]
    _topseed_profile_limit = _bounded_env_int(
        os.environ, "SYMCC_TOPSEED_PROFILE_BUDGET", 16, 1, 4096
    )
    _aux_showmap_jobs = _bounded_env_int(
        os.environ, "SYMCC_AUX_SHOWMAP_JOBS", 1, 1, 8
    )
    _aux_showmap_executor = ThreadPoolExecutor(
        max_workers=_aux_showmap_jobs,
        thread_name_prefix="symcc-aux-showmap",
    )
    _aux_showmap_capacity = _bounded_env_int(
        os.environ, "SYMCC_AUX_SHOWMAP_PENDING", 64, 1, 4096
    )
    _topseed_profile_futures: dict[
        str,
        Future[tuple[str, bytes | list[tuple[int, int]] | None]],
    ] = {}
    _frontier_futures: dict[
        str,
        Future[tuple[str, bytes | list[tuple[int, int]] | None]],
    ] = {}

    def _aux_showmap(
        stable_path: str, kind: str
    ) -> tuple[str, bytes | list[tuple[int, int]] | None]:
        bitmap = os.path.join(
            symcc_dir,
            f".{kind}.{os.getpid()}.{time.monotonic_ns()}.bm",
        )
        try:
            return afl_config.run_showmap(stable_path, bitmap)
        finally:
            try:
                os.unlink(bitmap)
            except OSError:
                pass

    def _topseed_profile(path: str) -> tuple[str, tuple[int, ...]] | None:
        if topseed_selector is None:
            return None
        try:
            candidate_id, stable_path, _content = object_store.import_path(path)
        except (OSError, ValueError):
            return None
        cached = _topseed_profile_cache.get(candidate_id)
        if cached is not None:
            return (candidate_id, cached) if cached else None
        future = _topseed_profile_futures.get(candidate_id)
        if future is not None:
            if not future.done():
                return None
            _topseed_profile_futures.pop(candidate_id, None)
            try:
                result_type, bitmap = future.result()
            except Exception:
                result_type, bitmap = "error", None
            features = (
                TopSeedSelector.coverage_features_from_bitmap(
                    bitmap, maximum=topseed_selector.max_features
                )
                if result_type == "success" and bitmap
                else ()
            )
            _topseed_profile_cache[candidate_id] = features
            while len(_topseed_profile_cache) > min(
                65_536, topseed_selector.max_candidates
            ):
                _topseed_profile_cache.pop(next(iter(_topseed_profile_cache)))
            return (candidate_id, features) if features else None
        if (
            _topseed_profile_budget[0] <= 0
            or len(_topseed_profile_futures) + len(_frontier_futures)
            >= _aux_showmap_capacity
        ):
            return None
        _topseed_profile_budget[0] -= 1
        _topseed_profile_futures[candidate_id] = _aux_showmap_executor.submit(
            _aux_showmap, stable_path, "topseed"
        )
        return None

    def _frontier_score(path: str) -> float:
        try:
            h, stable_path, _content = object_store.import_path(path)
        except (OSError, ValueError):
            return 0.0
        cached = _frontier_cache.get(h)
        if cached is not None:
            return cached
        future = _frontier_futures.get(h)
        if future is not None:
            if not future.done():
                return 0.0
            _frontier_futures.pop(h, None)
            try:
                rtype, data = future.result()
            except Exception:
                rtype, data = "error", None
            if rtype != "success" or not data:
                _frontier_cache[h] = 0.0
                return 0.0
            score = 0.0
            edge_rows = data if isinstance(data, list) else enumerate(data)
            for edge, bits in edge_rows:
                if bits:
                    frequency = _edge_freq.get(edge, 0)
                    score += 1.0 / (frequency + 1)
                    _edge_freq[edge] = frequency + 1
            _frontier_cache[h] = score
            return score
        if (
            _frontier_budget[0] <= 0
            or len(_topseed_profile_futures) + len(_frontier_futures)
            >= _aux_showmap_capacity
        ):
            return 0.0  # 本轮 showmap 预算耗尽 → 暂记 0，下轮再算
        _frontier_budget[0] -= 1
        _frontier_futures[h] = _aux_showmap_executor.submit(
            _aux_showmap, stable_path, "frontier"
        )
        return 0.0

    shutdown_clean = True
    try:
        if query_service_command is not None:
            try:
                query_service_log = open(
                    os.path.join(symcc_dir, ".query_service.log"), "a"
                )
                query_service_process = subprocess.Popen(
                    query_service_command,
                    stdin=subprocess.DEVNULL,
                    stdout=query_service_log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                time.sleep(0.1)
                if query_service_process.poll() is not None:
                    raise RuntimeError(
                        f"query service exited {query_service_process.returncode}"
                    )
                print(
                    f"[Master] Async query service: {query_compute_slots} workers, "
                    f"store={query_store}"
                )
            except (OSError, RuntimeError) as error:
                print(
                    f"[Master] Async query service disabled: {error}",
                    file=sys.stderr,
                )
                if query_service_process is not None:
                    _terminate_query_service(
                        query_service_process,
                        timeout=1.0,
                    )
                query_service_process = None
                if query_service_log is not None:
                    try:
                        query_service_log.close()
                    except OSError:
                        pass
                    query_service_log = None
                if semantic_proposals is not None:
                    semantic_proposals.query_store = None

        auxiliary_compute_components = {
            "query": (
                query_compute_slots
                if query_service_process is not None
                else 0
            ),
            "result_admission": result_admission_jobs,
            "coverage": afl_coverage_bridge.compute_slots,
            "density": _density_profile_jobs,
            "aux_showmap": (
                _aux_showmap_jobs
                if _ksched or topseed_selector is not None
                else 0
            ),
        }
        auxiliary_compute_slots = sum(auxiliary_compute_components.values())
        print(
            "[Master] Auxiliary compute slots: "
            f"{auxiliary_compute_slots} "
            f"({json.dumps(auxiliary_compute_components, sort_keys=True)})",
            flush=True,
        )

        while not shutdown_requested:
            (
                query_service_process,
                query_service_log,
                query_service_exit,
            ) = _retire_exited_query_service(
                query_service_process,
                query_service_log,
                semantic_proposals,
            )
            if query_service_exit is not None:
                print(
                    "[Master] Async query service exited during the campaign: "
                    f"returncode={query_service_exit}; query-store proposals disabled",
                    file=sys.stderr,
                    flush=True,
                )
            afl_coverage_update = afl_coverage_bridge.poll()
            if afl_coverage_update.local_bitmap_changed:
                bitmap_version += 1
                bitmap_journal.record(
                    bitmap_version, coverage.consume_delta()
                )
            _publish_current_coverage()
            # 读取自适应控制：期望活跃 worker 数（限流，避免每轮 IO）
            _now = time.monotonic()
            # Coverage ingestion is independent of symbolic dispatch.  Fill
            # the bounded showmap pool with a fair new-input/retry split so a
            # poison retry cannot starve AFL discoveries and a continuously
            # growing AFL queue cannot starve durable failed observations.
            if _now - last_afl_coverage_poll >= queue_poll_interval:
                afl_coverage_bridge.schedule_fair()
                last_afl_coverage_poll = time.monotonic()
            if online_value_profiles is not None:
                timed_profile = online_value_profiles.publish(now=_now)
                if timed_profile is not None:
                    print(
                        "[Master] Published empirical-domain "
                        f"generation {timed_profile.version[:12]} "
                        f"({timed_profile.profile_count} profiles)",
                        flush=True,
                    )
            if _now - last_control_check > 1.0:
                if os.path.isfile(control_file):
                    # An experiment-level allocator also controls AFL process count;
                    # it therefore has precedence over the local symbolic controller.
                    max_active_workers = _read_active_workers()
                elif parallel_controller is not None:
                    max_active_workers = parallel_controller.recommend(
                        queue_depth=last_queue_depth,
                        busy_workers=len(active_workers),
                        now=_now,
                    )
                last_control_check = _now
                # 内存安全阀：超限时裁剪到低水位。整表 clear 会让
                # 数百万历史任务在同一时刻全部失去去重保护，导致长跑吞吐断崖。
                for _c, _nm in (
                    (processed_files, "processed_files"),
                    (processed_content_hashes, "processed_content_hashes"),
                    (grimoire_seen, "grimoire_seen"),
                    (_density_cache, "_density_cache"),
                    (_frontier_cache, "_frontier_cache"),
                    (query_candidate_seen, "query_candidate_seen"),
                    (file_generation, "file_generation"),
                ):
                    if len(_c) > MAX_DEDUP_ENTRIES:
                        removed = _trim_tracking_container(
                            _c, MAX_DEDUP_ENTRIES
                        )
                        print(
                            f"[Master] {_nm} 超过 {MAX_DEDUP_ENTRIES} 条，"
                            f"已渐进裁剪 {removed} 条以限制内存",
                            flush=True,
                        )
                if (
                    result_object_gc_interval > 0.0
                    and _now - last_result_object_gc >= result_object_gc_interval
                ):
                    gc_stats = _collect_hybrid_result_objects(
                        result_object_store,
                        protected_object_ids=result_admission.protected_object_ids(),
                        max_entries=result_object_gc_max_entries,
                        min_age_seconds=result_object_gc_grace,
                    )
                    if gc_stats["deleted"] or gc_stats["failed"]:
                        print(
                            "[Master] result-object GC: "
                            f"{json.dumps(gc_stats, sort_keys=True)}",
                            flush=True,
                        )
                    last_result_object_gc = _now
            if component_policy is not None:
                component_choices = component_policy.select(now=_now)
            if agentic_backends is not None:
                for aliases, online_hint in agentic_backends.drain():
                    for alias in aliases:
                        agentic_hints[alias] = online_hint
            if structured_agentic is not None:
                _consume_structured_decisions(structured_agentic.drain())
            if verified_proposals is not None and _now - last_proposal_scan >= 1.0:
                added_proposals = verified_proposals.scan()
                proposal_jobs = verified_proposals.claim_pending(
                    max(1, max_active_workers * 2)
                )
                for proposal in proposal_jobs:
                    file_generation.setdefault(proposal.candidate_path, 0)
                    symcc_feedback_queue.append((proposal.candidate_path, 0))
                if added_proposals or proposal_jobs:
                    print(
                        f"[Master] Verified proposals: +{added_proposals} "
                        f"ingested, {len(proposal_jobs)} queued",
                        flush=True,
                    )
                last_proposal_scan = _now
            if (
                os.path.isdir(query_candidate_dir)
                and _now - last_query_candidate_scan >= 0.5
            ):
                try:
                    for candidate_name in os.listdir(query_candidate_dir):
                        if candidate_name.startswith(".") or candidate_name.endswith(
                            ".query.json"
                        ):
                            continue
                        candidate = os.path.join(query_candidate_dir, candidate_name)
                        if candidate in query_candidate_seen or not os.path.isfile(
                            candidate
                        ):
                            continue
                        query_candidate_seen.add(candidate)
                        file_generation.setdefault(candidate, 0)
                        symcc_feedback_queue.append((candidate, 0))
                except OSError:
                    pass
                last_query_candidate_scan = _now
            if adaptive_policy is not None and adaptive_policy.structural_tasks.enabled:
                _structural_workers = list(range(1, max_active_workers + 1))
                _structural_force = set(
                    adaptive_policy.structural_tasks.worker_regions
                ) != set(_structural_workers)
                adaptive_policy.rebalance_structural_tasks(
                    _structural_workers, now=_now, force=_structural_force
                )
            # 快速写出产出统计（每 ~5s），供 run_hybrid 控制器及时响应
            if _now - last_stats_out > 5.0:
                _write_symcc_stats()
                last_stats_out = _now
            _shared_heartbeat_due = (
                _now - last_shared_heartbeat >= shared_heartbeat_interval
                and (
                    (shared_work_leases is not None and active_lease_fences)
                    or (
                        shared_target_leases is not None
                        and (active_target_leases or pending_target_leases)
                    )
                )
            )
            if _shared_heartbeat_due:
                heartbeat_stats = _heartbeat_fenced_leases(
                    shared_work_leases,
                    active_leases,
                    active_lease_fences,
                    shared_target_leases,
                    active_target_leases,
                    pending_target_leases,
                )
                heartbeat_failures = (
                    heartbeat_stats["work_failed"] + heartbeat_stats["target_failed"]
                )
                if heartbeat_failures:
                    print(
                        f"[Master] shared lease heartbeat failures: {heartbeat_stats}",
                        flush=True,
                    )
                last_shared_heartbeat = _now
            if (
                shared_work_leases is not None
                and len(active_workers) < max_active_workers
            ):
                existing_items = {repr(item) for item in carried_items}
                stolen = 0
                for payload in shared_work_leases.recover_expired(
                    limit=max(1, max_active_workers)
                ):
                    item = _recoverable_work_item_from_lease_payload(payload)
                    if item is None:
                        continue
                    marker = repr(item)
                    if marker in existing_items:
                        continue
                    carried_items.append(item)
                    existing_items.add(marker)
                    stolen += 1
                if stolen:
                    carried_paths = {item[0] for item in carried_items}
                    print(
                        f"[Master] Multi-master lease recovery queued "
                        f"{stolen} expired work items",
                        flush=True,
                    )
            # 扫描 GRIMOIRE 高价值馈送目录，新文件注入反馈队列（限流 ~3s）
            if grimoire_feed_dir and _now - last_grimoire_scan > 3.0:
                last_grimoire_scan = _now
                _gnew = 0
                try:
                    for gname in os.listdir(grimoire_feed_dir):
                        gp = os.path.join(grimoire_feed_dir, gname)
                        if gp in grimoire_seen or not os.path.isfile(gp):
                            continue
                        grimoire_seen.add(gp)
                        # 代数记为 0（视作新种子级）；结构有效 → concolic 深挖
                        file_generation.setdefault(gp, 0)
                        symcc_feedback_queue.append((gp, 0))
                        _gnew += 1
                except OSError:
                    pass
                if _gnew:
                    print(
                        f"[Master] GRIMOIRE feed: +{_gnew} structured inputs -> "
                        f"SymCC ({len(grimoire_seen)} total)",
                        flush=True,
                    )
            # 合并输入源：SymCC 反馈用例优先，然后是 AFL queue 的新文件
            # 提取反馈队列：(path, generation) 元组
            pending_feedback_tuples = list(symcc_feedback_queue)
            symcc_feedback_queue.clear()
            # 按代数降序排列：深度优先，优先探索最新一代的输出
            pending_feedback_tuples.sort(key=lambda x: x[1], reverse=True)
            pending_feedback = [p for p, _g in pending_feedback_tuples]
            # 记录最大代数
            for _p, _g in pending_feedback_tuples:
                if _g > max_generation_reached:
                    max_generation_reached = _g

            # 扫描 AFL queue 的条件（避免 worker 全忙时忙等式重复 scandir）：
            #  - 有足够反馈用例可分发 → 跳过扫描；
            #  - 有空闲 worker 需要喂 → 立即扫描（响应性）；
            #  - 否则按 SYMCC_QUEUE_POLL_INTERVAL 节流（默认最多 2 次/秒）。
            idle_worker_waiting = len(active_workers) < max_active_workers
            scan_due = (_now - last_scan_time) >= queue_poll_interval
            # 阈值/批量都以"活跃" worker 数为准：停泊的 worker 不消费候选，
            # 用 num_workers 会在停泊时过度取用并过度扫描。
            if pending_feedback and len(pending_feedback) >= max_active_workers:
                new_inputs = []
            elif not (idle_worker_waiting or scan_due):
                new_inputs = []  # 节流：worker 都在忙且刚扫过 → 跳过
            else:
                _t0 = time.monotonic()
                _frontier_budget[0] = (
                    16  # 重置本轮前沿 showmap 预算（见 _frontier_score）
                )
                new_inputs = afl_config.best_new_testcases(
                    processed_files,
                    batch_size=max_active_workers * 4,
                    analyzed_hashes=processed_content_hashes,
                    edge_yield=(
                        None
                        if component_choices.get("seed") == "fifo"
                        else _get_edge_yield()
                    ),
                    frontier_fn=(
                        _frontier_score
                        if (_ksched or component_choices.get("seed") == "frontier")
                        else None
                    ),
                    score_fn=(
                        _adaptive_score
                        if (
                            adaptive_policy is not None
                            and component_choices.get("seed") == "contextual"
                        )
                        else None
                    ),
                )
                if component_choices.get("seed") == "fifo":
                    new_inputs.sort(
                        key=lambda path: afl_config._file_cache.get(path, {}).get(
                            "name", path
                        )
                    )
                # Measure the busy-path poll interval from scan completion.
                # A queue walk can itself consume the full scan budget; using
                # the pre-scan timestamp makes the next iteration immediately
                # due and turns the intended throttle into continuous scans.
                last_scan_time = time.monotonic()
                # AFL queue entries are coverage-producing observations, not
                # merely symbolic work items.  Merge them before dispatch so a
                # worker cannot report an AFL-known edge as a SymCC discovery.
                afl_coverage_bridge.schedule(new_inputs)
                afl_coverage_update = afl_coverage_bridge.poll()
                if afl_coverage_update.local_bitmap_changed:
                    bitmap_version += 1
                    bitmap_journal.record(
                        bitmap_version, coverage.consume_delta()
                    )
                    _publish_current_coverage(changed=True)
                # Never symbolically execute an AFL queue object before its
                # exact stable identity has contributed to the authoritative
                # AFL+SymCC union. Failed showmap entries stay unprocessed and
                # are naturally retried by the next queue scan.
                new_inputs = [
                    path
                    for path in new_inputs
                    if afl_coverage_bridge.is_synchronized(path)
                ]
                # AFL 种子代数为 0
                for inp in new_inputs:
                    if inp not in file_generation:
                        file_generation[inp] = 0
                if _prof:
                    _t_scan += time.monotonic() - _t0
                    _n_scan += 1

            # 动态工作窃取：整-种子项不足以填满活跃 worker 时，把种子细分为不相交字节
            # 区间子项填补空闲产能（多样性模式；否则等价于原来的整-种子列表）。上一轮
            # 未派发完的工作项（carried_items）优先结转到本轮队首，且其路径不再参与本轮
            # 细分，避免重复；剩余待填产能 = 活跃 worker 数 - 已结转项数。
            _profile_budget[0] = 8  # 重置本轮冷剖析预算（见 _profile_density）
            _src = [
                p for p in (pending_feedback + new_inputs) if p not in carried_paths
            ]
            _target_branches: dict[str, int] = {}
            _target_actions: dict[str, tuple[tuple[int, str], ...]] = {}
            if verified_proposals is not None:
                for proposal_path in _src:
                    proposal = verified_proposals.for_path(proposal_path)
                    if proposal is not None and proposal.target_branch:
                        _target_branches[proposal_path] = proposal.target_branch
            if dpor_explorer is not None and idle_worker_waiting:
                dpor_jobs = dpor_explorer.pop_pending(max(1, max_active_workers * 2))
                dpor_replay_items.extend(
                    _scheduled_work_item(job.path, job.prefix)
                    for job in dpor_jobs
                    if os.path.isfile(job.path)
                )
            # Reserve a bounded high-priority lane for explicit open prefixes even while
            # AFL keeps producing fresh seeds. This prevents the symbolic side from
            # starving its execution DAG without letting replay monopolize workers.
            _effective_replay_share = (
                0.0
                if component_choices.get("replay") == "fresh"
                else max(0.5, dag_replay_share)
                if component_choices.get("replay") == "prefix"
                else dag_replay_share
            )
            if (
                _src
                and not carried_items
                and adaptive_policy is not None
                and idle_worker_waiting
                and _effective_replay_share > 0.0
            ):
                _dag_budget = max(
                    1, math.ceil(max_active_workers * _effective_replay_share)
                )
                _dag_jobs = adaptive_policy.target_candidates(
                    _dag_budget,
                    cooldown=replay_cooldown,
                    external_admission=(
                        _admit_shared_target
                        if shared_target_leases is not None
                        else None
                    ),
                )
                _dag_paths = {job.path for job in _dag_jobs}
                _src = [job.path for job in _dag_jobs] + [
                    path for path in _src if path not in _dag_paths
                ]
                _target_branches.update(
                    {job.path: job.target_branch for job in _dag_jobs}
                )
                _target_actions.update(
                    {job.path: job.actions for job in _dag_jobs if job.actions}
                )
            # S²F 风格的“非休眠”机制：新 cross-seed 耗尽时，从有覆盖收益或高求解难度的
            # 历史路径中按冷却时间重放，并由策略 portfolio 尝试不同求解画像。
            if (
                not _src
                and not carried_items
                and adaptive_policy is not None
                and idle_worker_waiting
            ):
                _replay_jobs = adaptive_policy.replay_candidates(
                    min(max_active_workers, 8),
                    cooldown=replay_cooldown,
                    external_admission=(
                        _admit_shared_target
                        if shared_target_leases is not None
                        else None
                    ),
                )
                _src = [job.path for job in _replay_jobs]
                _target_branches.update(
                    {
                        job.path: job.target_branch
                        for job in _replay_jobs
                        if job.target_branch
                    }
                )
                _target_actions.update(
                    {job.path: job.actions for job in _replay_jobs if job.actions}
                )
            _src = _rank_source_paths(_src)
            _topseed_items: list[tuple] = []
            if (
                topseed_selector is not None
                and idle_worker_waiting
                and not topseed_item_proposals
            ):
                _topseed_profile_budget[0] = _topseed_profile_limit
                for candidate_path in _src:
                    profile = _topseed_profile(candidate_path)
                    if profile is None:
                        continue
                    candidate_id, candidate_coverage = profile
                    topseed_selector.admit(
                        candidate_id,
                        candidate_path,
                        candidate_coverage,
                    )
                historical_paths = [
                    path
                    for path in topseed_selector.historical_paths(
                        limit=max(8, max_active_workers * 8)
                    )
                    if os.path.isfile(path)
                ]
                topseed_proposal = topseed_selector.propose(
                    _src + historical_paths
                )
                if topseed_proposal is not None:
                    _src = [
                        path for path in _src
                        if path != topseed_proposal.path
                    ]
                    _topseed_items = _build_work_items(
                        [topseed_proposal.path],
                        1,
                        False,
                        1,
                        target_branches=_target_branches,
                        target_actions=_target_actions,
                    )
                    if _topseed_items:
                        topseed_item_proposals[id(_topseed_items[0])] = (
                            topseed_proposal.token
                        )
            _remaining_target = max(1, max_active_workers - len(carried_items))
            _splitter = component_choices.get("splitter", "whole")
            _dpor_items = dpor_replay_items
            dpor_replay_items = []
            work_queue = (
                _topseed_items
                + _dpor_items
                + carried_items
                + _build_work_items(
                    _src,
                    _remaining_target,
                    _splitter != "whole",
                    _focus_parts,
                    density_fn=_profile_density if _splitter == "density" else None,
                    target_branches=_target_branches,
                    target_actions=_target_actions,
                )
            )
            if live_initializer is not None and live_program is not None:
                executable_items: list[tuple] = []
                for item in work_queue:
                    topseed_item_token = topseed_item_proposals.pop(
                        id(item), ""
                    )
                    (
                        item_path,
                        item_focus,
                        item_target,
                        item_actions,
                        item_schedule,
                        item_continuation,
                    ) = _work_item_parts(item)
                    if item_continuation is None:
                        try:
                            snapshot = stable_regular_file_snapshot(
                                item_path,
                                max_bytes=max_live_input_bytes,
                                retain_content=True,
                            )
                            assert snapshot.content is not None
                            checkpoint = live_initializer.create(
                                live_program,
                                input_bytes=snapshot.content,
                                target_branch=item_target,
                            )
                            item_continuation = (
                                live_state_store.restore_continuation(
                                    checkpoint
                                ).descriptor.to_mapping()
                                if live_state_store is not None
                                else None
                            )
                        except (OSError, TypeError, ValueError) as exc:
                            if topseed_item_token and topseed_selector is not None:
                                topseed_selector.discard(topseed_item_token)
                            print(
                                f"[Master] skipping live-state seed "
                                f"{item_path!r}: {exc}",
                                flush=True,
                            )
                            continue
                    executable_item = (
                        item_path,
                        item_focus,
                        item_target,
                        item_actions,
                        item_schedule,
                        item_continuation,
                    )
                    executable_items.append(executable_item)
                    if topseed_item_token:
                        topseed_item_proposals[id(executable_item)] = (
                            topseed_item_token
                        )
                work_queue = executable_items
            last_queue_depth = len(work_queue) + len(symcc_feedback_queue)
            if state_coordinator is not None and work_queue:
                state_coordinator.recover_expired()
                work_queue = state_coordinator.order_work_items(
                    work_queue, active_worker_count=max_active_workers
                )

            # 交替处理 READY 和 RESULT 消息，避免单方向阻塞
            work_idx = 0
            any_progress = True

            def _recover_active_dispatch(
                wr: int,
                dispatch_token: str,
                *,
                source: str,
                park_worker: bool,
            ) -> bool:
                recovery = _rollback_owned_dispatch(
                    wr,
                    dispatch_token,
                    active_dispatches,
                    active_work_items,
                    active_workers,
                    active_strategies,
                    active_hashes,
                    active_leases,
                    active_lease_fences,
                    active_target_leases,
                    active_state_tasks,
                    active_schedule_prefixes,
                    active_agentic_tasks,
                    active_component_choices,
                )
                if recovery is None:
                    stats.quarantine_result(f"{source}-recovery-invariant")
                    print(
                        "[Master] failed to recover dispatch: "
                        f"worker={wr} source={source} missing owned state",
                        flush=True,
                    )
                    return False
                active_parallel_cohorts.pop(wr, None)
                recovered_topseed = active_topseed_runs.pop(wr, "")
                if recovered_topseed and topseed_selector is not None:
                    topseed_selector.observe(
                        recovered_topseed, (), failed=True
                    )
                    _save_topseed()
                dispatch_generation_gate.retire(wr)
                recovered_item, rollback = recovery
                _save_state_tasks()
                recovery_state = _enqueue_dispatch_recovery(
                    recovered_item,
                    work_queue,
                    work_idx,
                    dispatch_protocol_attempts,
                    dispatch_protocol_retries,
                    deferred_dispatches,
                    worker=wr,
                )
                disposition = str(recovery_state["disposition"])
                if disposition == "deferred":
                    stats.deferred_dispatches += 1
                else:
                    stats.requeued_dispatches += 1
                    if disposition == "requeued-unpersisted":
                        print(
                            "[Master] WARNING: deferred work could not be "
                            "persisted; requeuing in memory: "
                            f"{str(recovery_state['recovery_id'])[:12]}",
                            flush=True,
                        )
                if park_worker:
                    retired_dispatch_gate.park(wr, dispatch_token)
                    idle_ranks.discard(wr)
                    stats.watchdog_timeouts += 1
                else:
                    idle_ranks.add(wr)
                print(
                    "[Master] recovered dispatch: "
                    f"worker={wr} source={source} action={disposition} "
                    f"attempt={recovery_state['attempt']} "
                    f"rollback_failed={rollback['failed']}",
                    flush=True,
                )
                return True

            def _dispatch_to(wr: int, item: tuple) -> bool:
                # item = (种子路径, focus 区间或 None, target)。输入由 CAS digest 标识，
                # worker 首次缺失对象时同时发送内容；覆盖状态使用版本化稀疏 delta。
                nonlocal dispatch_sequence
                topseed_proposal_token = topseed_item_proposals.get(
                    id(item), ""
                )
                (
                    input_file,
                    item_focus,
                    target_branch,
                    s2f_actions,
                    schedule_prefix,
                    continuation,
                ) = _work_item_parts(item)
                if (
                    wr in preparing_dispatches
                    or wr in active_dispatches
                    or wr in committing_dispatches
                    or retired_dispatch_gate.is_parked(wr)
                ):
                    raise RuntimeError(
                        f"worker {wr} already owns a dispatch transaction"
                    )
                dispatch_sequence += 1
                dispatch_token = _make_dispatch_token(
                    dispatch_epoch, wr, dispatch_sequence
                )
                dispatch_tx = _DispatchReservationTransaction(wr, dispatch_token)
                preparing_dispatches[wr] = dispatch_tx

                def _reject_dispatch() -> bool:
                    preparing_dispatches.pop(wr, None)
                    rejected_topseed = topseed_item_proposals.pop(id(item), "")
                    if rejected_topseed and topseed_selector is not None:
                        topseed_selector.discard(rejected_topseed)
                    rollback = dispatch_tx.rollback()
                    if rollback["failed"]:
                        print(
                            "[Master] pre-dispatch rollback failures: "
                            f"worker={wr} {rollback}",
                            flush=True,
                        )
                    return False

                # Establish one exact bounded byte identity, or use the
                # checkpoint identity of a self-contained continuation, before
                # strategy, lease, or state-task decisions create side effects.
                try:
                    (
                        admitted_object_id,
                        dispatch_identity,
                        admitted_content,
                        admitted_input_bytes,
                    ) = _admit_hybrid_master_work(
                        object_store,
                        afl_config._file_cache,
                        input_file,
                        continuation,
                    )
                except (OSError, ValueError) as error:
                    print(
                        "[Master] rejected unstable or oversized input: "
                        f"{input_file!r}: {error}",
                        flush=True,
                    )
                    return _reject_dispatch()
                _ch = admitted_object_id

                solver_component = component_choices.get("solver", "learned")
                if solver_component == "exact":
                    strategy = 0
                elif solver_component == "diverse":
                    strategy = (wr + target_branch) % max(
                        1, len(SYMCC_STRATEGY_PROFILES)
                    )
                else:
                    strategy = (
                        adaptive_policy.select_strategy(target_branch)
                        if adaptive_policy is not None
                        else 0
                    )
                if (
                    any(action == "sample" for _branch, action in s2f_actions)
                    and len(SYMCC_STRATEGY_PROFILES) > 6
                ):
                    strategy = 6
                use_focus_set = (
                    compact_focus_enabled
                    and focus_set_str
                    and item_focus is None
                    and target_branch == 0
                )
                work_message = {
                    "path": input_file,
                    "dispatch_token": dispatch_token,
                    "focus_bytes": item_focus
                    if item_focus is not None
                    else ("" if use_focus_set else focus_bytes_str),
                    "strategy": strategy,
                    "target_branch": target_branch,
                }
                proposal = (
                    verified_proposals.for_path(input_file)
                    if verified_proposals is not None
                    else None
                )
                if proposal is not None:
                    work_message["proposal_id"] = proposal.proposal_id
                    work_message["proposal_kind"] = proposal.kind
                    if proposal.target_branch:
                        work_message["target_branch"] = proposal.target_branch
                if s2f_actions:
                    work_message["s2f_actions"] = s2f_actions
                target_contract_branch = _normalize_branch_id(
                    work_message.get("target_branch", 0)
                )
                target_contract_actions = _normalize_s2f_actions(
                    work_message.get("s2f_actions", ())
                )
                target_contract_group = _target_group(
                    target_contract_branch, target_contract_actions
                )
                if use_focus_set:
                    work_message["focus_set"] = focus_set_str
                if dpor_explorer is not None:
                    work_message["schedule_enabled"] = True
                    work_message["schedule_preload"] = dpor_preload
                    work_message["schedule_prefix"] = schedule_prefix
                if continuation is not None:
                    work_message["continuation"] = continuation
                    work_message["continuation_id"] = (
                        LiveContinuationDescriptor.from_mapping(
                            continuation
                        ).checkpoint_id()
                    )
                static_target_intervals = static_branch_dependencies.get(
                    target_contract_branch, ()
                )
                if static_target_intervals and item_focus is None:
                    target_offsets: list[int] = []
                    for lower, upper in static_target_intervals:
                        if upper - lower <= 4096:
                            target_offsets.extend(range(lower, upper + 1))
                    target_focus = os.path.join(
                        symcc_dir, f".static_focus_{target_contract_branch:x}"
                    )
                    if target_offsets and _write_compact_focus_set(
                        target_focus, target_offsets, max_entries=65536
                    ):
                        work_message["focus_set"] = target_focus
                if (
                    adaptive_policy is not None
                    and adaptive_policy.structural_tasks.enabled
                ):
                    work_message["task_region"] = (
                        adaptive_policy.structural_task_region(
                            input_file, target_contract_branch
                        )
                    )
                agentic_dispatch_task = {
                    "schema": 1,
                    "input_path": input_file,
                    "sha256": _ch or "",
                    "strategy": strategy,
                    "target_branch": target_contract_branch,
                    "focus_bytes": work_message.get("focus_bytes", ""),
                    "queue_remaining": max(0, len(work_queue) - work_idx),
                }
                structured_decision_id = ""
                structured_hint_applied = False
                if structured_agentic is not None and proposal is not None:
                    structured_decision_id = (
                        structured_agentic.decision_for_candidate(
                            proposal.candidate_sha256
                        )
                    )
                if builtin_agentic is not None:
                    builtin_hint = builtin_agentic.suggest(agentic_dispatch_task)
                    if builtin_hint:
                        apply_hint(
                            work_message, builtin_hint, len(SYMCC_STRATEGY_PROFILES)
                        )
                if semantic_fallback is not None:
                    semantic_hint = semantic_fallback.suggest(
                        input_file,
                        sha256=_ch or "",
                        target_branch=_normalize_branch_id(
                            work_message.get("target_branch", 0)
                        ),
                    )
                    if semantic_hint:
                        apply_hint(
                            work_message, semantic_hint, len(SYMCC_STRATEGY_PROFILES)
                        )
                hint = agentic_hints.get(_ch or "") or agentic_hints.get(input_file)
                if hint:
                    apply_hint(work_message, hint, len(SYMCC_STRATEGY_PROFILES))
                    hint_decision_id = (
                        structured_agentic_decisions.get(_ch or "") or
                        structured_agentic_decisions.get(input_file, "")
                    )
                    if hint_decision_id:
                        structured_decision_id = hint_decision_id
                        structured_hint_applied = True
                        for alias, decision_id in list(
                                structured_agentic_decisions.items()):
                            if decision_id == hint_decision_id:
                                structured_agentic_decisions.pop(alias, None)
                                agentic_hints.pop(alias, None)
                if proposal is not None and proposal.target_branch:
                    # Validation targets are part of the proposal contract and
                    # cannot be replaced by a later heuristic hint.
                    work_message["target_branch"] = proposal.target_branch
                if target_contract_group:
                    # Directed scheduler/proposal work is admitted as one
                    # target group. Hints may tune strategy/focus, but cannot
                    # replace or extend the leased branch contract.
                    _enforce_target_contract(
                        work_message,
                        target_contract_branch,
                        target_contract_actions,
                    )
                work_message["target_branch"] = _normalize_branch_id(
                    work_message.get("target_branch", 0)
                )
                if (
                    adaptive_policy is not None
                    and adaptive_policy.structural_tasks.enabled
                ):
                    work_message["task_region"] = (
                        adaptive_policy.structural_task_region(
                            input_file,
                            _normalize_branch_id(work_message.get("target_branch", 0)),
                        )
                    )
                s2f_actions = _normalize_s2f_actions(
                    work_message.get("s2f_actions", s2f_actions)
                )
                if s2f_actions:
                    work_message["s2f_actions"] = s2f_actions
                else:
                    work_message.pop("s2f_actions", None)
                if (
                    any(action == "sample" for _branch, action in s2f_actions)
                    and len(SYMCC_STRATEGY_PROFILES) > 6
                ):
                    work_message["strategy"] = 6
                target_group = _target_group(
                    work_message.get("target_branch", 0), s2f_actions
                )
                if adaptive_policy is not None and target_group:
                    dispatch_target = _normalize_branch_id(
                        work_message.get("target_branch", 0)
                    )
                    dispatch_actions = s2f_actions
                    dispatch_tx.defer(
                        "target-assignment",
                        lambda target=dispatch_target, actions=dispatch_actions: (
                            adaptive_policy.release_target_assignment(target, actions)
                        ),
                    )
                target_lease: tuple[tuple[int, ...], str] | None = None
                if shared_target_leases is not None and target_group:
                    target_fence = pending_target_leases.pop(target_group, "")
                    try:
                        if target_fence and not shared_target_leases.heartbeat_group(
                            target_group, target_fence
                        ):
                            target_fence = ""
                        if not target_fence:
                            target_fence = (
                                shared_target_leases.claim_group(
                                    target_group,
                                    {
                                        "path": input_file,
                                        "sha256": dispatch_identity,
                                        "target_branch": int(
                                            work_message.get("target_branch", 0) or 0
                                        ),
                                        "s2f_actions": [
                                            [branch, action]
                                            for branch, action in s2f_actions
                                        ],
                                    },
                                    owner=shared_master_id,
                                    worker=wr,
                                )
                                or ""
                            )
                    except OSError:
                        target_fence = ""
                    if not target_fence:
                        return _reject_dispatch()
                    target_lease = (target_group, target_fence)
                    dispatch_tx.defer(
                        "target-lease",
                        lambda lease=target_lease: _release_shared_target(lease),
                    )
                if parameter_policy is not None:
                    final_strategy = max(
                        0, int(work_message.get("strategy", strategy) or 0)
                    )
                    base_profile = (
                        SYMCC_STRATEGY_PROFILES[final_strategy]
                        if final_strategy < len(SYMCC_STRATEGY_PROFILES)
                        else SYMCC_STRATEGY_PROFILES[0]
                    )
                    parameter_assignment = parameter_policy.select(
                        base_profile,
                        context={
                            "target_branch": int(
                                work_message.get("target_branch", 0) or 0
                            ),
                            "worker": wr,
                            "task_region": int(work_message.get("task_region", 0) or 0),
                            "input_bytes": admitted_input_bytes,
                        },
                    )
                    work_message["parameter_token"] = parameter_assignment.token
                    work_message["parameter_overrides"] = parameter_assignment.overrides
                    dispatch_tx.defer(
                        "parameter-assignment",
                        lambda token=parameter_assignment.token: (
                            parameter_policy.abandon(token)
                        ),
                    )
                if algorithm_policy is not None:
                    algorithm_assignment = algorithm_policy.select(
                        input_file,
                        input_bytes=admitted_input_bytes,
                        target_branch=int(work_message.get("target_branch", 0) or 0),
                        worker=wr,
                        preferred_sequence=(
                            offline_policy.recommend()
                            if offline_policy is not None
                            else ""
                        ),
                    )
                    merged_overrides = dict(work_message.get("parameter_overrides", {}))
                    merged_overrides.update(algorithm_assignment.overrides)
                    work_message["parameter_overrides"] = merged_overrides
                    dispatch_tx.defer(
                        "algorithm-assignment",
                        lambda token=algorithm_assignment.token: (
                            algorithm_policy.abandon(token)
                        ),
                        lambda token=algorithm_assignment.token: (
                            algorithm_policy.discard(token)
                        ),
                    )
                state_task_id = ""
                if state_coordinator is not None:
                    final_state_item = (
                        input_file,
                        work_message.get("focus_bytes", "") or None,
                        _normalize_branch_id(work_message.get("target_branch", 0)),
                        s2f_actions,
                        tuple(
                            normalize_schedule_prefix(
                                work_message.get("schedule_prefix", ())
                            )
                        ),
                        continuation,
                    )
                    state_meta = state_coordinator.describe_item(
                        final_state_item,
                        sha256=dispatch_identity,
                        active_worker_count=max_active_workers,
                    )
                    state_task_id = str(state_meta["state_task_id"])
                    work_message["state_task_id"] = state_task_id
                    work_message["state_shard"] = int(state_meta["state_shard"])
                    work_message["state_owner"] = int(state_meta["state_owner"])
                    if not state_coordinator.lease(state_task_id, wr):
                        return _reject_dispatch()
                    dispatch_tx.defer(
                        "state-task",
                        lambda task=state_task_id: state_coordinator.abandon(
                            task, worker=wr
                        ),
                        lambda task=state_task_id: state_coordinator.discard(
                            task, worker=wr
                        ),
                    )
                    agentic_dispatch_task["state_task_id"] = state_task_id
                    agentic_dispatch_task["state_shard"] = work_message["state_shard"]
                agentic_dispatch_task["strategy"] = int(
                    work_message.get("strategy", strategy) or 0
                )
                agentic_dispatch_task["target_branch"] = int(
                    work_message.get("target_branch", 0) or 0
                )
                agentic_dispatch_task["focus_bytes"] = work_message.get(
                    "focus_bytes", ""
                )
                if structured_decision_id:
                    agentic_dispatch_task["structured_decision_id"] = (
                        structured_decision_id
                    )
                if _ch is not None:
                    work_message["sha256"] = _ch
                if object_transport_enabled and admitted_object_id is not None:
                    work_message["object_id"] = admitted_object_id
                    if admitted_object_id not in worker_objects[wr]:
                        work_message["object_content"] = admitted_content
                full_bitmap = bytes(coverage.data or bytearray(_AFL_MAP_SIZE))
                if shared_coverage_snapshot_enabled:
                    work_message["coverage_snapshot_path"] = (
                        shared_coverage_snapshot_path
                    )
                worker_bitmap_version = worker_bitmap_versions.get(wr, -1)
                if bitmap_deltas_enabled:
                    work_message.update(
                        bitmap_journal.payload(
                            worker_bitmap_version, bitmap_version, full_bitmap
                        )
                    )
                else:
                    work_message["bitmap_version"] = bitmap_version
                    if worker_bitmap_version < bitmap_version:
                        work_message["bitmap_full"] = full_bitmap
                current_profile = (
                    online_value_profiles.current
                    if online_value_profiles is not None
                    else None
                )
                work_message.update(
                    value_profile_update_payload(
                        current_profile, worker_profile_versions.get(wr, "")
                    )
                )
                lease_id = ""
                lease_fence = ""
                lease_payload: dict[str, typing.Any] | None = None
                if work_leases is not None or shared_work_leases is not None:
                    try:
                        lease_payload = {
                            "path": input_file,
                            "focus_bytes": str(
                                work_message.get("focus_bytes", "") or ""
                            ),
                            "target_branch": _normalize_branch_id(
                                work_message.get("target_branch", 0)
                            ),
                            "strategy": max(
                                0, int(work_message.get("strategy", strategy) or 0)
                            ),
                            "sha256": dispatch_identity,
                            "s2f_actions": [
                                [branch, action] for branch, action in s2f_actions
                            ],
                            "schedule_prefix": list(
                                normalize_schedule_prefix(
                                    work_message.get("schedule_prefix", ())
                                )
                            ),
                            "continuation": work_message.get("continuation"),
                            "parameter_token": str(
                                work_message.get("parameter_token", "") or ""
                            ),
                            "parameter_overrides": dict(
                                work_message.get("parameter_overrides", {})
                            ),
                        }
                    except (TypeError, ValueError):
                        lease_payload = {
                            "path": input_file,
                            "focus_bytes": "",
                            "target_branch": 0,
                            "strategy": int(strategy),
                            "sha256": dispatch_identity,
                            "parameter_token": "",
                            "parameter_overrides": {},
                            "schedule_prefix": [],
                        }
                    lease_id = WorkLeaseJournal.work_id(lease_payload)
                if shared_work_leases is not None and lease_payload is not None:
                    lease_fence = (
                        shared_work_leases.claim(
                            lease_id,
                            lease_payload,
                            owner=shared_master_id,
                            worker=wr,
                        )
                        or ""
                    )
                    if not lease_fence:
                        return _reject_dispatch()
                    work_message["lease_fence"] = lease_fence
                    dispatch_tx.defer(
                        "shared-work-lease",
                        lambda work=lease_id, fence=lease_fence: (
                            shared_work_leases.abandon(work, fence)
                        ),
                    )
                if work_leases is not None and lease_payload is not None:
                    lease_id = WorkLeaseJournal.work_id(lease_payload)
                    work_message["lease_id"] = lease_id
                    if not work_leases.lease(lease_id, lease_payload, worker=wr):
                        return _reject_dispatch()
                    dispatch_tx.defer(
                        "local-work-lease",
                        lambda work=lease_id: work_leases.abandon(work, worker=wr),
                    )
                elif lease_id:
                    work_message["lease_id"] = lease_id
                if adaptive_policy is not None:
                    adaptive_policy.reserve_worker_assignment(
                        wr,
                        input_file,
                        focus=str(work_message.get("focus_bytes", "") or ""),
                        target_branch=int(work_message.get("target_branch", 0) or 0),
                    )
                    dispatch_tx.defer(
                        "worker-assignment",
                        lambda: adaptive_policy.release_worker_assignment(wr),
                        lambda: adaptive_policy.discard_worker_assignment(wr),
                    )
                if proposal is not None and verified_proposals is not None:
                    verified_proposals.mark_dispatched(proposal.proposal_id)
                    dispatch_tx.defer(
                        "verified-proposal",
                        lambda proposal_id=proposal.proposal_id: (
                            verified_proposals.abandon_dispatch(
                                proposal_id, executed=False
                            )
                        ),
                        lambda proposal_id=proposal.proposal_id: (
                            verified_proposals.abandon_dispatch(
                                proposal_id, executed=True
                            )
                        ),
                    )
                if structured_hint_applied and structured_agentic is not None:
                    structured_agentic.record_hint_selection(
                        structured_decision_id,
                        agentic_dispatch_task,
                    )
                try:
                    comm.send(work_message, dest=wr, tag=TAG_WORK)
                except Exception:
                    preparing_dispatches.pop(wr, None)
                    failed_topseed = topseed_item_proposals.pop(id(item), "")
                    if failed_topseed and topseed_selector is not None:
                        topseed_selector.discard(failed_topseed)
                    rollback = dispatch_tx.rollback()
                    if rollback["failed"]:
                        print(
                            f"[Master] send rollback failures: worker={wr} {rollback}",
                            flush=True,
                        )
                    raise
                dispatch_tx.mark_dispatched()
                preparing_dispatches.pop(wr, None)
                active_dispatches[wr] = dispatch_tx
                active_work_items[wr] = tuple(item)
                active_workers[wr] = input_file
                active_strategies[wr] = int(work_message.get("strategy", strategy))
                active_hashes[wr] = dispatch_identity
                if lease_id:
                    active_leases[wr] = lease_id
                if lease_fence:
                    active_lease_fences[wr] = lease_fence
                if target_lease is not None:
                    active_target_leases[wr] = target_lease
                if state_task_id:
                    active_state_tasks[wr] = state_task_id
                active_schedule_prefixes[wr] = tuple(
                    normalize_schedule_prefix(work_message.get("schedule_prefix", ()))
                )
                active_agentic_tasks[wr] = {
                    **agentic_dispatch_task,
                    "strategy": active_strategies[wr],
                    "target_branch": _normalize_branch_id(
                        work_message.get("target_branch", 0)
                    ),
                    "focus_bytes": work_message.get("focus_bytes", ""),
                    "task_region": int(work_message.get("task_region", 0)),
                    "s2f_actions": [[branch, action] for branch, action in s2f_actions],
                }
                active_component_choices[wr] = dict(component_choices)
                if parallel_controller is not None:
                    active_parallel_cohorts[wr] = parallel_controller.cohort
                if topseed_proposal_token and topseed_selector is not None:
                    topseed_item_proposals.pop(id(item), None)
                    try:
                        active_topseed_runs[wr] = topseed_selector.commit(
                            topseed_proposal_token
                        )
                        _save_topseed()
                    except ValueError as error:
                        topseed_selector.discard(topseed_proposal_token)
                        print(
                            f"[Master] TopSeed dispatch commit rejected: {error}",
                            file=sys.stderr,
                            flush=True,
                        )
                processed_files.add(input_file)
                if _ch is not None:
                    processed_content_hashes.add(_ch)
                    if hash_ledger is not None:
                        hash_ledger.add(_ch)
                return True

            while any_progress:
                any_progress = False

                # 排空所有 READY 到 idle 集合（一并消费，避免遗留缓冲）
                _rstatus = MPI.Status()
                while comm.iprobe(
                    source=MPI.ANY_SOURCE, tag=TAG_READY, status=_rstatus
                ):
                    wr = _rstatus.Get_source()
                    ready = comm.recv(source=wr, tag=TAG_READY)
                    if retired_dispatch_gate.is_parked(wr):
                        retired_status = retired_dispatch_gate.observe_ready(wr, ready)
                        if retired_status != "recovered":
                            stats.quarantine_ready(f"watchdog-{retired_status}")
                            print(
                                "[Master] quarantined READY for parked "
                                f"worker={wr} status={retired_status}",
                                flush=True,
                            )
                            any_progress = True
                            continue
                        stats.watchdog_worker_recoveries += 1
                        ready_status = "idle"
                        print(
                            f"[Master] watchdog worker recovered: worker={wr}",
                            flush=True,
                        )
                    else:
                        ready_tx = active_dispatches.get(wr)
                        active_consistent = (ready_tx is None) == (
                            wr not in active_workers
                        )
                        expected_ready_token = (
                            ready_tx.dispatch_token
                            if (ready_tx is not None and active_consistent)
                            else ""
                        )
                        ready_status = (
                            dispatch_generation_gate.observe_ready(
                                wr, expected_ready_token, ready
                            )
                            if active_consistent
                            else "unowned"
                        )
                    if ready_status not in {"current", "idle"}:
                        stats.quarantine_ready(ready_status)
                        print(
                            "[Master] quarantined non-current READY: "
                            f"worker={wr} status={ready_status}",
                            flush=True,
                        )
                        any_progress = True
                        continue
                    bitmap_ready_version = _ready_bitmap_version(
                        ready.get("bitmap_version", -1), bitmap_version
                    )
                    worker_bitmap_versions[wr] = bitmap_ready_version
                    profile_version = ready.get("empirical_profile_version", "")
                    worker_profile_versions[wr] = (
                        profile_version if isinstance(profile_version, str) else ""
                    )
                    if ready_status == "idle":
                        idle_ranks.add(wr)
                    else:
                        idle_ranks.discard(wr)
                    any_progress = True

                # 仅向"活跃"（rank <= max_active_workers）的空闲 worker 派发；
                # 超额 rank 保持在 idle_ranks 中停泊（不占 CPU）。
                if work_idx < len(work_queue) and idle_ranks:
                    _t0 = time.monotonic()
                    for wr in sorted(idle_ranks):
                        if work_idx >= len(work_queue):
                            break
                        if wr > max_active_workers:
                            continue  # 停泊
                        if adaptive_policy is not None:
                            selected_index = adaptive_policy.work_index(
                                wr,
                                work_queue,
                                work_idx,
                                active_worker_count=max_active_workers,
                            )
                            if selected_index is None:
                                continue
                            if selected_index != work_idx:
                                work_queue[work_idx], work_queue[selected_index] = (
                                    work_queue[selected_index],
                                    work_queue[work_idx],
                                )
                        if state_coordinator is not None:
                            selected_index = state_coordinator.work_index(
                                wr,
                                work_queue,
                                work_idx,
                                active_worker_count=max_active_workers,
                            )
                            if (
                                selected_index is not None
                                and selected_index != work_idx
                            ):
                                work_queue[work_idx], work_queue[selected_index] = (
                                    work_queue[selected_index],
                                    work_queue[work_idx],
                                )
                        try:
                            dispatched = _dispatch_to(wr, work_queue[work_idx])
                        except Exception:
                            # A policy or storage failure can occur before the
                            # send-specific handler is installed. Retire the
                            # reservation immediately instead of waiting for
                            # master shutdown to unwind it.
                            rollback = _rollback_dispatch_registries(
                                wr,
                                preparing_dispatches,
                                active_dispatches,
                                committing_dispatches,
                            )
                            if rollback["failed"]:
                                print(
                                    "[Master] dispatch exception rollback "
                                    f"failures: worker={wr} {rollback}",
                                    flush=True,
                                )
                            raise
                        work_idx += 1
                        if dispatched:
                            idle_ranks.discard(wr)
                        any_progress = True
                    if _prof:
                        _t_dispatch += time.monotonic() - _t0
                        _n_dispatch += 1

                # 收集已完成 workers 的结果（非阻塞）
                admission_capacity = result_admission.available_capacity
                incoming_result = bool(
                    admission_capacity > 0
                    and comm.iprobe(source=MPI.ANY_SOURCE, tag=TAG_RESULT)
                )
                if incoming_result or result_admission.has_ready():
                    any_progress = True
                    # 批量收集所有可用结果
                    batch_results: list[tuple] = []
                    batch_lease_ids: list[tuple[str, int]] = []
                    batch_lease_fences: list[tuple[str, str]] = []
                    batch_target_leases: list[tuple[tuple[int, ...], str]] = []
                    batch_target_assignments: list[
                        tuple[int, tuple[tuple[int, str], ...]]
                    ] = []
                    batch_state_completions: list[
                        tuple[str, int, float, int, float, bool]
                    ] = []
                    batch_dispatch_workers: list[int] = []
                    profile_telemetry_batch: list[dict] = []
                    batch_agentic_outcomes: dict[
                        int, tuple[str, dict, dict | None, dict]
                    ] = {}
                    batch_agentic_followups: dict[
                        int, tuple[dict, dict[str, typing.Any]]
                    ] = {}
                    _t0 = time.monotonic()
                    if incoming_result:
                        raw_results, quarantined_results, _received_results = (
                            _receive_hybrid_result_batch(
                                comm,
                                dispatch_generation_gate,
                                active_dispatches,
                                active_workers,
                                admission_capacity,
                            )
                        )
                        result_admission.submit_many(raw_results)
                    else:
                        quarantined_results = []
                    for wr, result_status in quarantined_results:
                        stats.quarantine_result(result_status)
                        print(
                            "[Master] quarantined non-current result: "
                            f"worker={wr} status={result_status}",
                            flush=True,
                        )
                    for wr, dispatch_tx, admitted, error in (
                        result_admission.collect_ready()
                    ):
                        if error is not None:
                            dispatch_generation_gate.observe_result(
                                wr,
                                dispatch_tx.dispatch_token,
                                {},
                            )
                            stats.quarantine_result("payload")
                            print(
                                "[Master] quarantined invalid current result: "
                                f"worker={wr} error={error}",
                                flush=True,
                            )
                            continue
                        assert admitted is not None
                        result = admitted
                        current_ready = dispatch_generation_gate.has_ready(
                            wr, dispatch_tx.dispatch_token
                        )
                        dispatch_generation_gate.retire(wr)
                        completed_item = active_work_items.pop(wr, None)
                        completed_has_continuation = (
                            completed_item is not None
                            and _work_item_parts(completed_item)[5] is not None
                        )
                        if completed_item is not None:
                            completed_payload = _work_item_recovery_payload(
                                completed_item
                            )
                            dispatch_protocol_attempts.pop(
                                WorkLeaseJournal.work_id(completed_payload),
                                None,
                            )
                        ip = active_workers.pop(wr, "unknown")
                        input_sha = active_hashes.pop(wr, "")
                        completed_topseed_run = active_topseed_runs.pop(wr, "")
                        if completed_topseed_run:
                            completed_topseed_runs[wr] = completed_topseed_run
                        if object_transport_enabled and not completed_has_continuation:
                            reported_object_id = result.get("input_object_id")
                            object_acknowledged = _acknowledge_worker_input_object(
                                worker_objects,
                                wr,
                                input_sha,
                                reported_object_id,
                            )
                            if reported_object_id and not object_acknowledged:
                                print(
                                    "[Master] ignored mismatched worker input "
                                    f"object acknowledgement: rank={wr}",
                                    flush=True,
                                )
                        active_lease = active_leases.pop(wr, "")
                        active_lease_fence = active_lease_fences.pop(wr, "")
                        active_target_lease = active_target_leases.pop(wr, None)
                        if current_ready:
                            idle_ranks.add(wr)
                        active_state_task = active_state_tasks.pop(wr, "")
                        active_schedule_prefix = active_schedule_prefixes.pop(wr, ())
                        dispatched_strategy = active_strategies.pop(wr, 0)
                        dispatched_agentic_task = active_agentic_tasks.pop(
                            wr,
                            {
                                "schema": 1,
                                "input_path": ip,
                                "sha256": input_sha,
                                "strategy": dispatched_strategy,
                            },
                        )
                        completed_component_choice = active_component_choices.pop(
                            wr, {}
                        )
                        completed_parallel_cohort = active_parallel_cohorts.pop(
                            wr, -1
                        )
                        dispatched_target = _normalize_branch_id(
                            dispatched_agentic_task.get("target_branch", 0)
                        )
                        dispatched_actions = _normalize_s2f_actions(
                            dispatched_agentic_task.get("s2f_actions", ())
                        )
                        if shared_work_leases is not None:
                            if (
                                not active_lease
                                or not active_lease_fence
                                or not shared_work_leases.begin_commit(
                                    active_lease,
                                    active_lease_fence,
                                )
                            ):
                                stale_topseed = completed_topseed_runs.pop(wr, "")
                                if stale_topseed and topseed_selector is not None:
                                    topseed_selector.observe(
                                        stale_topseed, (), failed=True
                                    )
                                    _save_topseed()
                                print(
                                    "[Master] dropped stale fenced result "
                                    f"from worker {wr} for {active_lease[:12]}",
                                    flush=True,
                                )
                                if dispatch_tx is not None:
                                    active_dispatches.pop(wr, None)
                                    rollback = dispatch_tx.rollback()
                                    if rollback["failed"]:
                                        print(
                                            "[Master] stale-result rollback "
                                            f"failures: worker={wr} {rollback}",
                                            flush=True,
                                        )
                                else:
                                    _release_shared_target(active_target_lease)
                                    if adaptive_policy is not None:
                                        adaptive_policy.release_target_assignment(
                                            dispatched_target,
                                            dispatched_actions,
                                        )
                                continue
                        if dispatch_tx is not None:
                            active_dispatches.pop(wr, None)
                            committing_dispatches[wr] = dispatch_tx
                            batch_dispatch_workers.append(wr)
                        if active_target_lease is not None:
                            batch_target_leases.append(active_target_lease)
                        if dispatched_target or dispatched_actions:
                            batch_target_assignments.append(
                                (dispatched_target, dispatched_actions)
                            )
                        completed_component_choices[wr] = completed_component_choice
                        if completed_parallel_cohort >= 0:
                            completed_parallel_cohorts[wr] = (
                                completed_parallel_cohort
                            )
                        new_tcs = result.get("new_tests", [])
                        total_gen = result.get("total_generated", len(new_tcs))
                        result_budget_error = result.get("result_budget_error")
                        if isinstance(result_budget_error, dict):
                            print(
                                "[Master] worker result rejected by budget: "
                                f"rank={wr} {result_budget_error}",
                                flush=True,
                            )
                        completed_generated[wr] = max(0, int(total_gen))
                        continuation_frontier = result.get("continuation_frontier", ())
                        if live_state_store is not None and isinstance(
                            continuation_frontier, (list, tuple)
                        ):
                            for checkpoint in continuation_frontier:
                                try:
                                    descriptor = live_state_store.restore_continuation(
                                        str(checkpoint)
                                    ).descriptor.to_mapping()
                                except (OSError, TypeError, ValueError):
                                    continue
                                work_queue.append(
                                    (
                                        ip,
                                        dispatched_agentic_task.get("focus_bytes", "")
                                        or None,
                                        int(
                                            dispatched_agentic_task.get(
                                                "target_branch", 0
                                            )
                                            or 0
                                        ),
                                        _normalize_s2f_actions(
                                            dispatched_agentic_task.get(
                                                "s2f_actions", ()
                                            )
                                        ),
                                        active_schedule_prefix,
                                        descriptor,
                                    )
                                )
                        if semantic_fallback is not None:
                            completed_hashes[wr] = input_sha
                        stats.generated_count += total_gen
                        # BSFuzz：聚合超时分支 site_id，增长时写出共享跳过集
                        if branch_share_master:
                            ts = result.get("timeout_sites")
                            if ts:
                                before_n = len(global_timeout_sites)
                                global_timeout_sites.update(ts)
                                if len(global_timeout_sites) > before_n:
                                    try:
                                        _tmp = skip_sites_master_path + ".tmp"
                                        with open(_tmp, "w") as _sf:
                                            _sf.write(
                                                "\n".join(
                                                    str(s) for s in global_timeout_sites
                                                )
                                            )
                                        os.replace(_tmp, skip_sites_master_path)
                                    except OSError:
                                        pass
                        if _prof:
                            _n_recv_bytes += sum(
                                (
                                    len(tc.get("content", b""))
                                    if isinstance(tc.get("content"), bytes)
                                    else int(tc.get("object_size", 0) or 0)
                                )
                                for tc in new_tcs
                            )
                        telemetry_raw = result.get("telemetry")
                        if isinstance(telemetry_raw, dict):
                            profile_telemetry_batch.append(telemetry_raw)
                        telemetry = (
                            SolverTelemetry.from_mapping(telemetry_raw)
                            if isinstance(telemetry_raw, dict)
                            else None
                        )
                        proposal_id = str(result.get("proposal_id", "") or "")
                        if verified_proposals is not None and proposal_id:
                            proposal_verified = verified_proposals.validate(
                                proposal_id,
                                telemetry,
                                retcode=int(result.get("retcode", -1)),
                                killed=bool(result.get("killed", False)),
                            )
                            proposal_record = verified_proposals.records.get(
                                proposal_id
                            )
                            if (
                                semantic_proposals is not None
                                and proposal_record is not None
                                and proposal_record.grammar_rule_id
                            ):
                                semantic_proposals.observe_grammar_validation(
                                    proposal_record.grammar_rule_id,
                                    valid=proposal_verified,
                                    parser_valid=(
                                        True
                                        if proposal_record.last_reason
                                        == "parser-accepted"
                                        else False
                                        if proposal_record.last_reason.startswith(
                                            "parser-"
                                        )
                                        else None
                                    ),
                                    context_id=(
                                        proposal_record.parser_context_id
                                        or proposal_record.grammar_context_id
                                    ),
                                    source_context_id=(
                                        proposal_record.grammar_source_context_id
                                        or proposal_record.grammar_context_id
                                    ),
                                    candidate_context_id=(
                                        proposal_record.grammar_candidate_context_id
                                    ),
                                    production_id=(
                                        proposal_record.parser_production_id
                                    ),
                                    recursion_prefix_hex=(
                                        proposal_record.parser_recursion_prefix_hex
                                    ),
                                    recursion_suffix_hex=(
                                        proposal_record.parser_recursion_suffix_hex
                                    ),
                                    cfg_fragment_json=(
                                        proposal_record.parser_cfg_fragment_json
                                    ),
                                    cfg_fragment_sha256=(
                                        proposal_record.parser_cfg_fragment_sha256
                                    ),
                                    telemetry=telemetry,
                                )
                            if (
                                semantic_proposals is not None
                                and proposal_record is not None
                                and proposal_record.history_seed_id
                            ):
                                semantic_proposals.observe_history_validation(
                                    proposal_record.history_seed_id,
                                    valid=proposal_verified,
                                )
                            proposal_content = result.get("proposal_content")
                            proposal_object_id = result.get(
                                "proposal_object_id"
                            )
                            if proposal_verified and (
                                isinstance(proposal_content, bytes)
                                or isinstance(proposal_object_id, str)
                            ):
                                proposal_candidate = {
                                    "bitmap": result.get("proposal_bitmap"),
                                    "proposal_id": proposal_id,
                                }
                                if isinstance(proposal_content, bytes):
                                    proposal_candidate["content"] = proposal_content
                                else:
                                    proposal_candidate["object_id"] = (
                                        proposal_object_id
                                    )
                                    proposal_candidate["object_size"] = result.get(
                                        "proposal_object_size"
                                    )
                                new_tcs.append(proposal_candidate)
                        dispatched_strategy = int(
                            result.get("strategy", dispatched_strategy)
                        )
                        result_lease = str(active_lease or result.get("lease_id", ""))
                        result_lease_fence = str(
                            active_lease_fence or result.get("lease_fence", "")
                        )
                        result_state_task, state_task_mismatch = (
                            _authoritative_state_task(
                                active_state_task,
                                result.get("state_task_id", ""),
                            )
                        )
                        if state_task_mismatch:
                            print(
                                "[Master] ignored mismatched worker state task "
                                f"from rank {wr}",
                                flush=True,
                            )
                        result_schedule_prefix = normalize_schedule_prefix(
                            result.get("schedule_prefix", active_schedule_prefix)
                        )
                        schedule_trace = result.get("schedule_trace", "")
                        if (
                            dpor_explorer is not None
                            and isinstance(schedule_trace, str)
                            and schedule_trace
                        ):
                            schedule_target_branch = _normalize_branch_id(
                                dispatched_agentic_task.get("target_branch", 0)
                            )
                            added_prefixes = dpor_explorer.observe(
                                ip,
                                schedule_trace,
                                sha256=input_sha,
                                current_prefix=result_schedule_prefix,
                                target_branch=schedule_target_branch,
                            )
                            if added_prefixes:
                                print(
                                    f"[Master] DPOR queued {added_prefixes} "
                                    f"schedule prefixes "
                                    f"(pending {dpor_explorer.pending_count()})",
                                    flush=True,
                                )
                        if state_coordinator is not None and result_state_task:
                            batch_state_completions.append(
                                (
                                    result_state_task,
                                    wr,
                                    min(1.0, len(new_tcs) / max(1, total_gen)),
                                    total_gen,
                                    float(result.get("elapsed", 0) or 0),
                                    bool(result.get("killed", False)),
                                )
                            )
                        executor = str(
                            result.get(
                                "executor", _strategy_executor(dispatched_strategy)
                            )
                        )
                        executor_pulls[executor] = executor_pulls.get(executor, 0) + 1
                        result_s2f_actions = _normalize_s2f_actions(
                            result.get("s2f_actions", ())
                        )
                        result_parameter_token = str(
                            result.get("parameter_token", "") or ""
                        )
                        result_parameter_overrides = (
                            {
                                str(name): str(value)
                                for name, value in result.get(
                                    "parameter_overrides", {}
                                ).items()
                                if isinstance(name, str)
                            }
                            if isinstance(result.get("parameter_overrides"), dict)
                            else {}
                        )
                        if builtin_agentic is not None:
                            dispatched_agentic_task["strategy"] = dispatched_strategy
                            dispatched_agentic_task["target_branch"] = (
                                telemetry.target_branch
                                if telemetry is not None
                                else dispatched_agentic_task.get("target_branch", 0)
                            )
                            dispatched_agentic_task["s2f_actions"] = [
                                [branch, action]
                                for branch, action in result_s2f_actions
                            ]
                            builtin_agentic.observe(
                                dispatched_agentic_task,
                                telemetry_raw
                                if isinstance(telemetry_raw, dict)
                                else None,
                                result,
                            )
                        if (
                            agentic_tasks_out or agentic_backends is not None
                            or structured_agentic is not None
                        ) and telemetry is not None:
                            agentic_task = {
                                "schema": 1,
                                "input_path": ip,
                                "sha256": input_sha,
                                "strategy": dispatched_strategy,
                                "executor": executor,
                                "target_branch": telemetry.target_branch,
                                "target_reached": telemetry.target_reached,
                                "s2f_actions": [
                                    [branch, action]
                                    for branch, action in result_s2f_actions
                                ],
                                "open_branches": list(telemetry.open_branches),
                                "symbolic_branches": telemetry.symbolic_branches,
                                "generated": int(result.get(
                                    "total_generated", len(new_tcs)
                                )),
                                "coverage_delta": 0,
                                "interesting_cases": 0,
                                "solver_queries": telemetry.solver_queries,
                                "solver_unknown": telemetry.solver_unknown,
                                "z3_timeouts": telemetry.z3_timeouts,
                                "solver_time_us": telemetry.solver_time_us,
                                "backsolver_targets": telemetry.backsolver_targets,
                                "backsolver_attempts": telemetry.backsolver_attempts,
                                "backsolver_sat": telemetry.backsolver_sat,
                                "backsolver_constraints_kept": telemetry.backsolver_constraints_kept,
                                "backsolver_constraints_dropped": telemetry.backsolver_constraints_dropped,
                                "backsolver_direct_attempts": telemetry.backsolver_direct_attempts,
                                "backsolver_direct_sat": telemetry.backsolver_direct_sat,
                                "backsolver_validations": telemetry.backsolver_validations,
                                "backsolver_validation_failures": telemetry.backsolver_validation_failures,
                                "backsolver_z3_fallbacks": telemetry.backsolver_z3_fallbacks,
                                "difficulty": telemetry.difficulty,
                                "data_features": [
                                    list(feature)
                                    for feature in telemetry.data_features[:32]
                                ],
                                "static_data_features": [
                                    list(feature)
                                    for feature in telemetry.static_data_features[:64]
                                ],
                                "comparison_taints": [
                                    list(feature)
                                    for feature in telemetry.comparison_taints[:32]
                                ],
                            }
                            if agentic_tasks_out:
                                append_task(agentic_tasks_out, agentic_task)
                            if agentic_backends is not None:
                                agentic_backends.submit(agentic_task)
                            if structured_agentic is not None:
                                fallback_hint: dict[str, typing.Any] = {}
                                if builtin_agentic is not None:
                                    fallback_hint.update(
                                        builtin_agentic.suggest(agentic_task)
                                    )
                                if semantic_fallback is not None:
                                    semantic_agentic_hint = semantic_fallback.suggest(
                                        ip,
                                        sha256=input_sha,
                                        target_branch=telemetry.target_branch,
                                    )
                                    if semantic_agentic_hint:
                                        fallback_hint.update(semantic_agentic_hint)
                                batch_agentic_followups[wr] = (
                                    agentic_task, fallback_hint
                                )
                        structured_outcome_id = str(
                            dispatched_agentic_task.get(
                                "structured_decision_id", ""
                            ) or ""
                        )
                        if structured_agentic is not None and structured_outcome_id:
                            batch_agentic_outcomes[wr] = (
                                structured_outcome_id,
                                dict(dispatched_agentic_task),
                                dict(telemetry_raw)
                                if isinstance(telemetry_raw, dict)
                                else None,
                                {
                                    "elapsed": result.get("elapsed", 0),
                                    "retcode": result.get("retcode", 0),
                                    "total_generated": result.get(
                                        "total_generated", len(new_tcs)
                                    ),
                                    "killed": bool(result.get("killed", False)),
                                },
                            )
                        batch_results.append(
                            (
                                wr,
                                ip,
                                new_tcs,
                                result.get("retcode", 0),
                                result.get("elapsed", 0),
                                result.get("killed", False),
                                dispatched_strategy,
                                telemetry,
                                result_s2f_actions,
                                result_parameter_token,
                                result_parameter_overrides,
                            )
                        )
                        batch_lease_ids.append((result_lease, wr))
                        if result_lease and result_lease_fence:
                            batch_lease_fences.append(
                                (result_lease, result_lease_fence)
                            )
                    if online_value_profiles is not None and profile_telemetry_batch:
                        online_value_profiles.observe_many(profile_telemetry_batch)
                        published_profile = online_value_profiles.publish()
                        if published_profile is not None:
                            print(
                                "[Master] Published empirical-domain "
                                f"generation {published_profile.version[:12]} "
                                f"({published_profile.profile_count} profiles)",
                                flush=True,
                            )
                    if _prof:
                        _t_recv += time.monotonic() - _t0
                        _n_recv += len(batch_results)

                    # 批量 triage
                    _t0 = time.monotonic()
                    if batch_results:
                        def _observe_structured_agentic_triage(
                            worker_rank: int,
                            coverage_delta: int,
                            interesting_cases: int,
                        ) -> None:
                            if structured_agentic is None:
                                return
                            followup = batch_agentic_followups.get(worker_rank)
                            if followup is not None:
                                followup[0]["coverage_delta"] = coverage_delta
                                followup[0]["interesting_cases"] = (
                                    interesting_cases
                                )
                            context = batch_agentic_outcomes.pop(
                                worker_rank, None
                            )
                            if context is None:
                                return
                            (
                                decision_id,
                                dispatched_task,
                                raw_telemetry,
                                worker_result,
                            ) = context
                            worker_result["coverage_delta"] = coverage_delta
                            worker_result["interesting_cases"] = interesting_cases
                            structured_agentic.observe(
                                decision_id,
                                dispatched_task,
                                raw_telemetry,
                                worker_result,
                            )

                        bitmap_changed = _batch_triage(
                            batch_results,
                            stats,
                            coverage,
                            afl_config,
                            queue_dir,
                            crashes_dir,
                            hangs_dir,
                            afl_sync_queue,
                            save_all_dir,
                            symcc_dir,
                            bitmap_path_triage,
                            symcc_feedback_queue,
                            queue_id_ref,
                            crash_id_ref=crash_id_ref,
                            hang_id_ref=hang_id_ref,
                            file_generation=file_generation,
                            afl_extras_dir=afl_extras_dir,
                            hint_id_ref=hint_id_ref,
                            recent_byte_offsets=recent_byte_offsets,
                            focus_bytes_window=FOCUS_BYTES_WINDOW,
                            yield_callback=_update_edge_yield,
                            observation_callback=(
                                _observe_adaptive if adaptive_policy else None
                            ),
                            analyzed_hashes_ref=processed_content_hashes,
                            analyzed_hash_callback=(
                                hash_ledger.add if hash_ledger is not None else None
                            ),
                            coverage_claim_callback=(
                                _claim_global_coverage
                                if coverage_gossip is not None
                                else None
                            ),
                            coverage_claim_many_callback=(
                                _claim_global_coverage_many
                                if coverage_gossip is not None
                                else None
                            ),
                            proposal_retention_callback=(
                                _record_proposal_retention
                                if verified_proposals is not None
                                else None
                            ),
                            topseed_observation_callback=(
                                _observe_topseed
                                if topseed_selector is not None
                                else None
                            ),
                            topseed_feature_limit=(
                                topseed_selector.max_features
                                if topseed_selector is not None
                                else 65_536
                            ),
                            agentic_observation_callback=(
                                _observe_structured_agentic_triage
                                if structured_agentic is not None
                                else None
                            ),
                            triage_profile=(
                                _triage_detail if _prof else None
                            ),
                            crash_digests_ref=known_crash_digests,
                            hang_digests_ref=known_hang_digests,
                            result_object_store=result_object_store,
                            result_object_max_bytes=(
                                master_result_max_object_bytes
                            ),
                        )
                        if structured_agentic is not None:
                            for worker_rank in sorted(batch_agentic_followups):
                                followup_task, fallback_hint = (
                                    batch_agentic_followups[worker_rank]
                                )
                                structured_agentic.submit(
                                    followup_task,
                                    fallback_hint=fallback_hint,
                                )
                        if bitmap_changed:
                            bitmap_version += 1
                            bitmap_journal.record(
                                bitmap_version, coverage.consume_delta()
                            )
                            _publish_current_coverage(changed=True)
                        if work_leases is not None:
                            for lease_id, lease_worker in batch_lease_ids:
                                if lease_id:
                                    work_leases.complete(lease_id, worker=lease_worker)
                        if shared_work_leases is not None:
                            for lease_id, fence in batch_lease_fences:
                                shared_work_leases.complete(lease_id, fence)
                        for target_lease in batch_target_leases:
                            _release_shared_target(target_lease)
                        if adaptive_policy is not None:
                            for target, actions in batch_target_assignments:
                                adaptive_policy.release_target_assignment(
                                    target, actions
                                )
                        if state_coordinator is not None:
                            for (
                                task_id,
                                state_worker,
                                reward,
                                generated,
                                elapsed,
                                killed,
                            ) in batch_state_completions:
                                state_coordinator.complete(
                                    task_id,
                                    reward=reward,
                                    generated=generated,
                                    elapsed=elapsed,
                                    killed=killed,
                                    worker=state_worker,
                                )
                            _save_state_tasks()
                        for completed_worker in batch_dispatch_workers:
                            transaction = committing_dispatches.pop(
                                completed_worker, None
                            )
                            if transaction is not None:
                                transaction.commit()
                        # 更新 focus_bytes：仅从 interesting TCs 收集偏移
                        if len(recent_byte_offsets) >= 5:
                            min_off = max(0, min(recent_byte_offsets) - 32)
                            max_off = max(recent_byte_offsets) + 32
                            focus_bytes_str = f"{min_off}-{max_off}"
                            if compact_focus_enabled and _write_compact_focus_set(
                                focus_set_path, recent_byte_offsets
                            ):
                                focus_set_str = focus_set_path

                    if _prof:
                        _t_triage += time.monotonic() - _t0
                        _n_triage += 1

                # RESULT and READY use different MPI tags and may be observed in
                # either order. Recover only after both messages identify the
                # same current generation and the result itself was malformed.
                for wr, dispatch_tx in list(active_dispatches.items()):
                    dispatch_token = dispatch_tx.dispatch_token
                    if not dispatch_generation_gate.recoverable(wr, dispatch_token):
                        continue
                    if _recover_active_dispatch(
                        wr, dispatch_token, source="invalid-result", park_worker=False
                    ):
                        any_progress = True

                # A silent worker cannot produce the READY/RESULT join used
                # above. Retire only exact generations with no READY evidence;
                # the rank stays parked until an exact-token READY proves its
                # old execution has ended.
                for wr, dispatch_token in _expired_dispatches(
                    active_dispatches,
                    now=time.monotonic(),
                    timeout=dispatch_watchdog_sec,
                ):
                    if dispatch_generation_gate.has_ready(wr, dispatch_token):
                        continue
                    if _recover_active_dispatch(
                        wr, dispatch_token, source="watchdog-timeout", park_worker=True
                    ):
                        any_progress = True

            # 未派发完的工作项（含细分字节区间）按项结转到下一轮，避免按路径去重把同
            # 种子的其余区间或 target branch 丢弃。代数信息保留在 file_generation 中。
            carried_items = work_queue[work_idx:]
            # 安全阀：持续高产饱和时积压可能增长，超上限则截断以限制内存。被截断的项不会
            # 永久丢失覆盖率——其内容也已写入 AFL 队列/同步副本，会被后续扫描重新纳入（仅
            # 丢失代数深度、多做少量重扫）。carried_items 仅存小元组，故上限
            # 设得较宽，正常运行几乎不触发。
            _carry_cap = max(4096, max_active_workers * (_focus_parts + 1) * 4)
            if len(carried_items) > _carry_cap:
                print(
                    f"[Master] carried_items 积压 {len(carried_items)} 超 "
                    f"{_carry_cap}，截断以限制内存",
                    flush=True,
                )
                for discarded_item in carried_items[_carry_cap:]:
                    discarded_topseed = topseed_item_proposals.pop(
                        id(discarded_item), ""
                    )
                    if discarded_topseed and topseed_selector is not None:
                        topseed_selector.discard(discarded_topseed)
                carried_items = carried_items[:_carry_cap]
            if pending_target_leases:
                retained_target_groups: set[tuple[int, ...]] = set()
                for carried_item in carried_items:
                    (
                        _path,
                        _focus,
                        carried_target,
                        carried_actions,
                        _schedule,
                        _continuation,
                    ) = _work_item_parts(carried_item)
                    group = _target_group(carried_target, carried_actions)
                    if group:
                        retained_target_groups.add(group)
                for group, token in list(pending_target_leases.items()):
                    if group in retained_target_groups:
                        continue
                    pending_target_leases.pop(group, None)
                    _release_shared_target((group, token))
                    if adaptive_policy is not None:
                        adaptive_policy.release_target_assignment(
                            group[0],
                            tuple((target, "solve") for target in group[1:]),
                        )
            carried_paths = {item[0] for item in carried_items}

            # 旧的 collect/triage 代码已移到 while 循环内的交替处理中

            # 轻量进度汇总（每 2s，替代每批 triage print）：热路径外、走全局计数
            _pnow = time.monotonic()
            if _pnow - last_progress_time >= PROGRESS_INTERVAL:
                _dt = _pnow - last_progress_time
                _rate = (
                    (stats.generated_count - prog_prev_generated) / _dt
                    if _dt > 0
                    else 0
                )
                print(
                    f"[Master] {stats.interesting_count} interesting / "
                    f"{stats.generated_count} generated, "
                    f"{len(active_workers)} busy ({_rate:.0f} tc/s)",
                    flush=True,
                )
                last_progress_time = _pnow
                prog_prev_generated = stats.generated_count

            # Periodic stats output
            stats_interval = 15 if _prof else STATS_INTERVAL_SEC
            if time.monotonic() - last_stats_time > stats_interval:
                afl_coverage_bridge.schedule_fair()
                afl_retry = afl_coverage_bridge.poll()
                if afl_retry.local_bitmap_changed:
                    bitmap_version += 1
                    bitmap_journal.record(
                        bitmap_version, coverage.consume_delta()
                    )
                    _publish_current_coverage(changed=True)
                if coverage_gossip is not None:
                    coverage_gossip.heartbeat()
                    remote_coverage = coverage_gossip.pull()
                    if remote_coverage and coverage.merge_delta(remote_coverage):
                        bitmap_version += 1
                        bitmap_journal.record(bitmap_version, coverage.consume_delta())
                        _publish_current_coverage(changed=True)
                stats.log(stats_file)
                last_stats_time = time.monotonic()
                yields = _get_edge_yield()
                yield_str = " ".join(
                    f"{k}={v:.2f}(a={edge_yield_counts[k][0]},b={edge_yield_counts[k][1]})"
                    for k, v in yields.items()
                )
                _active_now = min(max_active_workers, num_workers)
                print(
                    f"[Master] Stats: {stats.total_count} ok, "
                    f"{stats.failed_count} failed, "
                    f"{stats.interesting_count} interesting / "
                    f"{stats.generated_count} total, "
                    f"max_depth={max_generation_reached}, "
                    f"active_workers={_active_now}/{num_workers}, "
                    f"yield=[{yield_str}]"
                )
                if adaptive_policy is not None:
                    adaptive_policy.save()
                    _as = adaptive_policy.snapshot()
                    print(
                        f"[Master] Adaptive: features="
                        f"{_as['coverage_features']} unique_paths="
                        f"{_as['unique_paths']} data_bits="
                        f"{_as['data_feature_bits']} prefix="
                        f"{_as['prefix_nodes']}/{_as['open_prefixes']} mdp="
                        f"{_as.get('selective_mdp_refreshes', 0)}/"
                        f"{_as.get('selective_mdp_skipped_refreshes', 0)}"
                        f"@{_as.get('selective_mdp_refresh_interval', 1)} "
                        f"strategy_pulls="
                        f"{_as['strategies']['pulls']} edge_dep="
                        f"{_as.get('edge_dependence_branches', 0)}/"
                        f"{_as.get('edge_dependence_cells', 0)} structural="
                        f"{_as.get('structural_task_regions', 0)}/"
                        f"{_as.get('structural_task_rebalances', 0)} mpc="
                        f"{_as.get('path_cover_count', 0)}/"
                        f"{_as.get('path_cover_infeasible', 0)} executors="
                        f"{executor_pulls} simifuzz="
                        f"{_as.get('simifuzz_workers', 0)}/"
                        f"{_as.get('simifuzz_assignments', 0)}/"
                        f"{_as.get('simifuzz_cross_learning', 0)}"
                    )
                if semantic_fallback is not None:
                    semantic_fallback.save()
                    print(f"[Master] SemanticFallback: {semantic_fallback.snapshot()}")
                if coverage_gossip is not None:
                    print(f"[Master] CoverageGossip: {coverage_gossip.snapshot()}")
                print(
                    f"[Master] AFLCoverageBridge: "
                    f"{afl_coverage_bridge.snapshot()}"
                )
                if parallel_controller is not None:
                    print(
                        f"[Master] ParallelController: "
                        f"{parallel_controller.snapshot()}"
                    )
                if shared_target_leases is not None:
                    print(
                        "[Master] TargetLeases: "
                        f"{shared_target_leases.snapshot_counts()} "
                        f"pending={len(pending_target_leases)} "
                        f"active={len(active_target_leases)}"
                    )
                if verified_proposals is not None:
                    verified_proposals.save()
                    print(
                        f"[Master] VerifiedProposals: {verified_proposals.snapshot()}"
                    )
                if semantic_proposals is not None:
                    semantic_proposals.save()
                    _write_pcfg_research_artifact()
                _write_parser_research_artifact()
                if parameter_policy is not None:
                    parameter_policy.save()
                    print(f"[Master] SelfConfig: {parameter_policy.snapshot()}")
                if component_policy is not None:
                    component_policy.save()
                    print(
                        f"[Master] Components: "
                        f"{component_policy.snapshot()} parallel="
                        f"{parallel_controller.snapshot() if parallel_controller else {}}"
                    )
                if agentic_backends is not None:
                    print(f"[Master] AgentBackends: {agentic_backends.snapshot()}")
                if structured_agentic is not None:
                    print(
                        f"[Master] StructuredAgentic: "
                        f"{structured_agentic.snapshot()}"
                    )
                # 写出产出统计供 run_hybrid 自适应控制器读取
                _write_symcc_stats()
                if _prof and _n_scan > 0:
                    print(
                        f"[PROF] scan={_t_scan:.2f}s/{_n_scan}x "
                        f"dispatch={_t_dispatch:.2f}s/{_n_dispatch}x "
                        f"recv={_t_recv:.2f}s/{_n_recv}x({_n_recv_bytes // 1024}KB) "
                        f"triage={_t_triage:.2f}s/{_n_triage}x "
                        f"idle={_t_idle:.2f}s"
                    )
                    if _triage_detail:
                        detail = " ".join(
                            f"{name}={elapsed:.2f}s"
                            for name, elapsed in sorted(_triage_detail.items())
                        )
                        print(f"[PROF-TRIAGE] {detail}")
                    sys.stdout.flush()

            # 无输入且无活跃 worker 时等待 AFL 产生新用例
            _t0 = time.monotonic()
            # 是否存在可立即派发的空闲"活跃"worker（rank<=上限；停泊 rank 不算）。
            # 上面的派发循环已把所有可派发的工作排空，故若反馈仍被放回队列，
            # 通常意味着无空闲活跃 worker——此时必须 sleep，否则 100% 忙等空转
            # 直到某 worker 返回（最长 TIMEOUT_SEC）。
            _dispatchable_idle = any(r <= max_active_workers for r in idle_ranks)
            if not work_queue and not active_workers and not symcc_feedback_queue:
                time.sleep(2)
            elif symcc_feedback_queue and _dispatchable_idle:
                pass  # 有反馈且有空闲活跃 worker → 立即分发，不睡
            else:
                # 有活跃 worker 在跑 → RESULT 很快到达：用更短轮询间隔把"结果到达→再派发"
                # 的服务延迟从 50ms 降到 5ms（master 负载 <10%、idle 57%，多轮询开销可忽略；
                # fastSolve 让许多求解变快后，50ms 占单个工作项比例更大）。否则用较长间隔省 CPU。
                time.sleep(0.005 if active_workers else 0.05)
            if _prof:
                _t_idle += time.monotonic() - _t0
    finally:
        # --- 优雅关闭 ---
        for transactions in (
            preparing_dispatches,
            active_dispatches,
            committing_dispatches,
        ):
            for worker, transaction in list(transactions.items()):
                rollback = transaction.rollback()
                if rollback["failed"]:
                    print(
                        "[Master] shutdown dispatch rollback failures: "
                        f"worker={worker} {rollback}",
                        flush=True,
                    )
            transactions.clear()
        for target_lease in list(active_target_leases.values()):
            _release_shared_target(target_lease)
        for targets, token in list(pending_target_leases.items()):
            _release_shared_target((targets, token))
            if adaptive_policy is not None:
                adaptive_policy.release_target_assignment(
                    targets[0],
                    tuple((target, "solve") for target in targets[1:]),
                )
        active_target_leases.clear()
        pending_target_leases.clear()
        if query_service_process is not None:
            _terminate_query_service(query_service_process, timeout=5.0)
        if query_service_log is not None:
            try:
                query_service_log.close()
            except OSError:
                pass
        if online_value_profiles is not None:
            final_profile = online_value_profiles.publish(force=True)
            if final_profile is not None:
                print(
                    "[Master] Final empirical-domain generation "
                    f"{final_profile.version[:12]} "
                    f"({final_profile.profile_count} profiles)",
                    flush=True,
                )
            print(
                f"[Master] OnlineEVP: {online_value_profiles.snapshot()}",
                flush=True,
            )
        if _density_executor is not None:
            _density_executor.shutdown(wait=True, cancel_futures=True)
        _aux_showmap_executor.shutdown(wait=True, cancel_futures=True)
        result_admission.close()
        # 清理密度剖析临时目录
        if _prof_dir:
            shutil.rmtree(_prof_dir, ignore_errors=True)
        if adaptive_policy is not None:
            adaptive_policy.save()
        if semantic_fallback is not None:
            semantic_fallback.save()
        if verified_proposals is not None:
            verified_proposals.save()
        if semantic_proposals is not None:
            semantic_proposals.save()
            _write_pcfg_research_artifact()
        _write_parser_research_artifact()
        if parameter_policy is not None:
            parameter_policy.save()
        if component_policy is not None:
            component_policy.save()
        if builtin_agentic is not None:
            builtin_agentic.save()
        if agentic_backends is not None:
            for aliases, online_hint in agentic_backends.drain():
                for alias in aliases:
                    agentic_hints[alias] = online_hint
            agentic_backends.close()
        if structured_agentic is not None:
            _consume_structured_decisions(
                structured_agentic.finish_requests()
            )
            structured_agentic.close()
        if topseed_selector is not None:
            for topseed_run in list(active_topseed_runs.values()):
                topseed_selector.observe(topseed_run, (), failed=True)
            active_topseed_runs.clear()
            for topseed_run in list(completed_topseed_runs.values()):
                topseed_selector.observe(topseed_run, (), failed=True)
            completed_topseed_runs.clear()
            for proposal_token in list(topseed_item_proposals.values()):
                topseed_selector.discard(proposal_token)
            topseed_item_proposals.clear()
            _save_topseed()
            print(
                f"[Master] TopSeed: {topseed_selector.telemetry()}",
                flush=True,
            )
        _save_state_tasks()
        # 输出 profiling 数据
        if _prof:
            wall = time.monotonic() - last_stats_time + STATS_INTERVAL_SEC
            print(
                f"[PROF] scan:     {_t_scan:>7.2f}s ({_n_scan} calls, "
                f"avg {_t_scan / _n_scan * 1000:.1f}ms)"
                if _n_scan
                else ""
            )
            print(
                f"[PROF] dispatch: {_t_dispatch:>7.2f}s ({_n_dispatch} sends, "
                f"avg {_t_dispatch / _n_dispatch * 1000:.2f}ms)"
                if _n_dispatch
                else ""
            )
            print(
                f"[PROF] recv:     {_t_recv:>7.2f}s ({_n_recv} results, "
                f"avg {_t_recv / max(_n_recv, 1) * 1000:.1f}ms, "
                f"~{_n_recv_bytes / 1024 / 1024:.1f}MB total)"
            )
            print(f"[PROF] triage:   {_t_triage:>7.2f}s ({_n_triage} batches)")
            if _triage_detail:
                # ``batch_core`` contains its child fields; callback and its
                # named scheduler sub-phases also overlap.  Keep the hierarchy
                # explicit so it is not misread as a wall-time decomposition.
                detail = " ".join(
                    f"{name}={elapsed:.2f}s"
                    for name, elapsed in sorted(_triage_detail.items())
                )
                print(f"[PROF] triage-detail(overlap): {detail}")
            print(f"[PROF] idle:     {_t_idle:>7.2f}s")
            print(f"[PROF] wall:     {wall:>7.2f}s (总墙钟，含各阶段与 idle)")
            sys.stdout.flush()

        final_afl_update = afl_coverage_bridge.close()
        if final_afl_update.local_bitmap_changed:
            bitmap_version += 1
            bitmap_journal.record(bitmap_version, coverage.consume_delta())
        # 先输出最终统计（在尝试与 worker 通信之前，因为 worker 可能已被 SIGTERM 杀死）
        _publish_current_coverage(force=True)
        stats.log(stats_file)
        print(
            f"[Master] Final stats: {stats.total_count} ok, "
            f"{stats.failed_count} failed, "
            f"{stats.interesting_count} interesting / "
            f"{stats.generated_count} total, "
            f"{stats.quarantined_results} results quarantined, "
            f"{stats.quarantined_ready_messages} READY quarantined, "
            f"{stats.requeued_dispatches} protocol requeued, "
            f"{stats.deferred_dispatches} protocol deferred, "
            f"{stats.watchdog_timeouts} watchdog timeouts, "
            f"{stats.watchdog_worker_recoveries} workers recovered"
        )
        print(
            f"[Master] Final AFLCoverageBridge: "
            f"{afl_coverage_bridge.snapshot()}"
        )
        print(
            f"[Master] Final ResultAdmission: {result_admission.snapshot()}"
        )
        if coverage_gossip is not None:
            print(
                f"[Master] Final CoverageGossip: {coverage_gossip.snapshot()}"
            )
        if parallel_controller is not None:
            print(
                f"[Master] Final ParallelController: "
                f"{parallel_controller.snapshot()}"
            )
        sys.stdout.flush()
        try:
            stats_file.close()
        except OSError:
            pass

        print("[Master] Shutting down workers with READY/STOP/ACK fencing...")
        shutdown = _cooperative_shutdown_workers(
            comm,
            range(1, size),
            initial_ready=idle_ranks,
            grace=shutdown_grace_sec,
        )
        shutdown_clean = bool(shutdown["clean"])
        print(
            "[Master] Shutdown: "
            f"acked={len(shutdown['acknowledged'])}/{num_workers} "
            f"pending={list(shutdown['pending'])} "
            f"quarantined={shutdown['quarantined_acks']} "
            f"communication_errors={list(shutdown['communication_errors'])} "
            f"elapsed={shutdown['elapsed']:.3f}s",
            flush=True,
        )
        if shutdown_clean:
            # Result objects are transport-only.  Once every worker has ACKed,
            # no future descriptor can reference this campaign-local store.
            shutil.rmtree(
                os.path.join(symcc_dir, ".result_objects"),
                ignore_errors=True,
            )
        master_queue_lock.close()

    return shutdown_clean


def worker(comm: "MPI.Intracomm", args: argparse.Namespace) -> None:
    """Worker process: receives inputs, runs SymCC, sends back results."""
    rank = comm.Get_rank()
    debug_ready = os.environ.get("SYMCC_DEBUG_READY") == "1"
    if debug_ready:
        print(f"[Worker {rank}] entered worker loop", flush=True)
    target_cmd = args.target
    executor_portfolio = ExecutorPortfolio.from_environment(target_cmd)

    worker_dir = tempfile.mkdtemp(prefix=f"symcc_mpi_w{rank}_")

    # QSYM 内部剪枝图和 AFL showmap 图属于不同哈希空间，必须分离。前者仅由
    # 本 worker 的 QSYM runtime 持久更新；后者通过 MPI delta 播种 worker 去重。
    solver_bitmap_file = os.path.join(worker_dir, "qsym_bitmap")
    worker_env = os.environ.copy()
    worker_env["SYMCC_AFL_COVERAGE_MAP"] = solver_bitmap_file
    adaptive_scheduler = os.environ.get("SYMCC_ADAPTIVE_SCHEDULER", "1") != "0"
    telemetry_file = os.path.join(worker_dir, "solver_telemetry.json")
    automatic_value_profile_context = False
    if adaptive_scheduler:
        worker_env["SYMCC_TELEMETRY_OUT"] = telemetry_file
        worker_env["SYMSAN_TELEMETRY_OUT"] = telemetry_file
        worker_env.setdefault("SYMCC_DATA_COVERAGE", "1")
        worker_env.setdefault("SYMCC_VALUE_PROFILE", "1")
        context = worker_env.get("SYMCC_VALUE_PROFILE_CONTEXT", "")
        if len(context) != 64 or any(
            byte not in "0123456789abcdef" for byte in context
        ):
            automatic_value_profile_context = True
            context = _command_executable_sha256(target_cmd)
        if context:
            worker_env["SYMCC_VALUE_PROFILE_CONTEXT"] = context
        else:
            worker_env.pop("SYMCC_VALUE_PROFILE_CONTEXT", None)
    max_object_bytes = _bounded_env_int(
        os.environ,
        "SYMCC_MAX_TRANSPORT_INPUT",
        16 * 1024 * 1024,
        1,
        _MAX_HYBRID_RESULT_BYTES,
    )
    worker_result_max_objects = _bounded_env_int(
        os.environ,
        "SYMCC_HYBRID_RESULT_MAX_OBJECTS",
        _DEFAULT_HYBRID_RESULT_MAX_OBJECTS,
        1,
        _MAX_HYBRID_RESULT_OBJECTS,
    )
    timeout_sites_max = _bounded_env_int(
        os.environ,
        "SYMCC_TIMEOUT_SITES_MAX",
        _DEFAULT_TIMEOUT_SITES_MAX,
        1,
        _MAX_TIMEOUT_SITES,
    )
    timeout_sites_max_bytes = _bounded_env_int(
        os.environ,
        "SYMCC_TIMEOUT_SITES_MAX_BYTES",
        _DEFAULT_TIMEOUT_SITES_MAX_BYTES,
        1,
        _MAX_TIMEOUT_SITES_MAX_BYTES,
    )
    schedule_trace_max_bytes = _bounded_env_int(
        os.environ,
        "SYMCC_SCHEDULE_TRACE_MAX",
        _DEFAULT_SCHEDULE_TRACE_MAX_BYTES,
        1,
        _MAX_SCHEDULE_TRACE_MAX_BYTES,
    )
    object_store = ContentAddressedInputStore(
        os.path.join(worker_dir, "objects"), max_object_bytes
    )

    # BSFuzz 跨-worker 超时分支共享（opt-in，SYMCC_BRANCH_SHARE=1）：
    #  - SYMCC_SKIP_SITES：master 聚合的全局超时 site_id 文件（每次 SymCC 进程新起，
    #    自动重读最新版，无需版本广播）；
    #  - SYMCC_TIMEOUT_OUT：本 worker 本次运行导出的超时 site_id（随后回传 master 聚合）。
    branch_share = os.environ.get("SYMCC_BRANCH_SHARE") == "1"
    symcc_dir_w = os.path.join(args.output_dir, args.name)
    result_object_store = None
    result_object_transport_enabled = os.environ.get(
        "SYMCC_RESULT_OBJECT_TRANSPORT", "1"
    ).lower() not in {
        "0",
        "false",
        "off",
        "no",
    }
    result_object_store_error_logged = False
    worker_live_store = None
    worker_live_executor = None
    if (
        os.environ.get("SYMCC_LIVE_PROGRAM")
        or os.environ.get("SYMCC_LIVE_LLVM")
        or os.environ.get("SYMCC_LIVE_STATE_STORE")
    ):
        try:
            worker_live_store = LiveStateStore(
                os.environ.get(
                    "SYMCC_LIVE_STATE_STORE", os.path.join(symcc_dir_w, ".live_states")
                ),
                page_size=max(64, int(os.environ.get("SYMCC_LIVE_PAGE_SIZE", "4096"))),
                **_live_state_graph_limits(os.environ),
            )
            worker_live_executor = LiveContinuationExecutor(worker_live_store)
        except (OSError, TypeError, ValueError):
            worker_live_store = None
            worker_live_executor = None
    try:
        async_query_workers = max(
            0, int(os.environ.get("SYMCC_ASYNC_QUERY_WORKERS", "0"))
        )
    except ValueError:
        async_query_workers = 0
    if async_query_workers:
        worker_env.setdefault(
            "SYMCC_QUERY_SPOOL", os.path.join(symcc_dir_w, ".query_spool")
        )
        worker_env.setdefault(
            "SYMCC_QUERY_OUTPUT_DIR", os.path.join(symcc_dir_w, ".query_candidates")
        )
    if adaptive_scheduler and os.environ.get("SYMCC_POLY_CACHE", "1") != "0":
        poly_cache_path = os.path.join(symcc_dir_w, ".poly_cache")
        worker_env.setdefault("SYMCC_POLY_CACHE", poly_cache_path)
        worker_env.setdefault("SYMCC_POLY_CROSS_PREFIX", "1")
        worker_env.setdefault("SYMCC_POLY_PROJECTED_REUSE", "1")
        worker_env.setdefault("SYMCC_POLY_EXACT_PROJECTION", "1")
        worker_env.setdefault("SYMCC_POLY_FIELD_RENAMING", "1")
    else:
        poly_cache_path = os.path.join(symcc_dir_w, ".poly_cache")
    skip_sites_path = os.path.join(symcc_dir_w, ".skip_sites")
    timeout_out_file = os.path.join(worker_dir, "timeout_sites")
    s2f_actions_file = os.path.join(worker_dir, "s2f_actions")
    if branch_share:
        worker_env["SYMCC_SKIP_SITES"] = skip_sites_path

    # 细粒度并行分解（opt-in，SYMCC_WORKER_DIVERSITY=1）：
    #  - 策略轴（本处，per-rank）：每个 worker 一个不同的求解策略画像 —— 负载均衡（各做
    #    完整分析），使相似种子在不同 worker 上产出发散输入；
    #  - 空间轴（focus 字节区间）：由 MASTER 按工作项动态分配（见 _build_work_items 的
    #    动态工作窃取），本 worker 直接采用 WORK 消息里的 focus_bytes。
    diversity = os.environ.get("SYMCC_WORKER_DIVERSITY") == "1"
    div_slot = rank - 1  # rank 0 为 master，worker 从 1 起
    if diversity and not adaptive_scheduler:
        _prof = SYMCC_STRATEGY_PROFILES[div_slot % len(SYMCC_STRATEGY_PROFILES)]
        for _k, _v in _prof.items():
            worker_env[_k] = _v

    # 初始化 streaming showmap（持久 fork server，~0.6ms/call）
    afl_showmap_path = shutil.which("afl-showmap")
    streaming_sm: StreamingShowmap | None = None
    _sm_init_tries = 0
    _sm_disabled_logged = False
    _SM_MAX_INIT_TRIES = 5  # AFL 首次尚未就绪时给几次重试，之后放弃（避免每轮重建）
    if afl_showmap_path is None:
        # 无 afl-showmap → 无法本地 dedup，每个 TC 全量回传 master 且走全量 showmap
        # triage（MPI/CPU 开销显著上升）。显式告警，避免静默降级不可见。
        print(
            f"[Worker {rank}] WARNING: afl-showmap 不在 PATH，"
            f"流式 dedup 关闭（每个 TC 全量回传 master，开销上升）",
            flush=True,
        )
    # Worker 端 coverage bitmap 副本 — 用于本地 dedup
    worker_cov = CoverageBitmap()
    worker_seen: set[bytes] = (
        set()
    )  # 跨 item 内容去重(#1);worker 生命周期,有界见 _WORKER_SEEN_CAP
    worker_parameter_keys = set(_STRATEGY_KEYS)
    current_bitmap_version = -1
    coverage_snapshot_version = [-1]
    current_profile_version = ""
    local_value_profile = os.path.join(worker_dir, "empirical_value_profile.runtime")
    executable_context_cache: dict[str, tuple[tuple[int, int, int, int], str]] = {}
    tace_enabled = os.environ.get("SYMCC_TACE", "1").lower() not in {
        "0",
        "false",
        "off",
        "no",
    }
    try:
        tace_min_bytes = max(1, int(os.environ.get("SYMCC_TACE_MIN_BYTES", "4096")))
    except ValueError:
        tace_min_bytes = 4096
    try:
        tace_timeout = max(1, int(os.environ.get("SYMCC_TACE_PROFILE_TIMEOUT", "5")))
    except ValueError:
        tace_timeout = 5
    try:
        tace_cache_limit = max(16, int(os.environ.get("SYMCC_TACE_CACHE", "2048")))
    except ValueError:
        tace_cache_limit = 2048
    tace_cache: dict[str, str | None] = {}
    tace_profile_dir = os.path.join(worker_dir, "tace-profile")

    # 每 worker 分相位计时（opt-in SYMCC_WORKER_PROFILE=1，热路径几乎零开销：
    # 每工作项约 6 次 time.monotonic()）。相位：wait=等待/取件, bmsync=位图同步,
    # import=输入拷入, exec=concolic 执行, showmap_dedup=showmap 取边+本地去重,
    # send=回传 master。用于瓶颈分析（如 ~12 worker 饱和点到底卡在执行/后处理/等待）。
    _wprof = os.environ.get("SYMCC_WORKER_PROFILE") == "1"
    _pt = {
        "wait": 0.0,
        "bmsync": 0.0,
        "import": 0.0,
        "exec": 0.0,
        "showmap_dedup": 0.0,
        "send": 0.0,
        "items": 0,
    }
    # #10 重复求解拆分计数：gen=生成总数, reported=worker 判新上报数,
    # infeasible=没打到新边(乐观求解不可行/冗余), worker_fresh=打到新边但本 item 内已被自己覆盖,
    # showmap_none=showmap 无输出。worker-内部冗余=gen-reported;worker-间冗余=reported-accepted(master)。
    _redun = {
        "gen": 0,
        "reported": 0,
        "infeasible": 0,
        "worker_fresh": 0,
        "showmap_none": 0,
        "items": 0,
        "snap_none": 0,
        "byte_dup": 0,
    }
    # 各 worker 各自把分相位计时落盘：编排层用 SIGTERM 杀 mpirun，进程内 MPI gather 来不及，
    # 故落 per-rank 文件，跑完由 aggregate_phase_timing() 事后合并。目录优先 SYMCC_WPROF_DIR。
    _wprof_dir = os.environ.get("SYMCC_WPROF_DIR") or symcc_dir_w
    _phs = ["wait", "bmsync", "import", "exec", "showmap_dedup", "send"]
    # 在途相位标记 {"phase": 名称|None, "start": 时刻}。被 SIGTERM 打断时把这段"在途"时长
    # 计入【正确的】相位——否则慢目标上单个工作项可能横跨整个窗口、到被杀时相位时长仍为 0
    # （只在完成后累加），导致严重低估。run_symcc_worker 会在 exec→showmap_dedup 转换处更新它。
    _inflight = {"phase": None, "start": 0.0}

    def _flush_prof() -> None:
        if _inflight["phase"] is not None:
            _pt[_inflight["phase"]] += time.monotonic() - _inflight["start"]
            # exec 已完成但（因 item 未跑完）尚未提交的部分，补计到 exec
            _pt["exec"] += _inflight.get("exec_done", 0.0)
            _inflight["phase"] = None
        try:
            os.makedirs(_wprof_dir, exist_ok=True)
            with open(
                os.path.join(_wprof_dir, f"phase_timing_rank{rank}.csv"), "w"
            ) as _f:
                _f.write(
                    f"{rank},{_pt['items']},"
                    + ",".join(f"{_pt[_p]:.4f}" for _p in _phs)
                    + "\n"
                )
            with open(os.path.join(_wprof_dir, f"redun_rank{rank}.csv"), "w") as _f:
                _f.write(
                    f"{rank},"
                    + ",".join(
                        str(_redun.get(_k, 0))
                        for _k in [
                            "gen",
                            "reported",
                            "infeasible",
                            "worker_fresh",
                            "showmap_none",
                            "items",
                            "snap_none",
                            "byte_dup",
                        ]
                    )
                    + "\n"
                )
        except (IOError, OSError):
            pass

    if _wprof:
        # SIGTERM 可捕获，落盘后退出（SIGKILL 不可捕获，但编排层通常先发 SIGTERM 再宽限）
        def _on_term(_signum: "int", _frame: "object") -> None:
            _flush_prof()
            if worker_live_executor is not None:
                worker_live_executor.close()
            os._exit(0)

        try:
            signal.signal(signal.SIGTERM, _on_term)
        except (ValueError, OSError):
            pass

    completed_dispatch_token = ""
    shutdown_token = ""
    while True:
        # Signal ready
        if debug_ready:
            print(
                f"[Worker {rank}] sending READY "
                f"bitmap={current_bitmap_version} "
                f"completed={bool(completed_dispatch_token)}",
                flush=True,
            )
        comm.send(
            {
                "rank": rank,
                "bitmap_version": current_bitmap_version,
                "empirical_profile_version": current_profile_version,
                "completed_dispatch_token": completed_dispatch_token,
            },
            dest=0,
            tag=TAG_READY,
        )
        completed_dispatch_token = ""

        # Wait for work or stop
        status = MPI.Status()
        _t = time.monotonic() if _wprof else 0.0
        msg = comm.recv(source=0, tag=MPI.ANY_TAG, status=status)
        if debug_ready:
            print(
                f"[Worker {rank}] received tag={status.Get_tag()}",
                flush=True,
            )
        if _wprof:
            _pt["wait"] += time.monotonic() - _t

        if status.Get_tag() == TAG_STOP:
            shutdown_token = _shutdown_stop_token(msg, rank)
            if not shutdown_token:
                print(
                    f"[Worker {rank}] Ignoring malformed shutdown message",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            break

        if status.Get_tag() != TAG_WORK:
            continue

        if "empirical_profile_version" in msg:
            current_profile_version, profile_installed = install_value_profile_update(
                msg, local_value_profile, current_profile_version
            )
            if profile_installed:
                worker_env["SYMCC_VALUE_PROFILE_IN"] = local_value_profile
            else:
                # Never continue with a stale generation when the master named
                # a different version but its payload failed verification.
                worker_env.pop("SYMCC_VALUE_PROFILE_IN", None)

        input_path = msg["path"]
        lease_id = str(msg.get("lease_id", "") or "")
        lease_fence = str(msg.get("lease_fence", "") or "")
        state_task_id = str(msg.get("state_task_id", "") or "")
        dispatch_token = _normalize_dispatch_token(msg.get("dispatch_token"))
        if not dispatch_token:
            raise RuntimeError("master sent work without a valid dispatch token")
        parameter_token = str(msg.get("parameter_token", "") or "")
        proposal_id = str(msg.get("proposal_id", "") or "")
        parameter_overrides = sanitize_parameter_overrides(
            msg.get("parameter_overrides")
        )
        bm_version = msg.get("bitmap_version", 0)
        if _wprof:
            _pt["items"] += 1
            _t = time.monotonic()

        # AFL coverage 只通过 MPI 的完整快照/稀疏 delta 更新 worker 端去重状态。
        # 它不会写入 solver_bitmap_file，避免污染 QSYM 自己的分支哈希空间。
        if bm_version > current_bitmap_version:
            if os.environ.get("SYMCC_NO_WORKER_SEED") != "1":
                worker_cov.data = BitmapDeltaJournal.apply(worker_cov.data, msg)
            current_bitmap_version = bm_version
        if _wprof:
            _pt["bmsync"] += time.monotonic() - _t

        # Continuation checkpoints are self-contained CAS work.  Resume them
        # before importing the original seed: AFL may legitimately have moved
        # or deleted that path since the checkpoint was forked.
        continuation_id = str(msg.get("continuation_id", "") or "")
        if continuation_id:
            started = time.monotonic()
            try:
                if worker_live_executor is None:
                    raise ValueError("live-state store is unavailable")
                live_result = worker_live_executor.resume(
                    continuation_id,
                    max_steps=_bounded_env_int(
                        os.environ,
                        "SYMCC_LIVE_STEPS_PER_LEASE",
                        1000,
                        1,
                        _MAX_LIVE_GRAPH_OBJECTS,
                    ),
                    max_states=min(
                        worker_result_max_objects,
                        _bounded_env_int(
                            os.environ,
                            "SYMCC_LIVE_STATES_PER_LEASE",
                            1,
                            1,
                            _MAX_LIVE_GRAPH_OBJECTS,
                        ),
                    ),
                )
                result = {
                    "new_tests": [],
                    # Scheduler reward counts resumable work, not transient
                    # checkpoints whose states halted in this lease.
                    "total_generated": len(live_result["frontier"]),
                    "continuation_generated": len(live_result["generated_checkpoints"]),
                    "retcode": 0,
                    "elapsed": time.monotonic() - started,
                    "killed": False,
                    "lease_id": lease_id,
                    "lease_fence": lease_fence,
                    "state_task_id": state_task_id,
                    "proposal_id": proposal_id,
                    "parameter_token": parameter_token,
                    "parameter_overrides": parameter_overrides,
                    "continuation_frontier": live_result["frontier"],
                    "continuation_id": continuation_id,
                }
            except (OSError, TypeError, ValueError, RuntimeError) as exc:
                result = {
                    "new_tests": [],
                    "total_generated": 0,
                    "retcode": -1,
                    "elapsed": time.monotonic() - started,
                    "killed": False,
                    "error": str(exc),
                    "lease_id": lease_id,
                    "lease_fence": lease_fence,
                    "state_task_id": state_task_id,
                    "proposal_id": proposal_id,
                    "parameter_token": parameter_token,
                    "parameter_overrides": parameter_overrides,
                    "continuation_id": continuation_id,
                }
            completed_dispatch_token = _send_dispatch_result(
                comm, result, dispatch_token
            )
            continue

        # 延迟初始化 streaming showmap（首次需要 AFL 已写出 fuzzer_stats/命令行）。
        # 限制重试次数：AFL 就绪前给几次机会，之后放弃并告警，避免每个工作项都
        # 重新读盘构造 AflConfig（永久失败时会变成 worker 热路径上的反复 I/O）。
        if (
            streaming_sm is None
            and afl_showmap_path
            and _sm_init_tries < _SM_MAX_INIT_TRIES
        ):
            _sm_init_tries += 1
            try:
                afl_cfg = AflConfig(os.path.join(args.output_dir, args.fuzzer_name))
                streaming_sm = StreamingShowmap(
                    afl_showmap_path, afl_cfg.target_command
                )
            except (
                OSError,
                RuntimeError,
                ValueError,
                IndexError,
                subprocess.SubprocessError,
            ) as e:
                if _sm_init_tries >= _SM_MAX_INIT_TRIES and not _sm_disabled_logged:
                    _sm_disabled_logged = True
                    print(
                        f"[Worker {rank}] WARNING: 流式 showmap 初始化连续 "
                        f"{_SM_MAX_INIT_TRIES} 次失败（{e}），退化为全量 showmap "
                        f"triage（MPI/CPU 开销上升）",
                        flush=True,
                    )

        # 优先使用按 SHA-256 标识的不可变对象。关闭对象传输时，路径兼容模式也必须
        # 稳定导入本地 CAS 并匹配 master 摘要，不能直接 copy2 一个可变名称。
        _t = time.monotonic() if _wprof else 0.0
        materialized_object_id = ""
        try:
            local_input, materialized_object_id = _materialize_hybrid_worker_input(
                object_store,
                msg,
                input_path,
            )
        except (IOError, OSError, ValueError):
            # 文件可能被替换、删除或超限；不运行与调度身份不一致的字节。
            result = {
                "new_tests": [],
                "retcode": -1,
                "elapsed": 0,
                "killed": False,
                "total_generated": 0,
                "lease_id": lease_id,
                "lease_fence": lease_fence,
                "state_task_id": state_task_id,
                "proposal_id": proposal_id,
                "parameter_token": parameter_token,
                "parameter_overrides": parameter_overrides,
            }
            completed_dispatch_token = _send_dispatch_result(
                comm, result, dispatch_token
            )
            continue
        if _wprof:
            _pt["import"] += time.monotonic() - _t

        # 选择性符号化 focus_bytes：直接采用 WORK 消息里的区间。多样性模式下这是 master
        # 动态工作窃取分配的不相交字节区间（见 _build_work_items）；否则是 master 的全局
        # focus（若有）。空则整段符号化。
        focus = msg.get("focus_bytes", "")
        if focus:
            worker_env["SYMCC_FOCUS_BYTES"] = focus
        elif "SYMCC_FOCUS_BYTES" in worker_env:
            del worker_env["SYMCC_FOCUS_BYTES"]
        focus_set = msg.get("focus_set", "")
        if focus_set:
            worker_env["SYMCC_FOCUS_SET"] = str(focus_set)
            worker_env.setdefault("SYMCC_FOCUS_MARGIN", "2")
        else:
            worker_env.pop("SYMCC_FOCUS_SET", None)

        # 在线策略组合由 master 逐工作项选择。先清除受控键，避免同一 worker 的上一策略
        # 泄漏到下一项；关闭自适应调度时保持原有 per-rank 固定画像。
        strategy = int(msg.get("strategy", 0))
        s2f_actions: tuple[tuple[int, str], ...] = ()
        if adaptive_scheduler:
            for key in worker_parameter_keys | set(parameter_overrides):
                worker_env.pop(key, None)
            worker_parameter_keys.update(parameter_overrides)
            if 0 <= strategy < len(SYMCC_STRATEGY_PROFILES):
                worker_env.update(SYMCC_STRATEGY_PROFILES[strategy])
            for key, value in parameter_overrides.items():
                if key == "SYMCC_POLY_CACHE":
                    if value.lower() not in {"0", "false", "off", "no"}:
                        worker_env[key] = poly_cache_path
                    else:
                        worker_env.pop(key, None)
                else:
                    worker_env[key] = value
            target_branch = _normalize_branch_id(msg.get("target_branch", 0))
            if target_branch > 0:
                worker_env["SYMCC_TARGET_BRANCH"] = str(target_branch)
            else:
                worker_env.pop("SYMCC_TARGET_BRANCH", None)
            s2f_actions = _normalize_s2f_actions(msg.get("s2f_actions", ()))
            if _write_s2f_action_file(s2f_actions_file, s2f_actions):
                worker_env["SYMCC_S2F_ACTIONS"] = s2f_actions_file
            else:
                worker_env.pop("SYMCC_S2F_ACTIONS", None)

        executor_class = worker_env.get(
            "SYMCC_EXECUTOR_CLASS", _strategy_executor(strategy)
        )
        try:
            algorithm_budget = max(
                1,
                min(
                    TIMEOUT_SEC,
                    int(
                        round(
                            float(
                                parameter_overrides.get(
                                    "SYMCC_ALGORITHM_BUDGET_SEC", TIMEOUT_SEC
                                )
                            )
                        )
                    ),
                ),
            )
        except (TypeError, ValueError, OverflowError):
            algorithm_budget = TIMEOUT_SEC
        execution_route = executor_portfolio.resolve(executor_class, algorithm_budget)
        tace_profiled = False
        # TACE two-stage path: for large ordinary inputs, run one bounded
        # no-solve dependency pass and cache the resulting sparse focus set by
        # immutable input digest. Explicit replay/focus work keeps its exact
        # symbolic domain to preserve target reachability.
        target_branch = _normalize_branch_id(msg.get("target_branch", 0))
        input_digest = str(
            msg.get("sha256", "") or msg.get("object_id", "") or input_path
        )
        try:
            input_size = os.path.getsize(local_input)
        except OSError:
            input_size = 0
        if (
            tace_enabled
            and execution_route.engine == "symcc"
            and input_size >= tace_min_bytes
            and not focus
            and not focus_set
            and target_branch == 0
        ):
            if input_digest not in tace_cache:
                if len(tace_cache) >= tace_cache_limit:
                    tace_cache.pop(next(iter(tace_cache)))
                profile_environment = dict(worker_env)
                profile_environment.update(execution_route.environment)
                offsets = _profile_tace_dependencies(
                    list(execution_route.command),
                    local_input,
                    tace_profile_dir,
                    min(tace_timeout, TIMEOUT_SEC),
                    execution_route.use_stdin,
                    profile_environment,
                )
                focus_path: str | None = None
                if offsets:
                    focus_path = os.path.join(
                        worker_dir, f"tace-{input_digest[:24] or time.monotonic_ns()}"
                    )
                    if not _write_compact_focus_set(
                        focus_path, offsets, max_entries=65536
                    ):
                        focus_path = None
                tace_cache[input_digest] = focus_path
                tace_profiled = True
            cached_focus = tace_cache.get(input_digest)
            if cached_focus:
                worker_env["SYMCC_FOCUS_SET"] = cached_focus
                worker_env.setdefault("SYMCC_FOCUS_MARGIN", "1")
        execution_env = dict(worker_env)
        execution_env.update(execution_route.environment)
        if automatic_value_profile_context:
            executable = str(execution_route.command[0])
            resolved_executable = (
                executable
                if os.path.sep in executable
                else shutil.which(executable) or ""
            )
            try:
                executable_stat = os.stat(resolved_executable)
                executable_signature = (
                    executable_stat.st_dev,
                    executable_stat.st_ino,
                    executable_stat.st_size,
                    executable_stat.st_mtime_ns,
                )
            except OSError:
                executable_signature = (0, 0, 0, 0)
            cached_context = executable_context_cache.get(resolved_executable)
            if cached_context is None or cached_context[0] != executable_signature:
                route_context = _command_executable_sha256(
                    list(execution_route.command)
                )
                executable_context_cache[resolved_executable] = (
                    executable_signature,
                    route_context,
                )
            else:
                route_context = cached_context[1]
            if route_context:
                execution_env["SYMCC_VALUE_PROFILE_CONTEXT"] = route_context
            else:
                execution_env.pop("SYMCC_VALUE_PROFILE_CONTEXT", None)

        # Run SymCC
        run_output = os.path.join(worker_dir, f"output_{time.monotonic_ns()}")

        if adaptive_scheduler:
            try:
                os.unlink(telemetry_file)
            except OSError:
                pass

        if branch_share:
            try:
                os.unlink(timeout_out_file)  # 清除上次残留
            except OSError:
                pass
            worker_env["SYMCC_TIMEOUT_OUT"] = timeout_out_file
            execution_env["SYMCC_TIMEOUT_OUT"] = timeout_out_file

        schedule_trace_path = ""
        schedule_prefix_file = ""
        schedule_prefix = normalize_schedule_prefix(msg.get("schedule_prefix", ()))
        if msg.get("schedule_enabled"):
            schedule_preload = str(msg.get("schedule_preload", "") or "")
            if schedule_preload and os.path.isfile(schedule_preload):
                stamp = time.monotonic_ns()
                schedule_trace_path = os.path.join(
                    worker_dir, f"schedule_{stamp}.trace"
                )
                schedule_prefix_file = os.path.join(
                    worker_dir, f"schedule_{stamp}.prefix"
                )
                execution_env["SYMCC_SCHEDULE_TRACE"] = schedule_trace_path
                execution_env["SYMCC_DPOR"] = "1"
                if schedule_prefix and write_schedule_prefix(
                    schedule_prefix_file, schedule_prefix
                ):
                    execution_env["SYMCC_SCHEDULE_PREFIX"] = schedule_prefix_file
                else:
                    execution_env.pop("SYMCC_SCHEDULE_PREFIX", None)
                execution_env.setdefault(
                    "SYMCC_SCHEDULE_WAIT_MS",
                    os.environ.get("SYMCC_SCHEDULE_WAIT_MS", "100"),
                )
                execution_env.setdefault(
                    "SYMCC_SCHEDULE_ENABLED",
                    os.environ.get("SYMCC_SCHEDULE_ENABLED", "1"),
                )
                execution_env.setdefault(
                    "SYMCC_SCHEDULE_ENABLED_SETTLE_US",
                    os.environ.get("SYMCC_SCHEDULE_ENABLED_SETTLE_US", "1000"),
                )
                execution_env.setdefault(
                    "SYMCC_SCHEDULE_ENABLED_MAX_THREADS",
                    os.environ.get("SYMCC_SCHEDULE_ENABLED_MAX_THREADS", "32"),
                )
                execution_env["LD_PRELOAD"] = prepend_ld_preload(
                    schedule_preload, execution_env.get("LD_PRELOAD")
                )

        try:
            if _wprof:
                _inflight["phase"] = (
                    "exec"  # 进入执行；run_symcc_worker 会在转入后处理时改标记
                )
                _inflight["start"] = time.monotonic()
                _inflight["exec_done"] = (
                    0.0  # 本 item 已完成的 exec 时长（转入后处理时填）
                )
            result_budget_error = None
            try:
                new_tests, total_gen, retcode, elapsed, killed, post_elapsed = (
                    run_symcc_worker(
                        list(execution_route.command),
                        local_input,
                        run_output,
                        execution_route.timeout_sec,
                        execution_route.use_stdin,
                        engine_name=execution_route.engine,
                        base_env=execution_env,
                        streaming_showmap=streaming_sm,
                        worker_coverage=worker_cov,
                        inflight=_inflight if _wprof else None,
                        redun=_redun if _wprof else None,
                        worker_seen=worker_seen,
                        raw_save_all_dir=args.save_all,
                        coverage_snapshot_path=str(
                            msg.get("coverage_snapshot_path", "") or ""
                        ),
                        coverage_snapshot_version_ref=coverage_snapshot_version,
                    )
                )
            except _WorkerResultBudgetExceeded as error:
                new_tests = []
                total_gen = error.objects
                retcode = error.retcode
                elapsed = error.elapsed
                killed = error.killed
                post_elapsed = error.post_elapsed
                result_budget_error = error.payload()
                print(
                    f"[Worker {rank}] {error}",
                    file=sys.stderr,
                    flush=True,
                )
            if _wprof:
                _inflight["phase"] = None  # 正常完成：用真实分段值,不用在途估计
                _pt["exec"] += elapsed  # 相位 E：concolic 执行
                _pt["showmap_dedup"] += post_elapsed  # 相位 F：showmap 取边 + 本地去重

            # 读取本次超时分支 site_id，回传 master 聚合
            timeout_sites = []
            if branch_share:
                try:
                    timeout_sites = _load_timeout_sites(
                        timeout_out_file,
                        max_sites=timeout_sites_max,
                        max_bytes=timeout_sites_max_bytes,
                    )
                except (IOError, OSError, UnicodeError, ValueError):
                    pass

            telemetry = None
            if adaptive_scheduler:
                raw_telemetry = None
                try:
                    with open(telemetry_file, encoding="utf-8") as stream:
                        candidate = json.load(stream)
                    if isinstance(candidate, dict):
                        raw_telemetry = candidate
                except (OSError, ValueError, TypeError):
                    pass
                try:
                    with open(
                        os.path.join(run_output, ".string_solver_metrics.json"),
                        encoding="ascii",
                    ) as stream:
                        string_metrics = json.load(stream)
                    if isinstance(string_metrics, dict):
                        if raw_telemetry is None:
                            raw_telemetry = {}
                        raw_telemetry["string_records_loaded"] = int(
                            string_metrics.get("records_loaded", 0)
                        )
                        raw_telemetry["string_solver_queries"] = int(
                            string_metrics.get("solver_queries", 0)
                        )
                        raw_telemetry["string_solver_verified"] = int(
                            string_metrics.get("solver_verified", 0)
                        )
                        raw_telemetry["string_dual_view_verified"] = int(
                            string_metrics.get("dual_view_verified", 0)
                        )
                except (OSError, ValueError, TypeError):
                    pass
                try:
                    input_bytes = os.path.getsize(local_input)
                except OSError:
                    input_bytes = 0
                if execution_route.engine == "symsan":
                    solver_algorithm = (
                        "rgd" if execution_env.get("SYMSAN_SOLVER") == "rgd" else "z3"
                    )
                else:
                    solver_algorithm = execution_env.get("SYMCC_SOLVER_ALGORITHM", "z3")
                telemetry = SolverTelemetry.from_observation(
                    raw_telemetry,
                    engine=execution_route.engine,
                    input_bytes=input_bytes,
                    generated=total_gen,
                    elapsed=elapsed,
                    return_code=retcode,
                    killed=killed,
                    solver_algorithm=solver_algorithm,
                )
            proposal_content = None
            proposal_bitmap = None
            if proposal_id:
                try:
                    with open(local_input, "rb") as stream:
                        proposal_content = stream.read(max_object_bytes + 1)
                    if len(proposal_content) > max_object_bytes:
                        proposal_content = None
                    elif (
                        streaming_sm is not None
                        and execution_env.get(
                            "SYMCC_VERIFY_PROPOSAL_BITMAP", "0"
                        ).lower()
                        not in {"0", "false", "off", "no"}
                    ):
                        proposal_bitmap = streaming_sm.get_edges(proposal_content)
                except OSError:
                    proposal_content = None
            schedule_trace = ""
            if schedule_trace_path:
                try:
                    with open(
                        schedule_trace_path, encoding="ascii", errors="replace"
                    ) as stream:
                        schedule_trace = stream.read(schedule_trace_max_bytes + 1)
                    if len(schedule_trace) > schedule_trace_max_bytes:
                        schedule_trace = ""
                except OSError:
                    schedule_trace = ""

            if result_object_transport_enabled and result_object_store is None:
                try:
                    # Delay shared-store creation until WORK arrives.  Starting
                    # it at worker boot can create the master's output path and
                    # make a fresh campaign look like an accidental resume.
                    result_object_store = ContentAddressedInputStore(
                        os.path.join(symcc_dir_w, ".result_objects"),
                        max_object_bytes,
                    )
                except (OSError, ValueError) as error:
                    if not result_object_store_error_logged:
                        print(
                            f"[Worker {rank}] result-object transport "
                            f"unavailable: {error}",
                            file=sys.stderr,
                            flush=True,
                        )
                        result_object_store_error_logged = True
            _stage_hybrid_result_objects(new_tests, result_object_store)
            proposal_object_id = None
            proposal_object_size = None
            if proposal_content is not None and result_object_store is not None:
                try:
                    proposal_object_id, _proposal_path = result_object_store.put(
                        proposal_content
                    )
                    proposal_object_size = len(proposal_content)
                    proposal_content = None
                except (OSError, ValueError):
                    # The bounded legacy field preserves proposal validation if
                    # the shared result store is transiently unavailable.
                    pass

            result = {
                "new_tests": new_tests,  # 只含 interesting 的 TC
                "total_generated": total_gen,  # 总生成数（含被过滤的）
                "input_object_id": materialized_object_id,
                "result_budget_error": result_budget_error,
                "retcode": retcode,
                "elapsed": elapsed,
                "killed": killed,
                "timeout_sites": timeout_sites,
                "telemetry": asdict(telemetry) if telemetry is not None else None,
                "strategy": strategy,
                "executor": execution_route.executor,
                "engine": execution_route.engine,
                "s2f_actions": [[branch, action] for branch, action in s2f_actions],
                "lease_id": lease_id,
                "lease_fence": lease_fence,
                "state_task_id": state_task_id,
                "proposal_id": proposal_id,
                "proposal_content": proposal_content,
                "proposal_object_id": proposal_object_id,
                "proposal_object_size": proposal_object_size,
                "proposal_bitmap": proposal_bitmap,
                "parameter_token": parameter_token,
                "parameter_overrides": parameter_overrides,
                "tace_profiled": tace_profiled,
                "tace_focused": bool(worker_env.get("SYMCC_FOCUS_SET"))
                and not bool(focus_set),
                "schedule_prefix": list(schedule_prefix),
                "schedule_trace": schedule_trace,
            }
        except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as e:
            # worker 弹性边界：I/O / 子进程 / 解析 / 运行时错误不应拖垮整个 MPI 作业，
            # 回传错误结果并继续。真正意外的异常（编程 bug）仍会向上抛出以暴露问题。
            print(f"[Worker {rank}] Error: {e}", file=sys.stderr)
            result = {
                "new_tests": [],
                "total_generated": 0,
                "input_object_id": materialized_object_id,
                "retcode": -1,
                "elapsed": 0,
                "killed": False,
                "lease_id": lease_id,
                "lease_fence": lease_fence,
                "state_task_id": state_task_id,
                "proposal_id": proposal_id,
                "parameter_token": parameter_token,
                "parameter_overrides": parameter_overrides,
                "schedule_prefix": list(schedule_prefix),
            }

        # Clean up output
        shutil.rmtree(run_output, ignore_errors=True)
        if schedule_trace_path:
            try:
                os.unlink(schedule_trace_path)
            except OSError:
                pass
        if schedule_prefix_file:
            try:
                os.unlink(schedule_prefix_file)
            except OSError:
                pass

        # Send result
        _t = time.monotonic() if _wprof else 0.0
        completed_dispatch_token = _send_dispatch_result(comm, result, dispatch_token)
        if _wprof:
            _pt["send"] += time.monotonic() - _t
            # 每个工作项后落盘一次（覆盖写,~ms 级）：编排层 SIGTERM→SIGKILL 常在 worker 阻塞于
            # showmap/子进程等原生调用时到达,信号处理器来不及跑;增量落盘保证数据不丢。
            _flush_prof()

    if _wprof:
        _flush_prof()  # 正常收到 TAG_STOP 退出时也落盘
    if streaming_sm is not None:
        streaming_sm.close()
    if worker_live_executor is not None:
        worker_live_executor.close()
    shutil.rmtree(worker_dir, ignore_errors=True)
    _send_shutdown_ack(comm, rank, shutdown_token)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="MPI-parallel SymCC + AFL fuzzing helper",
        usage="mpirun -np <N> python3 %(prog)s -a FUZZER -o DIR -n NAME -- TARGET [ARGS...]",
    )
    parser.add_argument(
        "-a", "--fuzzer-name", required=True, help="AFL fuzzer instance name"
    )
    parser.add_argument(
        "-o", "--output-dir", required=True, help="AFL output directory"
    )
    parser.add_argument(
        "-n", "--name", required=True, help="Name for this SymCC instance"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    parser.add_argument(
        "--save-all",
        default=None,
        metavar="DIR",
        help="保存所有 SymCC 生成的测试用例到指定目录（不经 afl-showmap 过滤）",
    )
    parser.add_argument(
        "--afl-sync-dir",
        default=None,
        metavar="DIR",
        help="把master判新的SymCC用例原子发布到独立AFL foreign queue；调用方必须"
        "在启动afl-fuzz主实例时以-F注册同一目录",
    )
    parser.add_argument(
        "--grimoire-feed",
        default=None,
        metavar="DIR",
        help="GRIMOIRE 高价值（结构有效、覆盖率增益）输入目录；master 会把其中"
        "新文件直接注入 SymCC 反馈队列，让 concolic 从深层结构输入继续挖",
    )
    parser.add_argument(
        "target", nargs=argparse.REMAINDER, help="Target command (after '--')"
    )

    args = parser.parse_args()

    if args.afl_sync_dir:
        args.afl_sync_dir = os.path.abspath(args.afl_sync_dir)
        internal_queues = {
            os.path.realpath(os.path.join(args.output_dir, args.fuzzer_name, "queue")),
            os.path.realpath(os.path.join(args.output_dir, args.name, "queue")),
        }
        if os.path.realpath(args.afl_sync_dir) in internal_queues:
            parser.error(
                "--afl-sync-dir must be an independent -F foreign queue, "
                "not an AFL or SymCC instance queue"
            )

    if args.target and args.target[0] == "--":
        args.target = args.target[1:]

    if not args.target:
        parser.error("No target command. Use: -- TARGET [ARGS...]")

    return args


def _WPROF_PHASES() -> list:
    return ["wait", "bmsync", "import", "exec", "showmap_dedup", "send"]


def aggregate_phase_timing(prof_dir: str) -> "str | None":
    """把各 worker 写出的 phase_timing_rank*.csv 汇总成 phase_timing.csv（每 worker 一行
    + TOTAL + MEAN_PCT），返回汇总文件路径。供跑完后离线聚合（worker 是被 SIGTERM 杀掉的，
    无法在进程内做 MPI gather，故各自落盘、事后合并）。"""
    phases = _WPROF_PHASES()
    rows = []
    try:
        names = sorted(
            n
            for n in os.listdir(prof_dir)
            if n.startswith("phase_timing_rank") and n.endswith(".csv")
        )
    except OSError:
        return None
    for n in names:
        try:
            with open(os.path.join(prof_dir, n)) as f:
                parts = f.readline().strip().split(",")
            if len(parts) >= 2 + len(phases):
                rows.append(parts)
        except (IOError, OSError, ValueError):
            continue
    if not rows:
        return None
    totals = [0.0] * len(phases)
    n_items = 0
    for r in rows:
        n_items += int(r[1])
        for i in range(len(phases)):
            totals[i] += float(r[2 + i])
    grand = sum(totals) or 1.0
    out = os.path.join(prof_dir, "phase_timing.csv")
    with open(out, "w") as f:
        f.write("rank,items," + ",".join(phases) + ",total_s\n")
        for r in rows:
            tot = sum(float(r[2 + i]) for i in range(len(phases)))
            f.write(",".join(r[: 2 + len(phases)]) + f",{tot:.4f}\n")
        f.write(
            f"TOTAL,{n_items},"
            + ",".join(f"{t:.4f}" for t in totals)
            + f",{grand:.4f}\n"
        )
        f.write(
            "MEAN_PCT,,"
            + ",".join(f"{100 * t / grand:.1f}" for t in totals)
            + ",100.0\n"
        )
    return out


def aggregate_redundancy(prof_dir: str) -> "str | None":
    """合并各 worker 的 redun_rank*.csv 与 redun_master.csv，产出 #10 重复求解拆分报告。
    两层拆分：
      (A) worker-内部冗余 vs worker-间冗余 vs 有效(accepted)，均以生成总数为分母；
      (B) 冗余的两类根因：乐观求解不可行(infeasible,没打到新边) vs 新鲜度间隙(freshness,
          打到新边但已被覆盖=worker 内自复 + worker 间被抢先)。"""
    gen = reported = infeasible = worker_fresh = showmap_none = 0
    tot_items = tot_snap_none = byte_dup = 0
    nworkers = 0
    try:
        names = [
            n
            for n in os.listdir(prof_dir)
            if n.startswith("redun_rank") and n.endswith(".csv")
        ]
    except OSError:
        return None
    for n in names:
        try:
            with open(os.path.join(prof_dir, n)) as f:
                p = f.readline().strip().split(",")
            if len(p) >= 6:
                gen += int(p[1])
                reported += int(p[2])
                infeasible += int(p[3])
                worker_fresh += int(p[4])
                showmap_none += int(p[5])
                if len(p) >= 8:
                    tot_items += int(p[6])
                    tot_snap_none += int(p[7])
                if len(p) >= 9:
                    byte_dup += int(p[8])  # 字节相同被预去重跳过 showmap 的数量
                nworkers += 1
        except (IOError, OSError, ValueError):
            continue
    if gen == 0:
        return None
    accepted = None
    try:
        with open(os.path.join(prof_dir, "redun_master.csv")) as f:
            f.readline()  # header
            accepted = int(f.readline().strip().split(",")[1])
    except (IOError, OSError, ValueError, IndexError):
        accepted = None
    # accepted 不可得时,退化用 reported 作上界(worker-间冗余记为未知)
    acc = accepted if accepted is not None else reported
    worker_internal = gen - reported  # worker 自身 dedup 丢掉的
    worker_between = max(
        0, reported - acc
    )  # master 全局 dedup 丢掉的 = bitmap 新鲜度间隙
    redundant = worker_internal + worker_between
    out = os.path.join(prof_dir, "redundancy.csv")
    with open(out, "w") as f:
        f.write(
            f"# #10 重复求解拆分 (workers={nworkers}, accepted=master interesting_count)\n"
        )
        f.write(
            "# 漏斗: generated → reported(过 worker 自身 dedup) → accepted(过 master 全局 dedup)\n"
        )
        f.write("stage,count,pct_of_generated\n")
        f.write(f"generated,{gen},100.0\n")
        f.write(f"reported(worker判新上报),{reported},{100 * reported / gen:.1f}\n")
        f.write(f"accepted(master全局判新),{acc},{100 * acc / gen:.1f}\n")
        f.write("\n# 冗余(generated-accepted)按发生位置拆分\n")
        f.write("where,count,pct_of_generated,pct_of_redundant\n")
        f.write(
            f"worker_internal(worker内自复),{worker_internal},"
            f"{100 * worker_internal / gen:.1f},{100 * worker_internal / max(1, redundant):.1f}\n"
        )
        # worker_internal 中【字节完全相同】的一类：已由内容级预去重跳过 showmap(纯节省,不影响正确性)
        f.write(
            f"  └ 其中 byte_identical(已跳过showmap),{byte_dup},"
            f"{100 * byte_dup / gen:.1f},{100 * byte_dup / max(1, redundant):.1f}\n"
        )
        f.write(
            f"worker_between(worker间/bitmap新鲜度间隙),{worker_between},"
            f"{100 * worker_between / gen:.1f},{100 * worker_between / max(1, redundant):.1f}\n"
        )
        # 根因子拆分仅当全局快照可用时才有意义（tot_snap_none < tot_items）
        snap_ok = tot_items - tot_snap_none
        f.write("\n# 根因子拆分 (需 SymCC 运行前的全局位图快照)\n")
        f.write(f"# items={tot_items}, 有全局快照的 items={snap_ok}\n")
        if snap_ok > 0:
            f.write("cause,count\n")
            f.write(f"optimistic_infeasible(没打到任何全局新边),{infeasible}\n")
            f.write(f"freshness_within_worker(打到新边但本item内自复),{worker_fresh}\n")
        else:
            f.write(
                "# 本次运行 worker 未获全局位图播种(每 worker 仅 1 个长 item,先于 master 写\n"
            )
            f.write(
                "# .shared_bitmap 完成),无法可靠区分 乐观求解不可行 vs 新鲜度间隙。\n"
            )
            f.write(
                "# 可测下界: worker_between 即 bitmap 新鲜度间隙(跨 worker)部分。\n"
            )
    return out


def _run_mpi_role(comm: typing.Any, args: argparse.Namespace, rank: int) -> bool:
    """Run one rank behind a coordinator-wide failure boundary."""
    if rank != 0:
        worker(comm, args)
        return True
    try:
        return master(comm, args)
    except BaseException as error:
        print(
            f"[Master] fatal coordinator error: {type(error).__name__}: {error}",
            file=sys.stderr,
            flush=True,
        )
        traceback.print_exc(file=sys.stderr)
        shutdown_grace = _bounded_mpi_timeout(
            os.environ.get("SYMCC_SHUTDOWN_GRACE_SEC", "120"),
            120.0,
        )
        try:
            shutdown = _cooperative_shutdown_workers(
                comm,
                range(1, comm.Get_size()),
                grace=shutdown_grace,
            )
            print(
                "[Master] exception shutdown: "
                f"acked={len(shutdown['acknowledged'])}/"
                f"{max(0, comm.Get_size() - 1)} "
                f"pending={list(shutdown['pending'])}",
                file=sys.stderr,
                flush=True,
            )
        except BaseException as shutdown_error:
            print(
                "[Master] exception shutdown failed: "
                f"{type(shutdown_error).__name__}: {shutdown_error}",
                file=sys.stderr,
                flush=True,
            )
        return False


def main() -> None:
    stack_after = os.environ.get("SYMCC_DEBUG_STACK_AFTER_SEC", "")
    if stack_after:
        try:
            import faulthandler

            faulthandler.dump_traceback_later(
                max(1.0, float(stack_after)),
                repeat=True,
                file=sys.stderr,
            )
        except (ImportError, OSError, ValueError):
            pass

    comm = MPI.COMM_WORLD
    rank = comm.Get_rank()

    # 高并行度下把本 rank 钉到编排层预留的核（与 AFL 自动绑核互斥），消除核争用/迁移
    _pin_self_to_reserved_core(rank)

    args = parse_args()

    lifecycle_clean = _run_mpi_role(comm, args, rank)

    if rank == 0 and not lifecycle_clean:
        print(
            "[Master] Coordinator failed or shutdown was incomplete; "
            "aborting MPI job with a non-zero status.",
            file=sys.stderr,
            flush=True,
        )
        comm.Abort(70)
        return

    finalize_grace = _bounded_mpi_timeout(
        os.environ.get("SYMCC_FINALIZE_GRACE_SEC", "30"),
        30.0,
    )
    if not _bounded_mpi_barrier(comm, finalize_grace):
        if rank == 0:
            print(
                "[Master] Final MPI barrier deadline expired; aborting job.",
                file=sys.stderr,
                flush=True,
            )
        comm.Abort(71)
        return
    MPI.Finalize()


if __name__ == "__main__":
    main()
