#!/usr/bin/env python3
"""Generate and verify replayable QF_BV backend conformance evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from qf_bv_backend import (
    QF_BV_OPERATORS,
    SmtLibQfbvSolver,
    lower_qfbv_query,
    normalize_qfbv_capabilities,
)
from query_store import QueryStore


CONFORMANCE_SCHEMA = "symcc-qfbv-backend-conformance-v1"
REPLAY_SCHEMA = "symcc-qfbv-backend-conformance-replay-v1"
BITWUZLA_VERSION = "0.9.1"
CVC5_VERSION = "1.1.2"
Z3_VERSION = "4.8.12"

_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SEMANTIC_VERSION = re.compile(r"(?<![0-9.])([0-9]+\.[0-9]+\.[0-9]+)")


@dataclass(frozen=True)
class BackendSpec:
    """One version-pinned, one-shot SMT-LIB backend invocation."""

    name: str
    command: tuple[str, ...]
    version_command: tuple[str, ...]
    expected_version: str


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def artifact_digest(artifact: Mapping[str, Any]) -> str:
    return _digest({
        key: value
        for key, value in artifact.items()
        if key != "artifact_sha256"
    })


def build_operator_matrix_envelope(timeout_ms: int = 5000) -> dict[str, Any]:
    """Build a SAT Query IR formula that exercises every supported operator."""
    nodes: list[dict[str, Any]] = []

    def node(
        op: str,
        bits: int,
        children: Sequence[int] = (),
        **attrs: Any,
    ) -> int:
        identifier = len(nodes)
        nodes.append({
            "id": identifier,
            "op": op,
            "bits": bits,
            "children": list(children),
            "attrs": attrs,
        })
        return identifier

    def constant(value: int, bits: int = 8) -> int:
        return node(
            "constant",
            bits,
            value_hex=value.to_bytes((bits + 7) // 8, "big").hex(),
        )

    def expect(value_node: int, value: int, bits: int = 8) -> int:
        return node("equal", 1, (value_node, constant(value, bits)))

    x = node("read", 8, index=0)
    y = node("read", 8, index=1)
    true = node("bool", 1, value=True)
    false = node("bool", 1, value=False)
    predicates = [expect(x, 0x42), expect(y, 0x03)]
    concatenated = node("concat", 16, (x, y))
    predicates.extend([
        expect(concatenated, 0x4203, 16),
        expect(node("extract", 8, (concatenated,), index=0), 0x03),
        expect(node("zext", 16, (x,)), 0x0042, 16),
        expect(node("sext", 16, (x,)), 0x0042, 16),
        expect(node("add", 8, (x, y)), 0x45),
        expect(node("sub", 8, (x, y)), 0x3F),
        expect(node("mul", 8, (x, y)), 0xC6),
        expect(node("udiv", 8, (x, y)), 0x16),
        expect(node("sdiv", 8, (x, y)), 0x16),
        expect(node("urem", 8, (x, y)), 0),
        expect(node("srem", 8, (x, y)), 0),
        expect(node("neg", 8, (x,)), 0xBE),
        expect(node("not", 8, (x,)), 0xBD),
        expect(node("and", 8, (x, y)), 0x02),
        expect(node("or", 8, (x, y)), 0x43),
        expect(node("xor", 8, (x, y)), 0x41),
        expect(node("shl", 8, (x, y)), 0x10),
        expect(node("lshr", 8, (x, y)), 0x08),
        expect(node("ashr", 8, (x, y)), 0x08),
        node("equal", 1, (x, x)),
        node("distinct", 1, (x, y)),
        node("ult", 1, (y, x)),
        node("ule", 1, (y, x)),
        node("ugt", 1, (x, y)),
        node("uge", 1, (x, y)),
        node("slt", 1, (y, x)),
        node("sle", 1, (y, x)),
        node("sgt", 1, (x, y)),
        node("sge", 1, (x, y)),
        node("land", 1, (true, true)),
        node("lor", 1, (false, true)),
        node("lnot", 1, (false,)),
        expect(node("ite", 8, (true, x, y)), 0x42),
        expect(node("rol", 8, (x, y)), 0x12),
        expect(node("ror", 8, (x, y)), 0x48),
    ])
    return {
        "schema": "symcc-query-ir-v1",
        "producer": "qfbv-conformance",
        "nodes": nodes,
        "prefix_roots": predicates[:-1],
        "target_root": predicates[-1],
        "input_hex": "0000",
        "timeout_ms": max(1, min(int(timeout_ms), 60000)),
        "metadata": {"source": "qfbv-conformance-operator-matrix"},
        "smt2": "(assert true)\n",
        "prefix_smt2": "(assert true)\n",
        "target_smt2": "(assert true)\n",
    }


def build_unsat_probe_envelope(timeout_ms: int = 5000) -> dict[str, Any]:
    """Build a contradiction used to audit explicit UNSAT authorization."""
    return {
        "schema": "symcc-query-ir-v1",
        "producer": "qfbv-conformance",
        "nodes": [
            {
                "id": 0,
                "op": "read",
                "bits": 8,
                "children": [],
                "attrs": {"index": 0},
            },
            {
                "id": 1,
                "op": "constant",
                "bits": 8,
                "children": [],
                "attrs": {"value_hex": "42"},
            },
            {
                "id": 2,
                "op": "equal",
                "bits": 1,
                "children": [0, 1],
                "attrs": {},
            },
            {
                "id": 3,
                "op": "constant",
                "bits": 8,
                "children": [],
                "attrs": {"value_hex": "43"},
            },
            {
                "id": 4,
                "op": "equal",
                "bits": 1,
                "children": [0, 3],
                "attrs": {},
            },
        ],
        "prefix_roots": [2],
        "target_root": 4,
        "input_hex": "00",
        "timeout_ms": max(1, min(int(timeout_ms), 60000)),
        "metadata": {"source": "qfbv-conformance-unsat-probe"},
        "smt2": "(assert true)\n",
        "prefix_smt2": "(assert true)\n",
        "target_smt2": "(assert true)\n",
    }


def default_backend_specs() -> tuple[BackendSpec, ...]:
    return (
        BackendSpec(
            name="z3-4.8.12",
            command=(
                "z3",
                "-smt2",
                "-model",
                "-t:{timeout_ms}",
                "{query}",
            ),
            version_command=("z3", "--version"),
            expected_version=Z3_VERSION,
        ),
        BackendSpec(
            name="cvc5-1.1.2",
            command=(
                "cvc5",
                "--lang",
                "smt2",
                "--produce-models",
                "{query}",
            ),
            version_command=("cvc5", "--version"),
            expected_version=CVC5_VERSION,
        ),
        BackendSpec(
            name="bitwuzla-0.9.1",
            command=(
                "bitwuzla",
                "--lang",
                "smt2",
                "--produce-models",
                "--time-limit",
                "{timeout_ms}",
                "{query}",
            ),
            version_command=("bitwuzla", "--version"),
            expected_version=BITWUZLA_VERSION,
        ),
    )


def normalize_backend_spec(raw: Mapping[str, Any]) -> BackendSpec:
    if not isinstance(raw, Mapping):
        raise ValueError("backend specification must be an object")

    def command(name: str) -> tuple[str, ...]:
        value = raw.get(name)
        if (
            not isinstance(value, Sequence)
            or isinstance(value, (str, bytes))
            or not value
            or len(value) > 64
        ):
            raise ValueError(f"{name} must be a non-empty bounded list")
        normalized = tuple(str(item) for item in value)
        if any(not item or len(item) > 4096 for item in normalized):
            raise ValueError(f"{name} contains an invalid argument")
        return normalized

    name = str(raw.get("name", ""))[:128]
    expected_version = str(raw.get("expected_version", ""))[:64]
    if not name or not re.fullmatch(r"[A-Za-z0-9_.+-]+", name):
        raise ValueError("backend name must be a bounded portable identifier")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", expected_version):
        raise ValueError("expected_version must be an exact semantic version")
    return BackendSpec(
        name=name,
        command=command("command"),
        version_command=command("version_command"),
        expected_version=expected_version,
    )


def load_backend_specs(raw: str | Path) -> tuple[BackendSpec, ...]:
    text = str(raw)
    path = Path(text)
    if path.is_file():
        value = json.loads(path.read_text(encoding="ascii"))
    else:
        value = json.loads(text)
    if isinstance(value, Mapping):
        value = value.get("backends")
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or not 1 <= len(value) <= 16
    ):
        raise ValueError("backend configuration must contain 1--16 backends")
    specs = tuple(normalize_backend_spec(item) for item in value)
    if len({spec.name for spec in specs}) != len(specs):
        raise ValueError("backend names must be unique")
    return specs


def _resolve_executable(command: str) -> Path:
    resolved = shutil.which(command)
    if resolved is None:
        candidate = Path(command)
        if not candidate.is_file():
            raise FileNotFoundError(f"backend executable not found: {command}")
        resolved = str(candidate)
    path = Path(resolved).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"backend executable not found: {command}")
    return path


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _version_identity(spec: BackendSpec) -> dict[str, Any]:
    executable = _resolve_executable(spec.command[0])
    version_executable = _resolve_executable(spec.version_command[0])
    if executable != version_executable:
        raise ValueError(
            f"{spec.name} command and version command resolve differently")
    completed = subprocess.run(
        list(spec.version_command),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10.0,
        check=False,
    )
    output = (completed.stdout + "\n" + completed.stderr).strip()
    if completed.returncode != 0:
        raise RuntimeError(
            f"{spec.name} version command exited {completed.returncode}")
    match = _SEMANTIC_VERSION.search(output)
    if match is None:
        raise RuntimeError(f"{spec.name} did not report a semantic version")
    actual_version = match.group(1)
    if actual_version != spec.expected_version:
        raise RuntimeError(
            f"{spec.name} expected {spec.expected_version}, "
            f"found {actual_version}")
    return {
        "executable": str(executable),
        "executable_sha256": _file_sha256(executable),
        "version_output": output[:4096],
        "actual_version": actual_version,
    }


def current_backend_identity(spec: BackendSpec) -> dict[str, Any]:
    """Resolve and hash the exact backend binary used by a later campaign."""
    return _version_identity(spec)


def _execute_case(
    envelope: Mapping[str, Any],
    spec: BackendSpec,
    *,
    accept_unsat: bool,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="symcc-qfbv-conformance-") as root:
        store = QueryStore(root)
        query_id, _ = store.ingest(envelope)
        lease = store.claim(f"conformance-{spec.name}")
        if lease is None:
            raise RuntimeError("conformance query was not claimable")
        capabilities = normalize_qfbv_capabilities({
            "accept_unsat": accept_unsat,
        })
        result = dict(SmtLibQfbvSolver(
            store,
            spec.command,
            name=spec.name,
            capabilities=capabilities,
        )(lease))
        completed = store.complete(lease, f"conformance-{spec.name}", result)
    return {
        "query_id": query_id,
        "status": str(result.get("status", "")),
        "backend_status": str(result.get("backend_status", "")),
        "assignments": result.get("assignments", {}),
        "backend_model_verified": result.get("backend_model_verified") is True,
        "backend_unsat_authorized": (
            result.get("backend_unsat_authorized") is True
        ),
        "backend_unsat_confirmation": str(
            result.get("backend_unsat_confirmation", "")),
        "backend_capabilities": result.get("backend_capabilities", {}),
        "lowering_certificate": result.get("lowering_certificate", {}),
        "elapsed_us": max(0, int(result.get("elapsed_us", 0))),
        "reason": str(result.get("reason", ""))[:512],
        "store_completed": completed,
    }


def _backend_passed(entry: Mapping[str, Any]) -> bool:
    sat = entry.get("operator_matrix")
    rejected = entry.get("unsat_rejected")
    authorized = entry.get("unsat_authorized")
    return (
        isinstance(sat, Mapping)
        and sat.get("status") == "sat"
        and sat.get("assignments") == {"0": 0x42, "1": 0x03}
        and sat.get("backend_model_verified") is True
        and sat.get("store_completed") is True
        and isinstance(rejected, Mapping)
        and rejected.get("status") == "unknown"
        and rejected.get("backend_status") == "unsat"
        and rejected.get("backend_unsat_authorized") is False
        and rejected.get("store_completed") is True
        and isinstance(authorized, Mapping)
        and authorized.get("status") == "unsat"
        and authorized.get("backend_unsat_authorized") is True
        and authorized.get("store_completed") is True
    )


def _semantic_projection(artifact: Mapping[str, Any]) -> dict[str, Any]:
    backends = artifact.get("backends", [])
    projected_backends = []
    if isinstance(backends, Sequence) and not isinstance(
            backends, (str, bytes)):
        for entry in backends:
            if not isinstance(entry, Mapping):
                continue
            projected_entry = {
                "name": entry.get("name"),
                "command": entry.get("command"),
                "version_command": entry.get("version_command"),
                "expected_version": entry.get("expected_version"),
                "actual_version": entry.get("actual_version"),
            }
            for case_name in (
                "operator_matrix",
                "unsat_rejected",
                "unsat_authorized",
            ):
                case = entry.get(case_name)
                projected_case = dict(case) if isinstance(case, Mapping) else case
                if isinstance(projected_case, dict):
                    projected_case.pop("elapsed_us", None)
                projected_entry[case_name] = projected_case
            projected_backends.append(projected_entry)
    return {
        "operator_matrix": artifact.get("operator_matrix"),
        "unsat_probe": artifact.get("unsat_probe"),
        "backends": projected_backends,
    }


def semantic_digest(artifact: Mapping[str, Any]) -> str:
    return _digest(_semantic_projection(artifact))


def run_qfbv_conformance(
    specs: Sequence[BackendSpec] | None = None,
    *,
    timeout_ms: int = 5000,
) -> dict[str, Any]:
    specs = tuple(specs or default_backend_specs())
    if not specs or len({spec.name for spec in specs}) != len(specs):
        raise ValueError("conformance requires uniquely named backends")
    timeout_ms = max(1, min(int(timeout_ms), 60000))
    matrix = build_operator_matrix_envelope(timeout_ms)
    unsat = build_unsat_probe_envelope(timeout_ms)
    entries: list[dict[str, Any]] = []
    for spec in specs:
        identity = _version_identity(spec)
        entry = {
            "name": spec.name,
            "command": list(spec.command),
            "version_command": list(spec.version_command),
            "expected_version": spec.expected_version,
            **identity,
            "operator_matrix": _execute_case(
                matrix,
                spec,
                accept_unsat=False,
            ),
            "unsat_rejected": _execute_case(
                unsat,
                spec,
                accept_unsat=False,
            ),
            "unsat_authorized": _execute_case(
                unsat,
                spec,
                accept_unsat=True,
            ),
        }
        entry["passed"] = _backend_passed(entry)
        entries.append(entry)
    artifact: dict[str, Any] = {
        "schema": CONFORMANCE_SCHEMA,
        "generated_unix_ms": int(time.time() * 1000),
        "timeout_ms": timeout_ms,
        "operator_matrix": {
            "envelope_sha256": _digest(matrix),
            "operators": sorted(QF_BV_OPERATORS),
            "expected_assignments": {"0": 0x42, "1": 0x03},
        },
        "unsat_probe": {
            "envelope_sha256": _digest(unsat),
            "policy": "reject-unless-capability-authorized",
        },
        "backends": entries,
        "passed": all(entry["passed"] for entry in entries),
    }
    artifact["semantic_sha256"] = semantic_digest(artifact)
    artifact["artifact_sha256"] = artifact_digest(artifact)
    return artifact


def _expected_lowering(
    envelope: Mapping[str, Any],
    *,
    accept_unsat: bool,
) -> tuple[str, dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="symcc-qfbv-verify-") as root:
        store = QueryStore(root)
        query_id, _ = store.ingest(envelope)
        loaded = store.load_query_ir(query_id)
        if loaded is None:
            raise RuntimeError("canonical conformance Query IR is unavailable")
        _, certificate, _ = lower_qfbv_query(
            query_id,
            loaded[0],
            loaded[1],
            normalize_qfbv_capabilities({
                "accept_unsat": accept_unsat,
            }),
        )
    return query_id, certificate


def _verify_case(
    case: Any,
    *,
    query_id: str,
    certificate: Mapping[str, Any],
) -> bool:
    if not isinstance(case, Mapping):
        return False
    capabilities = case.get("backend_capabilities")
    return (
        case.get("query_id") == query_id
        and case.get("lowering_certificate") == certificate
        and isinstance(capabilities, Mapping)
        and capabilities.get("capability_sha256")
        == certificate.get("capability_sha256")
        and isinstance(case.get("elapsed_us"), int)
        and case.get("elapsed_us", -1) >= 0
        and case.get("store_completed") is True
    )


def verify_qfbv_conformance(artifact: Mapping[str, Any]) -> bool:
    try:
        if artifact.get("schema") != CONFORMANCE_SCHEMA:
            return False
        if artifact.get("artifact_sha256") != artifact_digest(artifact):
            return False
        if artifact.get("semantic_sha256") != semantic_digest(artifact):
            return False
        timeout_ms = int(artifact.get("timeout_ms", 0))
        if not 1 <= timeout_ms <= 60000:
            return False
        if int(artifact.get("generated_unix_ms", -1)) < 0:
            return False
        matrix = build_operator_matrix_envelope(timeout_ms)
        unsat = build_unsat_probe_envelope(timeout_ms)
        matrix_identity = artifact.get("operator_matrix")
        unsat_identity = artifact.get("unsat_probe")
        if (
            not isinstance(matrix_identity, Mapping)
            or matrix_identity.get("envelope_sha256") != _digest(matrix)
            or matrix_identity.get("operators") != sorted(QF_BV_OPERATORS)
            or matrix_identity.get("expected_assignments")
            != {"0": 0x42, "1": 0x03}
            or not isinstance(unsat_identity, Mapping)
            or unsat_identity.get("envelope_sha256") != _digest(unsat)
            or unsat_identity.get("policy")
            != "reject-unless-capability-authorized"
        ):
            return False
        matrix_id, matrix_certificate = _expected_lowering(
            matrix,
            accept_unsat=False,
        )
        unsat_id, rejected_certificate = _expected_lowering(
            unsat,
            accept_unsat=False,
        )
        _, authorized_certificate = _expected_lowering(
            unsat,
            accept_unsat=True,
        )
        backends = artifact.get("backends")
        if (
            not isinstance(backends, Sequence)
            or isinstance(backends, (str, bytes))
            or not 1 <= len(backends) <= 16
        ):
            return False
        names: set[str] = set()
        for entry in backends:
            if not isinstance(entry, Mapping):
                return False
            spec = normalize_backend_spec(entry)
            if spec.name in names:
                return False
            names.add(spec.name)
            if entry.get("actual_version") != spec.expected_version:
                return False
            if not isinstance(entry.get("version_output"), str):
                return False
            if len(entry["version_output"]) > 4096:
                return False
            match = _SEMANTIC_VERSION.search(entry["version_output"])
            if match is None or match.group(1) != spec.expected_version:
                return False
            if not Path(str(entry.get("executable", ""))).is_absolute():
                return False
            if not _HEX_DIGEST.fullmatch(
                    str(entry.get("executable_sha256", ""))):
                return False
            if not _verify_case(
                entry.get("operator_matrix"),
                query_id=matrix_id,
                certificate=matrix_certificate,
            ):
                return False
            if not _verify_case(
                entry.get("unsat_rejected"),
                query_id=unsat_id,
                certificate=rejected_certificate,
            ):
                return False
            if not _verify_case(
                entry.get("unsat_authorized"),
                query_id=unsat_id,
                certificate=authorized_certificate,
            ):
                return False
            if entry.get("passed") is not True or not _backend_passed(entry):
                return False
        return artifact.get("passed") is True
    except (KeyError, OSError, TypeError, ValueError):
        return False


def replay_qfbv_conformance(
    artifact: Mapping[str, Any],
) -> dict[str, Any]:
    if not verify_qfbv_conformance(artifact):
        raise ValueError("source QF_BV conformance artifact is invalid")
    raw_backends = artifact.get("backends")
    assert isinstance(raw_backends, Sequence)
    specs = tuple(normalize_backend_spec(entry) for entry in raw_backends)
    replay = run_qfbv_conformance(
        specs,
        timeout_ms=int(artifact["timeout_ms"]),
    )
    result = {
        "schema": REPLAY_SCHEMA,
        "source_artifact_sha256": artifact["artifact_sha256"],
        "source_semantic_sha256": artifact["semantic_sha256"],
        "replay_semantic_sha256": replay["semantic_sha256"],
        "semantic_match": (
            replay["semantic_sha256"] == artifact["semantic_sha256"]
        ),
        "replay": replay,
    }
    result["replay_sha256"] = _digest(result)
    return result


def _write_or_print(value: Mapping[str, Any], output: str | None) -> None:
    payload = _canonical_json(value) + b"\n"
    if output:
        Path(output).write_bytes(payload)
    else:
        print(payload.decode("ascii"), end="")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--verify",
        help="verify an existing artifact without invoking a solver",
    )
    action.add_argument(
        "--replay",
        help="verify and semantically replay an existing artifact",
    )
    parser.add_argument(
        "--backend-config",
        help="JSON/path list of version-pinned backend specifications",
    )
    parser.add_argument("--timeout-ms", type=int, default=5000)
    parser.add_argument("--output")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.verify:
        artifact = json.loads(Path(args.verify).read_text(encoding="ascii"))
        valid = (
            isinstance(artifact, Mapping)
            and verify_qfbv_conformance(artifact)
        )
        print(json.dumps({"verified": valid}, sort_keys=True))
        return 0 if valid else 1
    if args.replay:
        artifact = json.loads(Path(args.replay).read_text(encoding="ascii"))
        if not isinstance(artifact, Mapping):
            raise ValueError("QF_BV conformance artifact must be an object")
        replay = replay_qfbv_conformance(artifact)
        _write_or_print(replay, args.output)
        return 0 if replay["semantic_match"] else 1
    specs = (
        load_backend_specs(args.backend_config)
        if args.backend_config
        else default_backend_specs()
    )
    artifact = run_qfbv_conformance(specs, timeout_ms=args.timeout_ms)
    _write_or_print(artifact, args.output)
    return 0 if verify_qfbv_conformance(artifact) else 1


if __name__ == "__main__":
    raise SystemExit(main())
