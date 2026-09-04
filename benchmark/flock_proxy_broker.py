#!/usr/bin/env python3
"""Benchmark-only bridge from TCP connections to local kernel flock locks."""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import signal
import socket
import socketserver
import struct
import sys
import threading
import time
from typing import BinaryIO


_MAGIC = b"SFLK001\0"
_HEADER = struct.Struct("!8sII")
_RESPONSE = struct.Struct("!I")
_MAX_PATH_BYTES = 4096
_SUPPORTED_OPERATIONS = {
    fcntl.LOCK_SH,
    fcntl.LOCK_EX,
    fcntl.LOCK_SH | fcntl.LOCK_NB,
    fcntl.LOCK_EX | fcntl.LOCK_NB,
}


def _receive_exact(stream: BinaryIO, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.read(size - len(chunks))
        if not chunk:
            raise EOFError("flock proxy request ended early")
        chunks.extend(chunk)
    return bytes(chunks)


class FlockProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], root: str, log: BinaryIO):
        self.root = os.path.realpath(root)
        self.log = log
        self.log_lock = threading.Lock()
        super().__init__(address, FlockProxyHandler)

    def record(self, event: dict[str, object]) -> None:
        event = {"time_ns": time.time_ns(), **event}
        line = json.dumps(event, sort_keys=True, separators=(",", ":"))
        with self.log_lock:
            self.log.write((line + "\n").encode("utf-8"))
            self.log.flush()


class FlockProxyHandler(socketserver.StreamRequestHandler):
    server: FlockProxyServer

    def setup(self) -> None:
        super().setup()
        self.request.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

    def _send_status(self, error_number: int) -> None:
        self.wfile.write(_RESPONSE.pack(error_number))
        self.wfile.flush()

    def handle(self) -> None:
        descriptor = -1
        path = ""
        operation = 0
        try:
            magic, operation, path_size = _HEADER.unpack(
                _receive_exact(self.rfile, _HEADER.size)
            )
            if magic != _MAGIC or not 0 < path_size <= _MAX_PATH_BYTES:
                raise OSError(errno.EPROTO, "invalid flock proxy request")
            if operation not in _SUPPORTED_OPERATIONS:
                raise OSError(errno.EINVAL, "unsupported flock operation")
            path = os.fsdecode(_receive_exact(self.rfile, path_size))
            resolved = os.path.realpath(path)
            if os.path.commonpath((self.server.root, resolved)) != self.server.root:
                raise OSError(errno.EPERM, "lock path is outside the proxy root")
            flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(resolved, flags)
            fcntl.flock(descriptor, operation)
            self._send_status(0)
            self.server.record(
                {
                    "event": "acquired",
                    "operation": operation,
                    "path": os.path.relpath(resolved, self.server.root),
                }
            )
            while self.rfile.read(4096):
                pass
        except (EOFError, OSError, ValueError) as error:
            error_number = (
                error.errno
                if isinstance(error, OSError) and error.errno
                else errno.EPROTO
            )
            try:
                self._send_status(error_number)
            except OSError:
                pass
            self.server.record(
                {
                    "errno": error_number,
                    "event": "rejected",
                    "operation": operation,
                    "path": path,
                }
            )
        finally:
            if descriptor >= 0:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
                self.server.record(
                    {
                        "event": "released",
                        "operation": operation,
                        "path": path,
                    }
                )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--log", required=True)
    arguments = parser.parse_args()
    if not 1 <= arguments.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    root = os.path.realpath(arguments.root)
    if not os.path.isdir(root):
        parser.error("--root must name an existing directory")

    with open(arguments.log, "ab", buffering=0) as log:
        with FlockProxyServer((arguments.host, arguments.port), root, log) as server:
            def request_shutdown(_signum: int, _frame: object) -> None:
                # socketserver.shutdown() must run outside serve_forever().
                threading.Thread(target=server.shutdown, daemon=True).start()

            signal.signal(signal.SIGTERM, request_shutdown)
            print(
                json.dumps(
                    {
                        "host": arguments.host,
                        "port": arguments.port,
                        "root": root,
                        "schema": "symcc-benchmark-flock-proxy-v1",
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            server.serve_forever(poll_interval=0.1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
