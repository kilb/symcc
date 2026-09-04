#!/usr/bin/env python3
"""Bounded end-to-end PalRUP checking with a content-addressed receipt.

This module runs the three official SAT 2026 checker stages over immutable
input snapshots.  A successful process exit is necessary but insufficient:
the complete confirmation-marker set and an UNSAT witness marker are checked
before a global receipt is issued.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Mapping, Sequence

from qfbv_proof_wire import (
    MAX_EXECUTABLE_BYTES,
    PALRUP_CHECKER_COMMIT,
    ProofWireError,
    _canonical_json,
    _digest,
    _executable_identity,
    _git_commit,
    _hex_digest,
    _integer,
    _run_bounded,
    _write_executable_snapshot,
)


PALRUP_GLOBAL_PROTOCOL = "palrup-sat2026-local-redistribute-confirm-v1"
PALRUP_GLOBAL_POLICY_SCHEMA = "symcc-qfbv-palrup-global-policy-v1"
PALRUP_GLOBAL_RECEIPT_SCHEMA = "symcc-qfbv-palrup-global-receipt-v1"

MAX_PALRUP_SOLVERS = 16_384
MAX_PALRUP_MATRIX_TASKS = 16_384
MAX_PALRUP_FORMULA_BYTES = 1 << 32
MAX_PALRUP_FRAGMENT_BYTES = 1 << 36
MAX_PALRUP_TOTAL_FRAGMENT_BYTES = 1 << 44
MAX_PALRUP_STAGE_ARTIFACT_BYTES = 1 << 40
MAX_PALRUP_PARALLEL_TASKS = 256
MAX_PALRUP_TIMEOUT_MS = 24 * 60 * 60 * 1000
_COPY_CHUNK_BYTES = 1 << 20


@dataclass(frozen=True)
class _Tool:
    name: str
    path: Path
    identity: dict[str, Any]
    content: bytes


@dataclass(frozen=True)
class _Snapshot:
    sha256: str
    size: int

    def metadata(self) -> dict[str, Any]:
        return {"sha256": self.sha256, "bytes": self.size}


def _copy_regular_descriptor(
    descriptor: int,
    destination: Path,
    *,
    maximum: int,
    label: str,
) -> _Snapshot:
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode):
        raise ProofWireError(f"{label} is not a regular file")
    if before.st_size < 0 or before.st_size > maximum:
        raise ProofWireError(f"{label} exceeds its byte bound")
    output = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    digest = hashlib.sha256()
    total = 0
    try:
        while True:
            chunk = os.read(descriptor, _COPY_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                raise ProofWireError(f"{label} exceeds its byte bound")
            digest.update(chunk)
            offset = 0
            while offset < len(chunk):
                written = os.write(output, chunk[offset:])
                if written <= 0:
                    raise OSError(f"short write while snapshotting {label}")
                offset += written
        os.fsync(output)
    finally:
        os.close(output)
    after = os.fstat(descriptor)
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if identity_before != identity_after or total != before.st_size:
        raise ProofWireError(f"{label} changed while it was snapshotted")
    return _Snapshot(digest.hexdigest(), total)


def _snapshot_path(
    source: Path,
    destination: Path,
    *,
    maximum: int,
    label: str,
) -> _Snapshot:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as error:
        raise ProofWireError(f"cannot open {label}") from error
    try:
        return _copy_regular_descriptor(
            descriptor,
            destination,
            maximum=maximum,
            label=label,
        )
    finally:
        os.close(descriptor)


def _snapshot_fragment(
    proof_descriptor: int,
    width: int,
    rank: int,
    destination: Path,
    *,
    maximum: int,
) -> _Snapshot:
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = (
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptors: list[int] = []
    try:
        row = os.open(str(rank // width), directory_flags, dir_fd=proof_descriptor)
        descriptors.append(row)
        worker = os.open(str(rank), directory_flags, dir_fd=row)
        descriptors.append(worker)
        fragment = os.open("out.palrup", file_flags, dir_fd=worker)
        descriptors.append(fragment)
        return _copy_regular_descriptor(
            fragment,
            destination,
            maximum=maximum,
            label=f"PalRUP fragment {rank}",
        )
    except OSError as error:
        raise ProofWireError(f"cannot open PalRUP fragment {rank}") from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _artifact(path: Path, *, maximum: int, label: str) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ProofWireError(f"missing {label}") from error
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or not 0 < status.st_size <= maximum:
            raise ProofWireError(f"{label} is empty, oversized, or not regular")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, _COPY_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                raise ProofWireError(f"{label} exceeds its byte bound")
            digest.update(chunk)
        if total != status.st_size:
            raise ProofWireError(f"{label} changed while it was read")
        return {"sha256": digest.hexdigest(), "bytes": total}
    finally:
        os.close(descriptor)


def _marker_directory(path: Path, label: str) -> None:
    try:
        status = path.lstat()
    except OSError as error:
        raise ProofWireError(f"missing {label}") from error
    if not stat.S_ISDIR(status.st_mode) or path.is_symlink():
        raise ProofWireError(f"{label} is not a real directory")


class PalrupGlobalChecker:
    """Run and attest the official PalRUP three-stage global checker."""

    def __init__(
        self,
        local_checker: str | os.PathLike[str],
        redistribute: str | os.PathLike[str],
        confirm: str | os.PathLike[str],
        *,
        local_checker_sha256: str,
        redistribute_sha256: str,
        confirm_sha256: str,
        source_commit: str = PALRUP_CHECKER_COMMIT,
        timeout_ms: int = 300_000,
        max_parallel: int = 16,
        read_buffer_kib: int = 1024,
        write_buffer_kib: int = 1024,
        merge_buffer_kib: int = 1024,
        queue_kib: int = 16 * 1024,
    ) -> None:
        paths = {
            "palrup_local_check": (local_checker, local_checker_sha256),
            "palrup_redistribute": (redistribute, redistribute_sha256),
            "palrup_confirm": (confirm, confirm_sha256),
        }
        tools: dict[str, _Tool] = {}
        for name, (path, expected_digest) in paths.items():
            resolved, identity, content = _executable_identity(path)
            if identity["bytes"] > MAX_EXECUTABLE_BYTES:
                raise ProofWireError(f"{name} exceeds its executable byte bound")
            if identity["sha256"] != _hex_digest(expected_digest, name):
                raise ProofWireError(f"{name} content identity differs from policy")
            tools[name] = _Tool(name, resolved, identity, content)
        self._tools = tools
        self.source_commit = _git_commit(source_commit, "PalRUP source commit")
        self.timeout_ms = _integer(
            timeout_ms, "PalRUP task timeout", 1, MAX_PALRUP_TIMEOUT_MS
        )
        self.max_parallel = _integer(
            max_parallel, "PalRUP parallel task bound", 1, MAX_PALRUP_PARALLEL_TASKS
        )
        self.read_buffer_kib = _integer(
            read_buffer_kib, "PalRUP read buffer KiB", 1, 1 << 20
        )
        self.write_buffer_kib = _integer(
            write_buffer_kib, "PalRUP write buffer KiB", 1, 1 << 20
        )
        self.merge_buffer_kib = _integer(
            merge_buffer_kib, "PalRUP merge buffer KiB", 1, 1 << 20
        )
        self.queue_kib = _integer(
            queue_kib, "PalRUP queue KiB", 1, 1 << 22
        )
        policy: dict[str, Any] = {
            "schema": PALRUP_GLOBAL_POLICY_SCHEMA,
            "protocol": PALRUP_GLOBAL_PROTOCOL,
            "source_commit": self.source_commit,
            "tools": {
                name: {
                    "sha256": tool.identity["sha256"],
                    "bytes": tool.identity["bytes"],
                }
                for name, tool in sorted(tools.items())
            },
            "redist_strategy": 3,
            "binary_fragments": True,
            "read_buffer_kib": self.read_buffer_kib,
            "write_buffer_kib": self.write_buffer_kib,
            "merge_buffer_kib": self.merge_buffer_kib,
            "queue_kib": self.queue_kib,
        }
        self.policy = policy
        self.policy_sha256 = _digest(_canonical_json(policy))

    def _verify_tool_identities(self) -> None:
        for tool in self._tools.values():
            resolved, identity, _content = _executable_identity(tool.path)
            if resolved != tool.path or identity != tool.identity:
                raise ProofWireError(f"{tool.name} identity changed after initialization")

    def _stage_tools(self, root: Path) -> dict[str, Path]:
        tool_root = root / "tools"
        tool_root.mkdir(mode=0o700)
        return {
            name: _write_executable_snapshot(tool_root, name, tool.content)
            for name, tool in self._tools.items()
        }

    def _run_phase(
        self,
        phase: str,
        commands: Sequence[tuple[int, Sequence[str]]],
    ) -> list[dict[str, Any]]:
        def run(item: tuple[int, Sequence[str]]) -> tuple[int, Any]:
            rank, command = item
            return rank, _run_bounded(command, timeout_ms=self.timeout_ms)

        results: dict[int, dict[str, Any]] = {}
        workers = min(self.max_parallel, len(commands))
        if workers < 1:
            raise ProofWireError(f"PalRUP {phase} has no tasks")
        executor = ThreadPoolExecutor(max_workers=workers)
        futures: dict[Any, int] = {}
        try:
            futures = {executor.submit(run, item): item[0] for item in commands}
            for future in as_completed(futures):
                rank = futures[future]
                try:
                    observed_rank, result = future.result()
                except Exception as error:
                    raise ProofWireError(
                        f"PalRUP {phase} task {rank} did not complete"
                    ) from error
                if observed_rank != rank:
                    raise ProofWireError(f"PalRUP {phase} task identity changed")
                if result.returncode != 0 or result.stderr:
                    diagnostic = (
                        f"exit={result.returncode}, "
                        f"stderr={'nonempty' if result.stderr else 'empty'}"
                    )
                    raise ProofWireError(
                        f"PalRUP {phase} task {rank} failed ({diagnostic})"
                    )
                results[rank] = {
                    "rank": rank,
                    "stdout_sha256": _digest(result.stdout),
                    "stderr_sha256": _digest(result.stderr),
                    "elapsed_us": result.elapsed_us,
                }
        except Exception:
            for future in futures:
                future.cancel()
            executor.shutdown(wait=True, cancel_futures=True)
            raise
        else:
            executor.shutdown(wait=True)
        ordered = [results[rank] for rank, _command in commands]
        if [entry["rank"] for entry in ordered] != [
            rank for rank, _command in commands
        ]:
            raise ProofWireError(f"PalRUP {phase} task conservation failed")
        return ordered

    @staticmethod
    def _layout(root: Path, width: int, ranks: int) -> None:
        for rank in range(ranks):
            (root / str(rank // width) / str(rank)).mkdir(
                mode=0o700, parents=True, exist_ok=False
            )

    def verify(
        self,
        formula_path: str | os.PathLike[str],
        proof_root: str | os.PathLike[str],
        num_solvers: int,
        *,
        max_formula_bytes: int = MAX_PALRUP_FORMULA_BYTES,
        max_fragment_bytes: int = MAX_PALRUP_FRAGMENT_BYTES,
        max_total_fragment_bytes: int = MAX_PALRUP_TOTAL_FRAGMENT_BYTES,
        max_stage_artifact_bytes: int = MAX_PALRUP_STAGE_ARTIFACT_BYTES,
    ) -> dict[str, Any]:
        """Check a complete proof and return a global UNSAT receipt."""

        self._verify_tool_identities()
        solvers = _integer(num_solvers, "PalRUP solver count", 1, MAX_PALRUP_SOLVERS)
        formula_limit = _integer(
            max_formula_bytes,
            "PalRUP formula byte bound",
            1,
            MAX_PALRUP_FORMULA_BYTES,
        )
        fragment_limit = _integer(
            max_fragment_bytes,
            "PalRUP fragment byte bound",
            1,
            MAX_PALRUP_FRAGMENT_BYTES,
        )
        total_limit = _integer(
            max_total_fragment_bytes,
            "PalRUP total fragment byte bound",
            1,
            MAX_PALRUP_TOTAL_FRAGMENT_BYTES,
        )
        artifact_limit = _integer(
            max_stage_artifact_bytes,
            "PalRUP stage artifact byte bound",
            1,
            MAX_PALRUP_STAGE_ARTIFACT_BYTES,
        )
        width = math.isqrt(solvers)
        if width * width < solvers:
            width += 1
        matrix_tasks = width * width
        if matrix_tasks > MAX_PALRUP_MATRIX_TASKS:
            raise ProofWireError("PalRUP redistribution matrix exceeds its task bound")

        with tempfile.TemporaryDirectory(prefix="symcc-palrup-global-") as directory:
            root = Path(directory)
            formula_root = root / "formula"
            proof_snapshot = root / "proof"
            working = root / "working"
            formula_root.mkdir(mode=0o700)
            proof_snapshot.mkdir(mode=0o700)
            working.mkdir(mode=0o700)
            tools = self._stage_tools(root)
            formula_target = formula_root / "input.cnf"
            formula = _snapshot_path(
                Path(formula_path),
                formula_target,
                maximum=formula_limit,
                label="PalRUP formula",
            )
            if formula.size == 0:
                raise ProofWireError("PalRUP formula is empty")

            proof_flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                proof_descriptor = os.open(proof_root, proof_flags)
            except OSError as error:
                raise ProofWireError("cannot open PalRUP proof root") from error
            fragments: list[dict[str, Any]] = []
            total_fragment_bytes = 0
            try:
                self._layout(proof_snapshot, width, solvers)
                for rank in range(solvers):
                    target = (
                        proof_snapshot
                        / str(rank // width)
                        / str(rank)
                        / "out.palrup"
                    )
                    snapshot = _snapshot_fragment(
                        proof_descriptor,
                        width,
                        rank,
                        target,
                        maximum=fragment_limit,
                    )
                    total_fragment_bytes += snapshot.size
                    if total_fragment_bytes > total_limit:
                        raise ProofWireError(
                            "PalRUP fragments exceed their aggregate byte bound"
                        )
                    fragments.append({"rank": rank, **snapshot.metadata()})
            finally:
                os.close(proof_descriptor)
            self._layout(working, width, matrix_tasks)

            bundle: dict[str, Any] = {
                "formula": formula.metadata(),
                "fragments": fragments,
                "num_solvers": solvers,
                "matrix_width": width,
                "matrix_tasks": matrix_tasks,
                "total_fragment_bytes": total_fragment_bytes,
            }
            bundle["bundle_sha256"] = _digest(_canonical_json(bundle))

            local_commands: list[tuple[int, Sequence[str]]] = []
            for rank in range(solvers):
                local_commands.append(
                    (
                        rank,
                        (
                            str(tools["palrup_local_check"]),
                            f"-formula-path={formula_target}",
                            f"-palrup-path={proof_snapshot}",
                            f"-working-path={working}",
                            f"-num-solvers={solvers}",
                            f"-pal-id={rank}",
                            f"-read-buffer-KB={self.read_buffer_kib}",
                            "-redist-strat=3",
                            f"-write-buffer-KB={self.write_buffer_kib}",
                            f"-merge-buffer-KB={self.merge_buffer_kib}",
                            f"-q-size-KB={self.queue_kib}",
                            "-q-alpha=0.5",
                            "-palrup-binary=1",
                        ),
                    )
                )
            local_results = self._run_phase("local-check", local_commands)
            fragment_hashes: list[dict[str, Any]] = []
            proxy_artifacts: list[dict[str, Any]] = []
            total_stage_artifact_bytes = 0
            for rank in range(solvers):
                fragment_hash = _artifact(
                    proof_snapshot
                    / str(rank // width)
                    / str(rank)
                    / "out.palrup.hash",
                    maximum=64,
                    label=f"PalRUP fragment hash {rank}",
                )
                if fragment_hash["bytes"] != 16:
                    raise ProofWireError(
                        f"PalRUP fragment hash {rank} has invalid byte size"
                    )
                fragment_hashes.append(
                    {"rank": rank, **fragment_hash}
                )
                proxy = _artifact(
                    working
                    / str(rank // width)
                    / str(rank)
                    / "out.palrup_proxy",
                    maximum=artifact_limit,
                    label=f"PalRUP proxy {rank}",
                )
                proxy_artifacts.append(
                    {"rank": rank, **proxy}
                )
                total_stage_artifact_bytes += fragment_hash["bytes"] + proxy["bytes"]
                if total_stage_artifact_bytes > artifact_limit:
                    raise ProofWireError(
                        "PalRUP stage artifacts exceed their aggregate byte bound"
                    )

            redist_commands = [
                (
                    rank,
                    (
                        str(tools["palrup_redistribute"]),
                        f"-working-path={working}",
                        f"-num-solvers={solvers}",
                        f"-pal-id={rank}",
                        f"-read-buffer-KB={self.read_buffer_kib}",
                        f"-write-buffer-KB={self.write_buffer_kib}",
                        "-redist-strat=3",
                    ),
                )
                for rank in range(matrix_tasks)
            ]
            redist_results = self._run_phase("redistribute", redist_commands)
            import_artifacts: list[dict[str, Any]] = []
            for rank in range(matrix_tasks):
                redistributed = _artifact(
                    working
                    / str(rank // width)
                    / str(rank)
                    / "out.palrup_import",
                    maximum=artifact_limit,
                    label=f"PalRUP redistributed import {rank}",
                )
                import_artifacts.append(
                    {"rank": rank, **redistributed}
                )
                total_stage_artifact_bytes += redistributed["bytes"]
                if total_stage_artifact_bytes > artifact_limit:
                    raise ProofWireError(
                        "PalRUP stage artifacts exceed their aggregate byte bound"
                    )

            if list(working.rglob(".check_ok")):
                raise ProofWireError("PalRUP confirmation marker appeared too early")
            confirm_commands = [
                (
                    rank,
                    (
                        str(tools["palrup_confirm"]),
                        f"-palrup-path={proof_snapshot}",
                        f"-working-path={working}",
                        f"-num-solvers={solvers}",
                        f"-pal-id={rank}",
                        f"-read-buffer-KB={self.read_buffer_kib}",
                        "-redist-strat=3",
                        "-palrup_binary=1",
                    ),
                )
                for rank in range(solvers)
            ]
            confirm_results = self._run_phase("confirm", confirm_commands)

            expected_markers: set[Path] = set()
            for rank in range(solvers):
                marker = (
                    working / str(rank // width) / str(rank) / ".check_ok"
                )
                _marker_directory(marker, f"PalRUP confirmation marker {rank}")
                expected_markers.add(marker)
            actual_markers = set(working.rglob(".check_ok"))
            if actual_markers != expected_markers:
                raise ProofWireError("PalRUP confirmation marker set is not exact")

            unsat_root = working / ".unsat_found"
            _marker_directory(unsat_root, "PalRUP UNSAT marker")
            witness_ranks: list[int] = []
            for child in sorted(unsat_root.iterdir(), key=lambda path: path.name):
                _marker_directory(child, "PalRUP UNSAT witness marker")
                try:
                    witness = int(child.name, 10)
                except ValueError as error:
                    raise ProofWireError("PalRUP UNSAT witness rank is invalid") from error
                if not 0 <= witness < solvers or str(witness) != child.name:
                    raise ProofWireError("PalRUP UNSAT witness rank is out of range")
                witness_ranks.append(witness)
            if not witness_ranks or len(witness_ranks) != len(set(witness_ranks)):
                raise ProofWireError("PalRUP UNSAT witness set is empty or duplicated")
            witness_ranks.sort()

            workspace: dict[str, Any] = {
                "fragment_hashes": fragment_hashes,
                "proxies": proxy_artifacts,
                "redistributed_imports": import_artifacts,
                "total_stage_artifact_bytes": total_stage_artifact_bytes,
            }
            workspace["workspace_sha256"] = _digest(_canonical_json(workspace))
            phase_results: dict[str, Any] = {
                "local_check": local_results,
                "redistribute": redist_results,
                "confirm": confirm_results,
            }
            phase_results["phase_result_sha256"] = _digest(
                _canonical_json(phase_results)
            )
            receipt: dict[str, Any] = {
                "schema": PALRUP_GLOBAL_RECEIPT_SCHEMA,
                "protocol": PALRUP_GLOBAL_PROTOCOL,
                "status": "global-unsat-confirmed",
                "scope": "official-palrup-local-redistribute-confirm",
                "source_commit": self.source_commit,
                "policy_sha256": self.policy_sha256,
                "tool_sha256": {
                    name: tool.identity["sha256"]
                    for name, tool in sorted(self._tools.items())
                },
                "bundle": bundle,
                "workspace": workspace,
                "phases": phase_results,
                "confirmed_ranks": list(range(solvers)),
                "unsat_witness_ranks": witness_ranks,
            }
            receipt["receipt_sha256"] = _digest(_canonical_json(receipt))
            return receipt

    def validate_receipt(
        self,
        receipt: Mapping[str, Any],
        *,
        formula_path: str | os.PathLike[str] | None = None,
        proof_root: str | os.PathLike[str] | None = None,
        recheck: bool = True,
    ) -> dict[str, Any]:
        if type(recheck) is not bool:
            raise ProofWireError("PalRUP recheck flag must be boolean")
        if not isinstance(receipt, Mapping):
            raise ProofWireError("PalRUP global receipt must be an object")
        normalized = dict(receipt)
        digest = _hex_digest(
            normalized.pop("receipt_sha256", None), "PalRUP global receipt"
        )
        if _digest(_canonical_json(normalized)) != digest:
            raise ProofWireError("PalRUP global receipt identity changed")
        if set(normalized) != {
            "schema",
            "protocol",
            "status",
            "scope",
            "source_commit",
            "policy_sha256",
            "tool_sha256",
            "bundle",
            "workspace",
            "phases",
            "confirmed_ranks",
            "unsat_witness_ranks",
        }:
            raise ProofWireError("PalRUP global receipt fields differ from the protocol")
        if (
            normalized.get("schema") != PALRUP_GLOBAL_RECEIPT_SCHEMA
            or normalized.get("protocol") != PALRUP_GLOBAL_PROTOCOL
            or normalized.get("status") != "global-unsat-confirmed"
            or normalized.get("scope")
            != "official-palrup-local-redistribute-confirm"
            or normalized.get("source_commit") != self.source_commit
            or normalized.get("policy_sha256") != self.policy_sha256
            or normalized.get("tool_sha256")
            != {
                name: tool.identity["sha256"]
                for name, tool in sorted(self._tools.items())
            }
        ):
            raise ProofWireError("PalRUP global receipt policy changed")
        bundle = normalized.get("bundle")
        workspace = normalized.get("workspace")
        phases = normalized.get("phases")
        if not all(isinstance(value, Mapping) for value in (bundle, workspace, phases)):
            raise ProofWireError("PalRUP global receipt sections are invalid")
        bundle_copy = dict(bundle)
        bundle_digest = _hex_digest(
            bundle_copy.pop("bundle_sha256", None), "PalRUP bundle"
        )
        if _digest(_canonical_json(bundle_copy)) != bundle_digest:
            raise ProofWireError("PalRUP bundle identity changed")
        if set(bundle) != {
            "formula",
            "fragments",
            "num_solvers",
            "matrix_width",
            "matrix_tasks",
            "total_fragment_bytes",
            "bundle_sha256",
        }:
            raise ProofWireError("PalRUP bundle fields differ from the protocol")
        workspace_copy = dict(workspace)
        workspace_digest = _hex_digest(
            workspace_copy.pop("workspace_sha256", None), "PalRUP workspace"
        )
        if _digest(_canonical_json(workspace_copy)) != workspace_digest:
            raise ProofWireError("PalRUP workspace identity changed")
        if set(workspace) != {
            "fragment_hashes",
            "proxies",
            "redistributed_imports",
            "total_stage_artifact_bytes",
            "workspace_sha256",
        }:
            raise ProofWireError("PalRUP workspace fields differ from the protocol")
        phases_copy = dict(phases)
        phases_digest = _hex_digest(
            phases_copy.pop("phase_result_sha256", None), "PalRUP phase results"
        )
        if _digest(_canonical_json(phases_copy)) != phases_digest:
            raise ProofWireError("PalRUP phase result identity changed")
        if set(phases) != {
            "local_check",
            "redistribute",
            "confirm",
            "phase_result_sha256",
        }:
            raise ProofWireError("PalRUP phase fields differ from the protocol")
        solvers = _integer(
            bundle.get("num_solvers"), "PalRUP receipt solver count", 1, MAX_PALRUP_SOLVERS
        )
        width = _integer(
            bundle.get("matrix_width"),
            "PalRUP receipt matrix width",
            1,
            MAX_PALRUP_SOLVERS,
        )
        matrix_tasks = _integer(
            bundle.get("matrix_tasks"),
            "PalRUP receipt matrix tasks",
            1,
            MAX_PALRUP_MATRIX_TASKS,
        )
        expected_width = math.isqrt(solvers)
        if expected_width * expected_width < solvers:
            expected_width += 1
        if width != expected_width or matrix_tasks != width * width:
            raise ProofWireError("PalRUP receipt redistribution matrix changed")
        formula = bundle.get("formula")
        fragments = bundle.get("fragments")
        if not isinstance(formula, Mapping) or set(formula) != {"sha256", "bytes"}:
            raise ProofWireError("PalRUP receipt formula identity is invalid")
        _hex_digest(formula.get("sha256"), "PalRUP receipt formula")
        _integer(
            formula.get("bytes"),
            "PalRUP receipt formula bytes",
            1,
            MAX_PALRUP_FORMULA_BYTES,
        )

        def validate_artifacts(
            raw: Any,
            *,
            count: int,
            label: str,
            minimum_bytes: int,
            maximum_bytes: int,
        ) -> None:
            if not isinstance(raw, list) or len(raw) != count:
                raise ProofWireError(f"{label} count is invalid")
            for rank, entry in enumerate(raw):
                if not isinstance(entry, Mapping) or set(entry) != {
                    "rank",
                    "sha256",
                    "bytes",
                }:
                    raise ProofWireError(f"{label} entry is invalid")
                if entry.get("rank") != rank:
                    raise ProofWireError(f"{label} rank conservation failed")
                _hex_digest(entry.get("sha256"), label)
                _integer(entry.get("bytes"), label, minimum_bytes, maximum_bytes)

        validate_artifacts(
            fragments,
            count=solvers,
            label="PalRUP receipt fragment",
            minimum_bytes=0,
            maximum_bytes=MAX_PALRUP_FRAGMENT_BYTES,
        )
        total_fragment_bytes = _integer(
            bundle.get("total_fragment_bytes"),
            "PalRUP receipt total fragment bytes",
            0,
            MAX_PALRUP_TOTAL_FRAGMENT_BYTES,
        )
        if total_fragment_bytes != sum(entry["bytes"] for entry in fragments):
            raise ProofWireError("PalRUP receipt fragment byte conservation failed")
        validate_artifacts(
            workspace.get("fragment_hashes"),
            count=solvers,
            label="PalRUP receipt fragment hash",
            minimum_bytes=16,
            maximum_bytes=16,
        )
        validate_artifacts(
            workspace.get("proxies"),
            count=solvers,
            label="PalRUP receipt proxy",
            minimum_bytes=1,
            maximum_bytes=MAX_PALRUP_STAGE_ARTIFACT_BYTES,
        )
        validate_artifacts(
            workspace.get("redistributed_imports"),
            count=matrix_tasks,
            label="PalRUP receipt redistributed import",
            minimum_bytes=1,
            maximum_bytes=MAX_PALRUP_STAGE_ARTIFACT_BYTES,
        )
        total_stage_artifact_bytes = _integer(
            workspace.get("total_stage_artifact_bytes"),
            "PalRUP receipt total stage artifact bytes",
            1,
            MAX_PALRUP_STAGE_ARTIFACT_BYTES,
        )
        observed_stage_bytes = sum(
            entry["bytes"]
            for name in ("fragment_hashes", "proxies", "redistributed_imports")
            for entry in workspace[name]
        )
        if total_stage_artifact_bytes != observed_stage_bytes:
            raise ProofWireError("PalRUP receipt stage byte conservation failed")

        def validate_phase(raw: Any, count: int, label: str) -> None:
            if not isinstance(raw, list) or len(raw) != count:
                raise ProofWireError(f"PalRUP {label} receipt count is invalid")
            for rank, entry in enumerate(raw):
                if not isinstance(entry, Mapping) or set(entry) != {
                    "rank",
                    "stdout_sha256",
                    "stderr_sha256",
                    "elapsed_us",
                }:
                    raise ProofWireError(f"PalRUP {label} receipt entry is invalid")
                if entry.get("rank") != rank:
                    raise ProofWireError(f"PalRUP {label} rank conservation failed")
                _hex_digest(entry.get("stdout_sha256"), f"PalRUP {label} stdout")
                if entry.get("stderr_sha256") != _digest(b""):
                    raise ProofWireError(f"PalRUP {label} recorded diagnostics")
                _integer(
                    entry.get("elapsed_us"),
                    f"PalRUP {label} elapsed time",
                    0,
                    (1 << 63) - 1,
                )

        validate_phase(phases.get("local_check"), solvers, "local-check")
        validate_phase(phases.get("redistribute"), matrix_tasks, "redistribute")
        validate_phase(phases.get("confirm"), solvers, "confirm")
        if normalized.get("confirmed_ranks") != list(range(solvers)):
            raise ProofWireError("PalRUP receipt confirmation set is incomplete")
        witnesses = normalized.get("unsat_witness_ranks")
        if (
            not isinstance(witnesses, list)
            or not witnesses
            or any(type(rank) is not int or not 0 <= rank < solvers for rank in witnesses)
            or len(witnesses) != len(set(witnesses))
            or witnesses != sorted(witnesses)
        ):
            raise ProofWireError("PalRUP receipt UNSAT witness set is invalid")
        if recheck:
            if formula_path is None or proof_root is None:
                raise ProofWireError("PalRUP receipt recheck requires proof inputs")
            rechecked = self.verify(formula_path, proof_root, solvers)
            for key in (
                "schema",
                "protocol",
                "status",
                "scope",
                "source_commit",
                "policy_sha256",
                "tool_sha256",
                "bundle",
                "workspace",
                "confirmed_ranks",
            ):
                if rechecked.get(key) != receipt.get(key):
                    raise ProofWireError(f"PalRUP receipt recheck changed {key}")
        normalized["receipt_sha256"] = digest
        return normalized


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--formula", type=Path, required=True)
    parser.add_argument("--proof-root", type=Path, required=True)
    parser.add_argument("--num-solvers", type=int, required=True)
    parser.add_argument("--local-check", type=Path, required=True)
    parser.add_argument("--redistribute", type=Path, required=True)
    parser.add_argument("--confirm", type=Path, required=True)
    parser.add_argument("--local-check-sha256", required=True)
    parser.add_argument("--redistribute-sha256", required=True)
    parser.add_argument("--confirm-sha256", required=True)
    parser.add_argument("--timeout-ms", type=int, default=300_000)
    parser.add_argument("--max-parallel", type=int, default=16)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    checker = PalrupGlobalChecker(
        arguments.local_check,
        arguments.redistribute,
        arguments.confirm,
        local_checker_sha256=arguments.local_check_sha256,
        redistribute_sha256=arguments.redistribute_sha256,
        confirm_sha256=arguments.confirm_sha256,
        timeout_ms=arguments.timeout_ms,
        max_parallel=arguments.max_parallel,
    )
    receipt = checker.verify(
        arguments.formula, arguments.proof_root, arguments.num_solvers
    )
    encoded = json.dumps(receipt, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    if arguments.output is not None:
        output = arguments.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.name}.tmp-{os.getpid()}")
        try:
            temporary.write_text(encoded, encoding="ascii")
            os.replace(temporary, output)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
