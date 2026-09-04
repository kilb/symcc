#!/usr/bin/env python3
"""Shared, fail-closed measurement primitives for the QA3 experiments."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterator, Mapping


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "util"))

from afl_streaming_showmap import (  # noqa: E402
    StreamingShowmap,
    parse_sparse_edge_rows,
)


UINT64_MAX = (1 << 64) - 1
MAX_TELEMETRY_BYTES = 1024 * 1024
MAX_INPUT_BYTES = StreamingShowmap.MAX_INPUT_BYTES
SHOWMAP_RETURN_STATUS = {
    0: "ok",
    1: "timeout",
    2: "crash",
}
AFL_INPUT_MODES = {"auto", "file", "stdin"}
TERMINAL_STATUS_POLICIES = {"normal-only", "stratified"}
_AFL_STDIN_SIGNATURES = (
    b"##SIG_AFL_PERSISTENT##",
    b"##SIG_AFL_SHM_FUZZ##",
)
_SHOWMAP_STATUSES = frozenset(SHOWMAP_RETURN_STATUS.values())


class MeasurementError(RuntimeError):
    """Raised when an experiment cannot produce trustworthy measurements."""


def detect_afl_input_mode(
    afl_binary: str | Path, requested: str = "auto"
) -> str:
    """Resolve file-vs-stdin delivery without loading the whole binary."""
    if requested not in AFL_INPUT_MODES:
        raise ValueError("input mode must be auto, file, or stdin")
    if requested != "auto":
        return requested
    binary = Path(afl_binary)
    overlap = max(len(signature) for signature in _AFL_STDIN_SIGNATURES) - 1
    tail = b""
    try:
        with binary.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                window = tail + block
                if any(signature in window for signature in _AFL_STDIN_SIGNATURES):
                    return "stdin"
                tail = window[-overlap:]
    except OSError as error:
        raise MeasurementError(
            f"cannot inspect AFL binary {binary}: {error}"
        ) from error
    return "file"


def coverage_status_is_eligible(
    status: str, *, policy: str, label: str
) -> bool:
    """Keep abnormal terminal states out of ordinary coverage metrics."""
    if policy not in TERMINAL_STATUS_POLICIES:
        raise ValueError(
            "terminal status policy must be normal-only or stratified"
        )
    if status not in _SHOWMAP_STATUSES:
        raise MeasurementError(f"unknown AFL terminal status for {label}: {status}")
    if status == "ok":
        return True
    if policy == "normal-only":
        raise MeasurementError(
            f"abnormal AFL terminal status for {label}: {status}"
        )
    return False


@dataclass(frozen=True)
class EdgeMeasurement:
    edges: frozenset[int]
    status: str
    returncode: int


@dataclass(frozen=True)
class ReplicatedEdgeMeasurement:
    edges: frozenset[int]
    status: str
    replicas: int
    unstable_edges: int
    union_edges: int
    intersection_edges: int


@dataclass(frozen=True)
class InterleavedCorpusMeasurement:
    measurements: dict[Path, ReplicatedEdgeMeasurement]
    samples: dict[Path, tuple[ReplicatedEdgeMeasurement, ...]]
    orderings: tuple[tuple[Path, ...], ...]


@dataclass(frozen=True)
class SolverCounts:
    queries: int
    time_us: int


class StreamingCoverageOracle:
    """Replicated coverage measurements in one persistent AFL++ session."""

    def __init__(
        self,
        afl_binary: str | Path,
        *,
        repeats: int = 3,
        stability_policy: str = "strict",
        timeout_ms: int = 5_000,
        showmap_binary: str = "afl-showmap",
        input_mode: str = "auto",
    ) -> None:
        if repeats <= 0:
            raise ValueError("repeats must be positive")
        if stability_policy not in {"strict", "intersection", "union"}:
            raise ValueError(
                "stability_policy must be strict, intersection, or union"
            )
        binary = Path(afl_binary).resolve()
        self._input_mode = detect_afl_input_mode(binary, input_mode)
        self._afl_binary = binary
        self._timeout_ms = timeout_ms
        self._showmap_binary = showmap_binary
        self._fallbacks = 0
        self._retired_restarts = 0
        self._probe_observations = 0
        self._probe: dict[str, Any] = {"result": "not-run"}
        self._mode = "probing"
        self._oracle: StreamingShowmap | None = None
        if self._input_mode == "file":
            # AFL++ -S transports bytes, not per-testcase filenames. Passing
            # @@ to the installed showmap version selects an incompatible
            # target path, so file ABI measurements remain isolated.
            self._mode = "one-shot"
            self._probe = {"result": "file-input-requires-one-shot"}
            self.repeats = repeats
            self.stability_policy = stability_policy
            return
        try:
            self._oracle = StreamingShowmap(
                showmap_binary,
                [str(binary)],
                timeout_ms=timeout_ms,
            )
        except (OSError, RuntimeError, subprocess.SubprocessError):
            self._mode = "one-shot"
            self._fallbacks = 1
            self._probe = {"result": "stream-start-failed"}
        self.repeats = repeats
        self.stability_policy = stability_policy

    @property
    def restart_count(self) -> int:
        return self._retired_restarts + (
            self._oracle.restart_count if self._oracle is not None else 0
        )

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def fallbacks(self) -> int:
        return self._fallbacks

    @property
    def input_mode(self) -> str:
        return self._input_mode

    @property
    def probe_observations(self) -> int:
        return self._probe_observations

    @property
    def probe(self) -> dict[str, Any]:
        return dict(self._probe)

    def _retire_streaming(self) -> None:
        if self._oracle is not None:
            self._retired_restarts += self._oracle.restart_count
            self._oracle.close()
            self._oracle = None

    def _one_shot(self, path: Path) -> ReplicatedEdgeMeasurement:
        return measure_edges_repeated(
            self._afl_binary,
            path,
            repeats=self.repeats,
            stability_policy=self.stability_policy,
            timeout_ms=self._timeout_ms,
            showmap_binary=self._showmap_binary,
            input_mode=self._input_mode,
        )

    def measure(self, input_path: str | Path) -> ReplicatedEdgeMeasurement:
        path = Path(input_path)
        if self._mode == "one-shot":
            return self._one_shot(path)
        content = read_bounded_input(path)
        results = []
        assert self._oracle is not None
        first = self._oracle.get_result(content)
        self._probe_observations += 1 if self._mode == "probing" else 0
        if first is None:
            self._probe = {"result": "stream-query-failed"}
            self._retire_streaming()
            self._mode = "one-shot"
            self._fallbacks += 1
            return self._one_shot(path)
        results.append(first)
        if self._mode == "probing":
            isolated = measure_edges(
                self._afl_binary,
                path,
                timeout_ms=self._timeout_ms,
                showmap_binary=self._showmap_binary,
                input_mode=self._input_mode,
            )
            self._probe_observations += 1
            streaming_edges = frozenset(
                edge for edge, _count in first.edges
            )
            self._probe = {
                "streaming_status": first.status,
                "streaming_status_detail": first.status_detail,
                "streaming_edges": len(streaming_edges),
                "isolated_status": isolated.status,
                "isolated_edges": len(isolated.edges),
            }
            if (
                isolated.status != first.status
                or isolated.edges != streaming_edges
            ):
                reasons = []
                if isolated.status != first.status:
                    reasons.append("status-mismatch")
                if isolated.edges != streaming_edges:
                    reasons.append("edge-set-mismatch")
                self._probe["result"] = "+".join(reasons)
                self._retire_streaming()
                self._mode = "one-shot"
                self._fallbacks += 1
                return self._one_shot(path)
            self._probe["result"] = "compatible"
            self._mode = "streaming"
        for _ in range(1, self.repeats):
            result = self._oracle.get_result(content)
            if result is None:
                self._retire_streaming()
                self._mode = "one-shot"
                self._fallbacks += 1
                return self._one_shot(path)
            results.append(result)
        raw_statuses = {result.raw_status for result in results}
        if len(raw_statuses) != 1:
            rendered = ",".join(
                f"{result.status}:{result.status_detail}" for result in results
            )
            raise MeasurementError(
                "afl-showmap terminal status changed across replicas: "
                + rendered
            )
        edge_sets = [
            frozenset(edge for edge, _count in result.edges)
            for result in results
        ]
        if any(not edges for edges in edge_sets):
            raise MeasurementError(f"AFL edge map is empty for {path}")
        union = set().union(*edge_sets)
        intersection = set(edge_sets[0])
        for edges in edge_sets[1:]:
            intersection.intersection_update(edges)
        unstable_edges = len(union - intersection)
        if self.stability_policy == "strict" and unstable_edges:
            sizes = ",".join(str(len(edges)) for edges in edge_sets)
            raise MeasurementError(
                "AFL edge set changed across replicas "
                f"(sizes={sizes}, unstable_edges={unstable_edges})"
            )
        selected = (
            union if self.stability_policy == "union" else intersection
        )
        if not selected:
            raise MeasurementError(
                f"AFL {self.stability_policy} edge set is empty across replicas"
            )
        return ReplicatedEdgeMeasurement(
            edges=frozenset(selected),
            status=results[0].status,
            replicas=self.repeats,
            unstable_edges=unstable_edges,
            union_edges=len(union),
            intersection_edges=len(intersection),
        )

    def close(self) -> None:
        self._retire_streaming()

    def __enter__(self) -> StreamingCoverageOracle:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def reconcile_edge_measurements(
    measurements: list[ReplicatedEdgeMeasurement],
    *,
    stability_policy: str,
    label: str,
) -> ReplicatedEdgeMeasurement:
    if not measurements:
        raise ValueError("measurements must not be empty")
    if stability_policy not in {"strict", "intersection", "union"}:
        raise ValueError(
            "stability_policy must be strict, intersection, or union"
        )
    statuses = {measurement.status for measurement in measurements}
    if len(statuses) != 1:
        raise MeasurementError(
            f"afl-showmap terminal status changed for {label}: "
            + ",".join(measurement.status for measurement in measurements)
        )
    edge_sets = [measurement.edges for measurement in measurements]
    union = set().union(*edge_sets)
    intersection = set(edge_sets[0])
    for edges in edge_sets[1:]:
        intersection.intersection_update(edges)
    unstable_edges = len(union - intersection)
    if stability_policy == "strict" and unstable_edges:
        sizes = ",".join(str(len(edges)) for edges in edge_sets)
        raise MeasurementError(
            f"AFL edge set changed across interleaved rounds for {label} "
            f"(sizes={sizes}, unstable_edges={unstable_edges})"
        )
    selected = union if stability_policy == "union" else intersection
    if not selected:
        raise MeasurementError(
            f"AFL {stability_policy} edge set is empty for {label}"
        )
    return ReplicatedEdgeMeasurement(
        edges=frozenset(selected),
        status=measurements[0].status,
        replicas=sum(item.replicas for item in measurements),
        unstable_edges=unstable_edges,
        union_edges=len(union),
        intersection_edges=len(intersection),
    )


def measure_corpus_interleaved(
    oracle: StreamingCoverageOracle,
    paths: list[Path],
    *,
    rounds: int,
    stability_policy: str,
    round_delay_seconds: float = 1.0,
) -> InterleavedCorpusMeasurement:
    """Replicate a corpus in rounds to expose temporal and order effects."""
    if rounds <= 0:
        raise ValueError("rounds must be positive")
    if round_delay_seconds < 0:
        raise ValueError("round_delay_seconds must not be negative")
    unique_paths = list(dict.fromkeys(path.resolve() for path in paths))
    if not unique_paths:
        return InterleavedCorpusMeasurement({}, {}, ())
    samples: dict[Path, list[ReplicatedEdgeMeasurement]] = {
        path: [] for path in unique_paths
    }
    orderings: list[tuple[Path, ...]] = []
    for round_index in range(rounds):
        offset = round_index % len(unique_paths)
        ordering = unique_paths[offset:] + unique_paths[:offset]
        orderings.append(tuple(ordering))
        for path in ordering:
            samples[path].append(oracle.measure(path))
        if round_index + 1 < rounds and round_delay_seconds:
            time.sleep(round_delay_seconds)
    measurements = {
        path: reconcile_edge_measurements(
            measurements,
            stability_policy=stability_policy,
            label=str(path),
        )
        for path, measurements in samples.items()
    }
    return InterleavedCorpusMeasurement(
        measurements=measurements,
        samples={path: tuple(items) for path, items in samples.items()},
        orderings=tuple(orderings),
    )


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
    except OSError as error:
        raise MeasurementError(f"cannot hash {path}: {error}") from error
    return digest.hexdigest()


def read_bounded_input(path: str | Path) -> bytes:
    """Read one test case under the streaming protocol's hard size limit."""
    candidate = Path(path)
    try:
        with candidate.open("rb") as stream:
            content = stream.read(MAX_INPUT_BYTES + 1)
    except OSError as error:
        raise MeasurementError(f"cannot read input {candidate}: {error}") from error
    if len(content) > MAX_INPUT_BYTES:
        raise MeasurementError(
            f"input {candidate} exceeds {MAX_INPUT_BYTES} bytes"
        )
    return content


def prefix_depth(path: str | Path, depth: int) -> int:
    try:
        content = Path(path).read_bytes()
    except OSError as error:
        raise MeasurementError(f"cannot read candidate {path}: {error}") from error
    matched = 0
    while (
        matched < depth
        and matched < len(content)
        and content[matched] == 0x41 + matched
    ):
        matched += 1
    return matched


def iter_output_files(directory: str | Path) -> Iterator[Path]:
    try:
        entries = sorted(Path(directory).iterdir(), key=lambda path: path.name)
    except OSError as error:
        raise MeasurementError(
            f"cannot enumerate output directory {directory}: {error}"
        ) from error
    for path in entries:
        if path.name.endswith(".hints") or not path.is_file():
            continue
        yield path


def strategy_tag(filename: str, default: str) -> str:
    return filename.split("-", 1)[1] if "-" in filename else default


def _canonical_uint64(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MeasurementError(f"telemetry field {field!r} is not an integer")
    if value < 0 or value > UINT64_MAX:
        raise MeasurementError(f"telemetry field {field!r} is out of range")
    return value


def load_solver_counts(path: str | Path) -> SolverCounts:
    telemetry_path = Path(path)
    try:
        with telemetry_path.open("rb") as stream:
            encoded = stream.read(MAX_TELEMETRY_BYTES + 1)
        if len(encoded) > MAX_TELEMETRY_BYTES:
            raise MeasurementError(
                f"solver telemetry {telemetry_path} exceeds "
                f"{MAX_TELEMETRY_BYTES} bytes"
            )
        raw = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise MeasurementError(
            f"cannot read solver telemetry {telemetry_path}: {error}"
        ) from error
    if not isinstance(raw, Mapping):
        raise MeasurementError(
            f"solver telemetry {telemetry_path} is not a JSON object"
        )
    return SolverCounts(
        queries=_canonical_uint64(raw.get("solver_queries"), "solver_queries"),
        time_us=_canonical_uint64(raw.get("solver_time_us"), "solver_time_us"),
    )


def parse_edge_map(path: str | Path, *, map_size: int) -> frozenset[int]:
    if map_size <= 0:
        raise ValueError("map_size must be positive")
    try:
        lines = Path(path).read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as error:
        raise MeasurementError(f"cannot read AFL edge map {path}: {error}") from error

    try:
        return frozenset(
            edge for edge, _hit_count in parse_sparse_edge_rows(
                lines, map_size=map_size
            )
        )
    except ValueError as error:
        raise MeasurementError(str(error)) from error


def measure_edges(
    afl_binary: str | Path,
    input_path: str | Path,
    *,
    timeout_ms: int = 5_000,
    map_size: int = 65_536,
    showmap_binary: str = "afl-showmap",
    temporary_directory: str | Path | None = None,
    input_mode: str = "auto",
) -> EdgeMeasurement:
    if timeout_ms <= 0:
        raise ValueError("timeout_ms must be positive")
    if map_size <= 0:
        raise ValueError("map_size must be positive")
    binary = Path(afl_binary).resolve()
    mode = detect_afl_input_mode(binary, input_mode)
    content = read_bounded_input(input_path)
    directory = None if temporary_directory is None else str(temporary_directory)
    with tempfile.NamedTemporaryFile(
        prefix="qa3-showmap-", suffix=".map", dir=directory, delete=False
    ) as temporary:
        map_path = Path(temporary.name)
    command = [
        showmap_binary,
        "-t",
        str(timeout_ms),
        "-m",
        "none",
        "-q",
        "-o",
        str(map_path),
        "--",
        str(binary),
    ]
    run_input: bytes | None
    if mode == "file":
        command.append(str(Path(input_path).resolve()))
        run_input = None
    else:
        run_input = content
    environment = os.environ.copy()
    environment["AFL_MAP_SIZE"] = str(map_size)
    try:
        try:
            invocation = {
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.PIPE,
                "env": environment,
                "check": False,
                "timeout": timeout_ms / 1000.0 + 10.0,
            }
            if run_input is None:
                completed = subprocess.run(
                    command, stdin=subprocess.DEVNULL, **invocation
                )
            else:
                completed = subprocess.run(
                    command, input=run_input, **invocation
                )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise MeasurementError(f"afl-showmap invocation failed: {error}") from error
        status = SHOWMAP_RETURN_STATUS.get(completed.returncode)
        if status is None:
            raw_detail = completed.stderr or b""
            detail = (
                raw_detail.decode("utf-8", errors="replace")
                if isinstance(raw_detail, bytes)
                else str(raw_detail)
            ).strip()[-1000:]
            suffix = f": {detail}" if detail else ""
            raise MeasurementError(
                f"afl-showmap exited with {completed.returncode}{suffix}"
            )
        edges = parse_edge_map(map_path, map_size=map_size)
        return EdgeMeasurement(
            edges=edges,
            status=status,
            returncode=completed.returncode,
        )
    finally:
        try:
            map_path.unlink()
        except FileNotFoundError:
            pass


def measure_edges_repeated(
    afl_binary: str | Path,
    input_path: str | Path,
    *,
    repeats: int = 3,
    stability_policy: str = "strict",
    timeout_ms: int = 5_000,
    map_size: int = 65_536,
    showmap_binary: str = "afl-showmap",
    temporary_directory: str | Path | None = None,
    input_mode: str = "auto",
) -> ReplicatedEdgeMeasurement:
    """Measure one input repeatedly and make edge instability explicit."""
    if repeats <= 0:
        raise ValueError("repeats must be positive")
    if stability_policy not in {"strict", "intersection", "union"}:
        raise ValueError(
            "stability_policy must be strict, intersection, or union"
        )
    measurements = [
        measure_edges(
            afl_binary,
            input_path,
            timeout_ms=timeout_ms,
            map_size=map_size,
            showmap_binary=showmap_binary,
            temporary_directory=temporary_directory,
            input_mode=input_mode,
        )
        for _ in range(repeats)
    ]
    statuses = {measurement.status for measurement in measurements}
    if len(statuses) != 1:
        raise MeasurementError(
            "afl-showmap terminal status changed across replicas: "
            + ",".join(measurement.status for measurement in measurements)
        )
    union = set().union(*(measurement.edges for measurement in measurements))
    intersection = set(measurements[0].edges)
    for measurement in measurements[1:]:
        intersection.intersection_update(measurement.edges)
    unstable_edges = len(union - intersection)
    if stability_policy == "strict" and unstable_edges:
        sizes = ",".join(str(len(item.edges)) for item in measurements)
        raise MeasurementError(
            "AFL edge set changed across replicas "
            f"(sizes={sizes}, unstable_edges={unstable_edges})"
        )
    selected = union if stability_policy == "union" else intersection
    if not selected:
        raise MeasurementError(
            f"AFL {stability_policy} edge set is empty across replicas"
        )
    return ReplicatedEdgeMeasurement(
        edges=frozenset(selected),
        status=measurements[0].status,
        replicas=repeats,
        unstable_edges=unstable_edges,
        union_edges=len(union),
        intersection_edges=len(intersection),
    )
