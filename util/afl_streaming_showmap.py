#!/usr/bin/env python3
"""Bounded client for the AFL++ streaming showmap protocol."""

from __future__ import annotations

from dataclasses import dataclass
import io
import os
import select
import struct
import subprocess
import time
from typing import BinaryIO, Iterable, Sequence


_EDGE_STRUCT = struct.Struct("<IB")
_STATUS_STRUCT = struct.Struct("<H")
_U32_STRUCT = struct.Struct("<I")
_STATUS_NAMES = {0: "ok", 1: "timeout", 2: "crash"}


def parse_sparse_edge_rows(
    rows: Iterable[str], *, map_size: int
) -> tuple[tuple[int, int], ...]:
    """Parse an AFL text map without accepting ambiguous or invalid rows."""
    if map_size <= 0:
        raise ValueError("map_size must be positive")
    edges: list[tuple[int, int]] = []
    seen: set[int] = set()
    for line_number, raw in enumerate(rows, 1):
        line = raw.strip()
        if not line:
            continue
        parts = line.split(":")
        if (
            len(parts) != 2
            or not parts[0].isdigit()
            or not parts[1].isdigit()
        ):
            raise ValueError(
                f"malformed AFL edge map row {line_number}: {raw!r}"
            )
        edge = int(parts[0], 10)
        hit_count = int(parts[1], 10)
        if edge >= map_size or not 1 <= hit_count <= 255:
            raise ValueError(
                f"out-of-range AFL edge map row {line_number}: {raw!r}"
            )
        if edge in seen:
            raise ValueError(
                f"duplicate AFL edge map row {line_number}: {raw!r}"
            )
        seen.add(edge)
        edges.append((edge, hit_count))
    if not edges:
        raise ValueError("AFL edge map is empty")
    return tuple(edges)


@dataclass(frozen=True)
class StreamingShowmapResult:
    status: str
    status_detail: int
    raw_status: int
    edges: tuple[tuple[int, int], ...]
    stdout: bytes
    stderr: bytes


class StreamingShowmap:
    """Keep one AFL++ forkserver alive for bounded coverage queries."""

    MAX_INPUT_BYTES = 16 * 1024 * 1024
    MAX_EDGES = 1 << 20
    MAX_MAP_SIZE = 1 << 23
    MAX_AUXILIARY_BYTES = 4 * 1024 * 1024
    PROTOCOL_GRACE_SECONDS = 5.0

    def __init__(
        self,
        afl_showmap: str,
        target_cmd: Sequence[str],
        *,
        timeout_ms: int = 5_000,
    ):
        if not target_cmd:
            raise ValueError("target_cmd must not be empty")
        if timeout_ms <= 0:
            raise ValueError("timeout_ms must be positive")
        self._proc: subprocess.Popen[bytes] | None = None
        self._dead = True
        self._afl_showmap = afl_showmap
        self._target_cmd = list(target_cmd)
        self._timeout_ms = timeout_ms
        placeholder = os.environ.get("AFL_INPUT_PLACEHOLDER", "@@") or "@@"
        if any(placeholder in argument for argument in self._target_cmd):
            raise ValueError(
                "AFL++ streaming showmap requires a stdin target command; "
                "file placeholders require isolated measurement"
            )
        self._input_mode = "stdin"
        self._restart_count = 0
        self._spawn()

    @property
    def restart_count(self) -> int:
        return self._restart_count

    @property
    def input_mode(self) -> str:
        return self._input_mode

    def _spawn(self) -> None:
        command = [
            self._afl_showmap,
            "-S",
            "-t",
            str(self._timeout_ms),
            "-m",
            "none",
            "--",
            *self._target_cmd,
        ]
        self._proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        if self._proc.stdin is None or self._proc.stdout is None:
            self.close()
            raise RuntimeError("afl-showmap did not provide protocol pipes")
        self._dead = False

    def restart(self) -> bool:
        self.close()
        try:
            self._spawn()
        except (OSError, RuntimeError, subprocess.SubprocessError):
            self._proc = None
            self._dead = True
            return False
        self._restart_count += 1
        return True

    def _read_exact(
        self,
        stream: BinaryIO,
        length: int,
        deadline: float,
    ) -> bytes | None:
        content = bytearray()
        while len(content) < length:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._dead = True
                return None
            try:
                ready, _, _ = select.select([stream], [], [], remaining)
            except (TypeError, io.UnsupportedOperation):
                # BytesIO-based protocol tests have no descriptor. Real Popen
                # pipes always take the deadline-enforced path above.
                ready = [stream]
            except (OSError, ValueError):
                self._dead = True
                return None
            if not ready:
                self._dead = True
                return None
            block = stream.read(length - len(content))
            if not block:
                self._dead = True
                return None
            content += block
        return bytes(content)

    def _read_bounded_blob(
        self, stream: BinaryIO, deadline: float
    ) -> bytes | None:
        raw_length = self._read_exact(stream, _U32_STRUCT.size, deadline)
        if raw_length is None:
            return None
        length = _U32_STRUCT.unpack(raw_length)[0]
        if length > self.MAX_AUXILIARY_BYTES:
            self._dead = True
            return None
        return self._read_exact(stream, length, deadline)

    def _get_result_once(
        self,
        content: bytes,
        *,
        deadline: float | None = None,
    ) -> StreamingShowmapResult | None:
        if (
            self._dead
            or self._proc is None
            or self._proc.stdin is None
            or self._proc.stdout is None
            or len(content) > self.MAX_INPUT_BYTES
        ):
            return None
        try:
            self._proc.stdin.write(_U32_STRUCT.pack(len(content)))
            self._proc.stdin.write(content)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError, struct.error):
            self._dead = True
            return None

        stream = self._proc.stdout
        protocol_deadline = (
            time.monotonic()
            + self._timeout_ms / 1000.0
            + self.PROTOCOL_GRACE_SECONDS
        )
        if deadline is not None:
            protocol_deadline = min(protocol_deadline, deadline)
        encoded_status = self._read_exact(
            stream, _STATUS_STRUCT.size, protocol_deadline
        )
        encoded_count = self._read_exact(
            stream, _U32_STRUCT.size, protocol_deadline
        )
        if encoded_status is None or encoded_count is None:
            return None
        raw_status = _STATUS_STRUCT.unpack(encoded_status)[0]
        exit_kind = raw_status & 0x3
        reserved = (raw_status >> 2) & 0x3F
        status = _STATUS_NAMES.get(exit_kind)
        if status is None or reserved:
            self._dead = True
            return None

        edge_count = _U32_STRUCT.unpack(encoded_count)[0]
        if edge_count > self.MAX_EDGES:
            self._dead = True
            return None
        encoded_edges = self._read_exact(
            stream, _EDGE_STRUCT.size * edge_count, protocol_deadline
        )
        if encoded_edges is None:
            return None
        edges = tuple(_EDGE_STRUCT.iter_unpack(encoded_edges))
        seen_edges: set[int] = set()
        for edge, hit_count in edges:
            if (
                edge >= self.MAX_MAP_SIZE
                or hit_count == 0
                or edge in seen_edges
            ):
                self._dead = True
                return None
            seen_edges.add(edge)
        stdout = self._read_bounded_blob(stream, protocol_deadline)
        stderr = self._read_bounded_blob(stream, protocol_deadline)
        if stdout is None or stderr is None:
            return None
        return StreamingShowmapResult(
            status=status,
            status_detail=raw_status >> 8,
            raw_status=raw_status,
            edges=edges,
            stdout=stdout,
            stderr=stderr,
        )

    def get_result(
        self,
        content: bytes,
        *,
        deadline: float | None = None,
    ) -> StreamingShowmapResult | None:
        if len(content) > self.MAX_INPUT_BYTES:
            return None
        if deadline is not None and time.monotonic() >= deadline:
            return None
        restarted = False
        if self._dead:
            if not self.restart():
                return None
            restarted = True
        result = self._get_result_once(content, deadline=deadline)
        if result is not None or restarted:
            return result
        if deadline is not None and time.monotonic() >= deadline:
            return None
        if not self.restart():
            return None
        return self._get_result_once(content, deadline=deadline)

    def get_edges(self, content: bytes) -> list[tuple[int, int]] | None:
        """Return ordinary coverage only for a normal, nonempty execution."""
        result = self.get_result(content)
        return (
            list(result.edges)
            if result is not None and result.status == "ok" and result.edges
            else None
        )

    def close(self) -> None:
        process = self._proc
        self._proc = None
        self._dead = True
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()
            process.terminate()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def __enter__(self) -> StreamingShowmap:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()
