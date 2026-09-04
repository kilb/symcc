#!/usr/bin/env python3
"""Executable Agolic BSE adapter for SymCC continuation programs.

Worker execution writes solver-derived candidates only to a plan-private
staging directory.  The coordinator later replays those bytes concretely and
is the sole writer of the cumulative corpus and replay-derived coverage map.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import resource
import stat
import time
from typing import Any, Mapping, Sequence

from agolic_planning import CoverageSnapshot, PLAN_SCHEMA
from distributed_state import LiveStateStore
from live_continuation import LiveContinuationExecutor


BSE_RESULT_SCHEMA = "symcc-agolic-continuation-bse-result-v1"
REPLAY_RESULT_SCHEMA = "symcc-agolic-continuation-replay-v1"
_MAX_JSON_BYTES = 64 * 1024 * 1024
_MAX_INPUT_BYTES = 4 * 1024 * 1024
_MAX_ARTIFACTS = 4096
_MAX_STEPS = (1 << 31) - 1
_MAX_STATES = 100_000


class AgolicBSERunnerError(ValueError):
    """Raised when a plan, artifact, or replay violates the runner contract."""


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, OverflowError) as error:
        raise AgolicBSERunnerError("value is not canonical JSON") from error


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise AgolicBSERunnerError(f"{name} is not a SHA-256 identity")
    return value


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AgolicBSERunnerError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise AgolicBSERunnerError(
            f"{name} is outside {minimum}..{maximum}"
        )
    return value


def _finite(value: Any, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise AgolicBSERunnerError(f"{name} must be finite")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise AgolicBSERunnerError(f"{name} must be finite") from error
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise AgolicBSERunnerError(
            f"{name} is outside {minimum}..{maximum}"
        )
    return result


def _exact_keys(value: Any, expected: set[str], name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise AgolicBSERunnerError(f"{name} has an invalid shape")
    return value


def _regular_file(path: Path, maximum: int, name: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise AgolicBSERunnerError("O_NOFOLLOW is required")
    try:
        descriptor = os.open(path, flags | nofollow)
    except OSError as error:
        raise AgolicBSERunnerError(f"{name} cannot be opened safely") from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size < 0
            or before.st_size > maximum
        ):
            raise AgolicBSERunnerError(f"{name} is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1 << 20, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        public = os.stat(path, follow_symlinks=False)
    except OSError as error:
        raise AgolicBSERunnerError(f"{name} disappeared while reading") from error
    def identity(item: os.stat_result) -> tuple[int, ...]:
        return (
            item.st_dev,
            item.st_ino,
            item.st_mode,
            item.st_size,
            item.st_mtime_ns,
            item.st_ctime_ns,
        )
    if (
        len(content) != before.st_size
        or identity(before) != identity(after)
        or identity(after) != identity(public)
    ):
        raise AgolicBSERunnerError(f"{name} changed while reading")
    return content


def _json_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AgolicBSERunnerError(f"duplicate JSON member {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise AgolicBSERunnerError(f"non-finite JSON number {value!r}")


def _load_json(path: Path, name: str) -> tuple[dict[str, Any], bytes]:
    content = _regular_file(path, _MAX_JSON_BYTES, name)
    try:
        value = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_json_without_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AgolicBSERunnerError(f"{name} is not valid JSON") from error
    if not isinstance(value, dict):
        raise AgolicBSERunnerError(f"{name} must be a JSON object")
    return value, content


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = _regular_file(path, len(content), "existing artifact")
        if existing != content:
            raise AgolicBSERunnerError("content-addressed artifact mismatch")
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short artifact write")
            view = view[written:]
        os.fsync(descriptor)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    finally:
        os.close(descriptor)
    try:
        os.link(temporary, path)
    except FileExistsError:
        existing = _regular_file(path, len(content), "raced artifact")
        if existing != content:
            raise AgolicBSERunnerError("content-addressed artifact race")
    finally:
        os.unlink(temporary)
    directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


class AgolicContinuationBSERunner:
    """Run admitted plans and replay candidates through continuation IR."""

    def __init__(
        self,
        program_path: str | os.PathLike[str],
        workspace: str | os.PathLike[str],
        *,
        program_sha256: str,
        max_steps: int = 100_000,
        max_states: int = 4096,
        max_artifacts: int = 256,
        memory_limit_enforced: bool = False,
        environment_isolated: bool = False,
    ) -> None:
        self.program_path = Path(program_path).resolve()
        self.workspace = Path(workspace).resolve()
        self.program_sha256 = _sha256(program_sha256, "program SHA-256")
        self.max_steps = _integer(max_steps, "maximum steps", 1, _MAX_STEPS)
        self.max_states = _integer(max_states, "maximum states", 1, _MAX_STATES)
        self.max_artifacts = _integer(
            max_artifacts, "maximum artifacts", 1, _MAX_ARTIFACTS
        )
        if type(memory_limit_enforced) is not bool:
            raise AgolicBSERunnerError("memory limit flag must be boolean")
        if type(environment_isolated) is not bool:
            raise AgolicBSERunnerError("environment isolation flag must be boolean")
        self.memory_limit_enforced = memory_limit_enforced
        self.environment_isolated = environment_isolated
        self.program, encoded = _load_json(self.program_path, "continuation program")
        if _sha256_bytes(encoded) != self.program_sha256:
            raise AgolicBSERunnerError("continuation program identity mismatch")
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.runs = self.workspace / "runs"
        self.corpus = self.workspace / "corpus"
        self.runs.mkdir(exist_ok=True)
        self.corpus.mkdir(exist_ok=True)

    def _normalize_plan(self, raw: Any) -> dict[str, Any]:
        plan = _exact_keys(raw, {
            "schema", "plan_id", "round", "target", "specification",
            "fingerprint", "rationale", "issued_at",
        }, "Agolic plan")
        if plan.get("schema") != PLAN_SCHEMA:
            raise AgolicBSERunnerError("Agolic plan schema is unsupported")
        plan_id = _sha256(plan.get("plan_id"), "plan ID")
        fingerprint = _sha256(plan.get("fingerprint"), "plan fingerprint")
        round_number = _integer(plan.get("round"), "plan round", 1, (1 << 63) - 1)
        specification = _exact_keys(plan.get("specification"), {
            "target_id", "target_branch", "source_file", "function", "line",
            "mode", "profile", "time_limit_seconds", "memory_limit_mib",
            "witness", "environment", "symbolic_inputs",
        }, "Agolic plan specification")
        if _digest(specification) != fingerprint:
            raise AgolicBSERunnerError("Agolic plan fingerprint mismatch")
        if _digest({"round": round_number, "fingerprint": fingerprint}) != plan_id:
            raise AgolicBSERunnerError("Agolic plan ID mismatch")
        target = _exact_keys(plan.get("target"), {
            "target_id", "target_branch", "source_file", "function", "line",
            "distance", "opportunity", "modes", "witnesses",
        }, "Agolic target")
        for field in ("target_id", "target_branch", "source_file", "function", "line"):
            if target.get(field) != specification.get(field):
                raise AgolicBSERunnerError("Agolic target and specification disagree")
        mode = specification.get("mode")
        if mode not in {"harness-entry", "witness-guided"}:
            raise AgolicBSERunnerError("Agolic BSE mode is unsupported")
        modes = target.get("modes")
        if (
            not isinstance(modes, list)
            or mode not in modes
            or any(item not in {"harness-entry", "witness-guided"} for item in modes)
        ):
            raise AgolicBSERunnerError("target does not admit the BSE mode")
        _finite(
            specification.get("time_limit_seconds"),
            "BSE time limit", 0.1, 86_400.0,
        )
        _integer(
            specification.get("memory_limit_mib"),
            "BSE memory limit", 1, 1_048_576,
        )
        target_branch = _integer(
            specification.get("target_branch"),
            "target branch", 1, (1 << 64) - 1,
        )
        function = specification.get("function")
        functions = self.program.get("functions")
        if not isinstance(functions, Mapping) or function not in functions:
            raise AgolicBSERunnerError("target function is not in the program")
        site_records = [
            (
                str(function_name),
                int(instruction.get("site", 0)),
                str(instruction.get("op", "")),
            )
            for function_name, body in functions.items()
            if isinstance(body, Mapping)
            for blocks in [body.get("blocks", {})]
            if isinstance(blocks, Mapping)
            for block in blocks.values()
            if isinstance(block, Sequence) and not isinstance(block, (str, bytes))
            for instruction in block
            if isinstance(instruction, Mapping)
        ]
        target_sites = [
            (owner, op) for owner, site, op in site_records
            if site == target_branch
        ]
        if len(target_sites) != 1:
            raise AgolicBSERunnerError("target branch is not a unique program site")
        if target_sites[0][0] != function or target_sites[0][1] not in {
            "branch", "throw_if",
        }:
            raise AgolicBSERunnerError(
                "target branch is not a decision in the target function"
            )
        witness = specification.get("witness")
        if (mode == "witness-guided") != (witness is not None):
            raise AgolicBSERunnerError("witness does not match the BSE mode")
        if witness is not None:
            witness = _exact_keys(witness, {
                "sha256", "path", "release_function", "release_branch",
                "provenance",
            }, "Agolic witness")
            witness_digest = _sha256(witness.get("sha256"), "witness SHA-256")
            witness_path = witness.get("path")
            if not isinstance(witness_path, str) or not witness_path:
                raise AgolicBSERunnerError("witness path is invalid")
            witness_bytes = _regular_file(
                Path(witness_path), _MAX_INPUT_BYTES, "Agolic witness"
            )
            if _sha256_bytes(witness_bytes) != witness_digest:
                raise AgolicBSERunnerError("Agolic witness identity mismatch")
            release_function = witness.get("release_function")
            release_branch = witness.get("release_branch")
            if not isinstance(release_function, str):
                raise AgolicBSERunnerError("witness release function is invalid")
            release_branch = _integer(
                release_branch, "witness release branch", 0, (1 << 64) - 1
            )
            if bool(release_function) == bool(release_branch):
                raise AgolicBSERunnerError(
                    "witness requires exactly one release boundary"
                )
            if release_function and release_function not in functions:
                raise AgolicBSERunnerError(
                    "witness release function is not in the program"
                )
            release_ops = [
                op for _owner, site, op in site_records if site == release_branch
            ]
            if release_branch and (
                len(release_ops) != 1
                or release_ops[0] not in {"branch", "throw_if"}
            ):
                raise AgolicBSERunnerError(
                    "witness release branch is not a unique decision site"
                )
            target_witnesses = target.get("witnesses")
            if (
                not isinstance(target_witnesses, list)
                or witness not in target_witnesses
            ):
                raise AgolicBSERunnerError(
                    "witness is not in the reviewed target frontier"
                )
        environment = specification.get("environment")
        if not isinstance(environment, Mapping) or len(environment) > 256:
            raise AgolicBSERunnerError("BSE environment is invalid")
        for raw_key, raw_value in environment.items():
            if (
                not isinstance(raw_key, str)
                or not raw_key.startswith("SYMCC_")
                or not isinstance(raw_value, str)
                or "\x00" in raw_key
                or "\x00" in raw_value
                or len(raw_key) > 256
                or len(raw_value.encode("utf-8")) > 4096
            ):
                raise AgolicBSERunnerError("BSE environment entry is invalid")
        if environment and not self.environment_isolated:
            raise AgolicBSERunnerError(
                "non-empty BSE environment requires an isolated runner process"
            )
        symbolic_inputs = specification.get("symbolic_inputs")
        if not isinstance(symbolic_inputs, Mapping):
            raise AgolicBSERunnerError("symbolic input surface is invalid")
        if set(symbolic_inputs) - {"seed_hex"}:
            raise AgolicBSERunnerError("symbolic input surface is unsupported")
        raw_seed = symbolic_inputs.get("seed_hex", "")
        if not isinstance(raw_seed, str) or len(raw_seed) > 2 * _MAX_INPUT_BYTES:
            raise AgolicBSERunnerError("seed_hex is invalid")
        try:
            seed = bytes.fromhex(raw_seed)
        except ValueError as error:
            raise AgolicBSERunnerError("seed_hex is invalid") from error
        if seed.hex() != raw_seed:
            raise AgolicBSERunnerError("seed_hex is not canonical lowercase hex")
        return json.loads(_canonical(plan).decode("ascii"))

    def preflight(self, raw_plan: Mapping[str, Any]) -> tuple[bool, str]:
        try:
            self._normalize_plan(raw_plan)
            return True, "ok"
        except (AgolicBSERunnerError, OSError, ValueError) as error:
            return False, str(error)

    def _seed(self, plan: Mapping[str, Any]) -> bytes:
        specification = plan["specification"]
        witness = specification["witness"]
        if witness is not None:
            content = _regular_file(
                Path(witness["path"]), _MAX_INPUT_BYTES, "Agolic witness"
            )
            if _sha256_bytes(content) != witness["sha256"]:
                raise AgolicBSERunnerError("Agolic witness identity mismatch")
            return content
        symbolic_inputs = specification["symbolic_inputs"]
        if not isinstance(symbolic_inputs, Mapping):
            raise AgolicBSERunnerError("symbolic input surface is invalid")
        unknown = set(symbolic_inputs) - {"seed_hex"}
        if unknown:
            raise AgolicBSERunnerError(
                f"unsupported symbolic input fields: {sorted(unknown)}"
            )
        raw_seed = symbolic_inputs.get("seed_hex", "")
        if not isinstance(raw_seed, str) or len(raw_seed) > 2 * _MAX_INPUT_BYTES:
            raise AgolicBSERunnerError("seed_hex is invalid")
        try:
            seed = bytes.fromhex(raw_seed)
        except ValueError as error:
            raise AgolicBSERunnerError("seed_hex is invalid") from error
        if seed.hex() != raw_seed:
            raise AgolicBSERunnerError("seed_hex is not canonical lowercase hex")
        return seed

    def execute_plan(self, raw_plan: Mapping[str, Any]) -> dict[str, Any]:
        plan = self._normalize_plan(raw_plan)
        plan_id = plan["plan_id"]
        specification = plan["specification"]
        run_root = self.runs / plan_id
        candidates_root = run_root / "candidates"
        candidates_root.mkdir(parents=True, exist_ok=True)
        seed = self._seed(plan)
        witness = specification["witness"]
        release_function = witness["release_function"] if witness else ""
        release_branch = witness["release_branch"] if witness else 0
        started = time.monotonic()
        cpu_started = time.process_time()
        store = LiveStateStore(run_root / "store")
        with LiveContinuationExecutor(store) as executor:
            root = executor.create(
                self.program,
                input_bytes=seed,
                target_branch=specification["target_branch"],
            )
            execution = executor.resume(
                root,
                max_steps=self.max_steps,
                max_states=self.max_states,
                witness_release_function=release_function,
                witness_release_branch=release_branch,
                max_seconds=specification["time_limit_seconds"],
            )
            release_reached = execution["witness_guidance"]["release_reached"]
            allow_artifacts = (
                specification["mode"] == "harness-entry" or release_reached
            )
            candidates: list[dict[str, Any]] = []
            failures: list[str] = []
            seen: set[str] = set()
            terminal: list[Mapping[str, Any]] = []
            solver_time_seconds = float(execution["solver_time_seconds"])
            if allow_artifacts:
                terminal = [
                    item for item in execution["halted"]
                    if item.get("status") in {
                        "returned", "halted", "unhandled-exception",
                    }
                ]
                for item in terminal[:self.max_artifacts]:
                    solve_started = time.monotonic()
                    try:
                        content = executor.materialize_input(
                            item["checkpoint"], fallback_input=seed
                        )
                    except (OSError, ValueError) as error:
                        failures.append(type(error).__name__)
                        continue
                    finally:
                        solver_time_seconds += time.monotonic() - solve_started
                    digest = _sha256_bytes(content)
                    if digest in seen:
                        continue
                    seen.add(digest)
                    path = candidates_root / digest
                    _atomic_bytes(path, content)
                    candidates.append({
                        "sha256": digest,
                        "size": len(content),
                        "checkpoint": item["checkpoint"],
                        "relative_path": f"candidates/{digest}",
                    })
        elapsed = time.monotonic() - started
        cpu = time.process_time() - cpu_started
        if execution["bounded"]:
            status = "timeout"
        elif allow_artifacts and terminal and not candidates and failures:
            status = "error"
        else:
            status = "complete"
        return {
            "schema": BSE_RESULT_SCHEMA,
            "plan_id": plan_id,
            "program_sha256": self.program_sha256,
            "mode": specification["mode"],
            "status": status,
            "release_reached": release_reached,
            "candidates": candidates,
            "materialization_failures": failures,
            "execution": execution,
            "elapsed_seconds": elapsed,
            "cpu_seconds": cpu,
            "solver_time_seconds": solver_time_seconds,
            "resource_limits": {
                "wall_time_enforced": True,
                "memory_limit_enforced": self.memory_limit_enforced,
                "time_limit_seconds": specification["time_limit_seconds"],
                "memory_limit_mib": specification["memory_limit_mib"],
                "max_steps": self.max_steps,
                "max_states": self.max_states,
                "max_artifacts": self.max_artifacts,
            },
        }

    def _validate_run_result(
        self,
        plan: Mapping[str, Any],
        raw: Any,
    ) -> Mapping[str, Any]:
        result = _exact_keys(raw, {
            "schema", "plan_id", "program_sha256", "mode", "status",
            "release_reached", "candidates", "materialization_failures",
            "execution", "elapsed_seconds", "cpu_seconds",
            "solver_time_seconds", "resource_limits",
        }, "Agolic BSE result")
        if (
            result.get("schema") != BSE_RESULT_SCHEMA
            or result.get("plan_id") != plan["plan_id"]
            or result.get("program_sha256") != self.program_sha256
            or result.get("mode") != plan["specification"]["mode"]
            or result.get("status") not in {"complete", "timeout", "error"}
            or type(result.get("release_reached")) is not bool
        ):
            raise AgolicBSERunnerError("Agolic BSE result identity is invalid")
        _finite(
            result.get("solver_time_seconds"),
            "BSE solver time", 0.0, 86_400.0,
        )
        _finite(result.get("elapsed_seconds"), "BSE elapsed time", 0.0, 172_800.0)
        _finite(result.get("cpu_seconds"), "BSE CPU time", 0.0, 172_800.0)
        execution = result.get("execution")
        specification = plan["specification"]
        witness = specification["witness"]
        expected_release_function = (
            witness["release_function"] if witness is not None else ""
        )
        expected_release_branch = (
            witness["release_branch"] if witness is not None else 0
        )
        if (
            not isinstance(execution, Mapping)
            or execution.get("schema") != "symcc-live-execution-result-v1"
            or not isinstance(execution.get("witness_guidance"), Mapping)
            or execution["witness_guidance"].get("release_reached")
            is not result.get("release_reached")
        ):
            raise AgolicBSERunnerError("BSE execution evidence is inconsistent")
        guidance = execution["witness_guidance"]
        if (
            guidance.get("enabled") is not (witness is not None)
            or guidance.get("concrete_replay") is not False
            or guidance.get("release_function") != expected_release_function
            or guidance.get("release_branch") != expected_release_branch
            or type(execution.get("bounded")) is not bool
            or type(execution.get("timed_out")) is not bool
        ):
            raise AgolicBSERunnerError("BSE witness-guidance evidence is invalid")
        execution_solver_time = _finite(
            execution.get("solver_time_seconds"),
            "execution solver time", 0.0, 86_400.0,
        )
        if float(result["solver_time_seconds"]) < execution_solver_time:
            raise AgolicBSERunnerError("BSE solver-time evidence is inconsistent")
        failures = result.get("materialization_failures")
        if (
            not isinstance(failures, list)
            or len(failures) > self.max_artifacts
            or any(
                not isinstance(item, str) or not item or len(item) > 128
                for item in failures
            )
        ):
            raise AgolicBSERunnerError(
                "BSE materialization failures are invalid"
            )
        limits = _exact_keys(result.get("resource_limits"), {
            "wall_time_enforced", "memory_limit_enforced", "max_steps",
            "max_states", "max_artifacts", "time_limit_seconds",
            "memory_limit_mib",
        }, "BSE resource limits")
        if (
            limits.get("wall_time_enforced") is not True
            or type(limits.get("memory_limit_enforced")) is not bool
            or limits.get("time_limit_seconds")
            != specification["time_limit_seconds"]
            or limits.get("memory_limit_mib")
            != specification["memory_limit_mib"]
            or limits.get("max_steps") != self.max_steps
            or limits.get("max_states") != self.max_states
            or limits.get("max_artifacts") != self.max_artifacts
        ):
            raise AgolicBSERunnerError("BSE resource-limit evidence is invalid")
        candidates = result.get("candidates")
        if (
            not isinstance(candidates, list)
            or len(candidates) > self.max_artifacts
        ):
            raise AgolicBSERunnerError("Agolic BSE candidates are invalid")
        halted = execution.get("halted")
        if not isinstance(halted, list):
            raise AgolicBSERunnerError("BSE terminal evidence is invalid")
        terminal_checkpoints: set[str] = set()
        for item in halted:
            if not isinstance(item, Mapping):
                raise AgolicBSERunnerError("BSE terminal evidence is invalid")
            if item.get("status") in {
                "returned", "halted", "unhandled-exception",
            }:
                terminal_checkpoints.add(
                    _sha256(item.get("checkpoint"), "terminal checkpoint")
                )
        seen: set[str] = set()
        for candidate in candidates:
            candidate = _exact_keys(candidate, {
                "sha256", "size", "checkpoint", "relative_path",
            }, "Agolic BSE candidate")
            digest = _sha256(candidate.get("sha256"), "candidate SHA-256")
            if digest in seen:
                raise AgolicBSERunnerError("duplicate Agolic BSE candidate")
            seen.add(digest)
            _integer(candidate.get("size"), "candidate size", 0, _MAX_INPUT_BYTES)
            checkpoint = _sha256(
                candidate.get("checkpoint"), "candidate checkpoint"
            )
            if checkpoint not in terminal_checkpoints:
                raise AgolicBSERunnerError(
                    "candidate is not bound to a terminal checkpoint"
                )
            if candidate.get("relative_path") != f"candidates/{digest}":
                raise AgolicBSERunnerError("candidate path is not canonical")
        if (
            result.get("mode") == "witness-guided"
            and not result.get("release_reached")
            and candidates
        ):
            raise AgolicBSERunnerError(
                "pre-release execution cannot emit candidates"
            )
        allow_artifacts = (
            result.get("mode") == "harness-entry"
            or result.get("release_reached") is True
        )
        if execution["bounded"]:
            expected_status = "timeout"
        elif allow_artifacts and terminal_checkpoints and not candidates and failures:
            expected_status = "error"
        else:
            expected_status = "complete"
        if result.get("status") != expected_status:
            raise AgolicBSERunnerError("BSE result status is inconsistent")
        return result

    def _replay_bytes(self, content: bytes, identity: str) -> dict[str, Any]:
        replay_root = self.workspace / "replay" / identity
        store = LiveStateStore(replay_root)
        with LiveContinuationExecutor(store) as executor:
            root = executor.create(self.program, input_bytes=content)
            replay = executor.resume(
                root,
                max_steps=self.max_steps,
                max_states=1,
                concrete_replay=True,
                max_seconds=60.0,
            )
        if (
            replay["forks"] != 0
            or replay["feasibility_checks"] != 0
            or replay["witness_guidance"]["pre_release_solver_queries"] != 0
            or replay["witness_guidance"]["pre_release_forks"] != 0
            or replay["bounded"]
            or not any(
                item.get("status") in {
                    "returned", "halted", "unhandled-exception",
                }
                for item in replay["halted"]
            )
        ):
            raise AgolicBSERunnerError("candidate concrete replay is incomplete")
        return replay

    def _corpus_paths(self) -> list[Path]:
        paths: list[Path] = []
        for entry in sorted(self.corpus.iterdir(), key=lambda item: item.name):
            _sha256(entry.name, "corpus artifact name")
            metadata = entry.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise AgolicBSERunnerError("corpus contains a non-regular artifact")
            paths.append(entry)
        if len(paths) > _MAX_ARTIFACTS:
            raise AgolicBSERunnerError("corpus artifact limit is exceeded")
        return paths

    def replay_coverage(self) -> dict[str, Any]:
        elements: set[str] = set()
        branches: set[int] = set()
        functions: set[str] = set()
        artifacts: list[str] = []
        for path in self._corpus_paths():
            content = _regular_file(path, _MAX_INPUT_BYTES, "corpus artifact")
            if _sha256_bytes(content) != path.name:
                raise AgolicBSERunnerError("corpus artifact identity mismatch")
            replay = self._replay_bytes(content, path.name)
            artifacts.append(path.name)
            functions.update(replay["entered_functions"])
            for outcome in replay["executed_branch_outcomes"]:
                site = int(outcome["site"])
                taken = bool(outcome["taken"])
                branches.add(site)
                elements.add(f"branch:{site}:{'T' if taken else 'F'}")
        snapshot = {
            "elements": sorted(elements),
            "branches": sorted(branches),
            "functions": sorted(functions),
            "corpus_artifacts": artifacts,
        }
        snapshot["replay_identity"] = _digest(snapshot)
        return snapshot

    def replay_result(
        self,
        raw_plan: Mapping[str, Any],
        raw_result: Mapping[str, Any],
        _prior_coverage: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        plan = self._normalize_plan(raw_plan)
        result = self._validate_run_result(plan, raw_result)
        if _prior_coverage is not None:
            try:
                expected_prior = CoverageSnapshot.from_mapping(
                    _prior_coverage
                ).as_dict()
            except ValueError as error:
                raise AgolicBSERunnerError(
                    "prior replay coverage is invalid"
                ) from error
            if self.replay_coverage() != expected_prior:
                raise AgolicBSERunnerError(
                    "prior replay coverage does not match the current corpus"
                )
        run_root = self.runs / plan["plan_id"]
        generated: list[str] = []
        replay_started = time.monotonic()
        for candidate in result["candidates"]:
            digest = candidate["sha256"]
            path = run_root / candidate["relative_path"]
            content = _regular_file(path, _MAX_INPUT_BYTES, "staged candidate")
            if len(content) != candidate["size"] or _sha256_bytes(content) != digest:
                raise AgolicBSERunnerError("staged candidate identity mismatch")
            self._replay_bytes(content, f"staged-{plan['plan_id']}-{digest}")
            _atomic_bytes(self.corpus / digest, content)
            generated.append(digest)
        coverage = self.replay_coverage()
        target_branch = plan["specification"]["target_branch"]
        target_elements = [
            element for element in coverage["elements"]
            if element.startswith(f"branch:{target_branch}:")
        ]
        return {
            "status": result["status"],
            "replay_verified": True,
            "replay_identity": coverage["replay_identity"],
            "coverage_elements": coverage["elements"],
            "covered_branches": coverage["branches"],
            "covered_functions": coverage["functions"],
            "corpus_artifacts": coverage["corpus_artifacts"],
            "generated_artifacts": generated,
            "target_coverage_elements": target_elements,
            "generated": len(generated),
            "elapsed_seconds": result["elapsed_seconds"] + (
                time.monotonic() - replay_started
            ),
            "cpu_seconds": result["cpu_seconds"],
            "solver_time_seconds": result["solver_time_seconds"],
            "reason": (
                "release-not-reached"
                if result["mode"] == "witness-guided"
                and not result["release_reached"]
                else ""
            ),
        }


def _apply_memory_limit(memory_mib: int) -> None:
    limit = memory_mib * 1024 * 1024
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    if hard != resource.RLIM_INFINITY:
        limit = min(limit, hard)
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))


def _apply_plan_environment(raw: Any) -> None:
    if not isinstance(raw, Mapping):
        raise AgolicBSERunnerError("BSE environment is invalid")
    for key, value in raw.items():
        if (
            not isinstance(key, str)
            or not key.startswith("SYMCC_")
            or not isinstance(value, str)
            or "\x00" in key
            or "\x00" in value
            or len(key) > 256
            or len(value.encode("utf-8")) > 4096
        ):
            raise AgolicBSERunnerError("BSE environment entry is invalid")
        os.environ[key] = value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--program", required=True)
    parser.add_argument("--program-sha256", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--max-steps", type=int, default=100_000)
    parser.add_argument("--max-states", type=int, default=4096)
    parser.add_argument("--max-artifacts", type=int, default=256)
    subparsers = parser.add_subparsers(dest="command", required=True)
    execute = subparsers.add_parser("execute")
    execute.add_argument("plan")
    replay = subparsers.add_parser("replay")
    replay.add_argument("plan")
    replay.add_argument("result")
    subparsers.add_parser("coverage")
    args = parser.parse_args()

    plan: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    memory_enforced = False
    if args.command in {"execute", "replay"}:
        plan, _encoded = _load_json(Path(args.plan), "Agolic plan")
    if args.command == "execute":
        assert plan is not None
        specification = _exact_keys(plan.get("specification"), {
            "target_id", "target_branch", "source_file", "function", "line",
            "mode", "profile", "time_limit_seconds", "memory_limit_mib",
            "witness", "environment", "symbolic_inputs",
        }, "Agolic plan specification")
        memory_mib = _integer(
            specification.get("memory_limit_mib"),
            "BSE memory limit", 1, 1_048_576,
        )
        _apply_memory_limit(memory_mib)
        _apply_plan_environment(specification.get("environment"))
        memory_enforced = True
    elif args.command == "replay":
        result, _encoded = _load_json(Path(args.result), "Agolic BSE result")

    runner = AgolicContinuationBSERunner(
        args.program,
        args.workspace,
        program_sha256=args.program_sha256,
        max_steps=args.max_steps,
        max_states=args.max_states,
        max_artifacts=args.max_artifacts,
        memory_limit_enforced=memory_enforced,
        environment_isolated=args.command in {"execute", "replay"},
    )
    if args.command == "execute":
        assert plan is not None
        output = runner.execute_plan(plan)
    elif args.command == "replay":
        assert plan is not None and result is not None
        output = runner.replay_result(plan, result)
    else:
        output = {
            "schema": REPLAY_RESULT_SCHEMA,
            **runner.replay_coverage(),
        }
    print(json.dumps(output, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
