#!/usr/bin/env python3
"""Run non-skippable official LIDRUP and PalRUP interoperability oracles."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from qfbv_incremental_proof import (  # noqa: E402
    CLAUSE_PROTOCOL,
    CLAUSE_RECORD_SCHEMA,
    PROJECT_LRUP_DAG_SCHEMA,
    IncrementalProofChecker,
    IncrementalProofStore,
    make_rup_clause_record,
    make_unsat_result_receipt,
)
from qfbv_incremental_sat import bitblast_qfbv_query  # noqa: E402
from qfbv_proof_wire import (  # noqa: E402
    LIDRUP_CHECKER_COMMIT,
    PALRUP_CHECKER_COMMIT,
    LidrupExternalChecker,
    PalrupDelete,
    PalrupFragmentOracle,
    PalrupImport,
    PalrupProduce,
    encode_palrup_fragment,
    import_lidrup_artifacts,
)


ORACLE_SCHEMA = "symcc-qfbv-proof-wire-official-oracle-v1"


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical_digest(value: object) -> str:
    return _digest(
        json.dumps(
            value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("ascii")
    )


def _plan():
    return bitblast_qfbv_query(
        "proof-wire-official-oracle",
        ["true", "false"],
        {
            "true": {
                "op": "bool",
                "bits": 1,
                "children": [],
                "attrs": {"value": True},
            },
            "false": {
                "op": "bool",
                "bits": 1,
                "children": [],
                "attrs": {"value": False},
            },
        },
    )


def _require_tool(path: Path, commit: str) -> tuple[Path, str]:
    executable = path.expanduser().resolve(strict=True)
    if not executable.is_file() or executable.stat().st_mode & 0o111 == 0:
        raise RuntimeError(f"official oracle tool is not executable: {executable}")
    commit_file = executable.parent.parent / "share" / "source-commit"
    if (
        not commit_file.is_file()
        or commit_file.read_text(encoding="ascii").strip() != commit
    ):
        raise RuntimeError(f"official oracle source commit is not pinned: {executable}")
    return executable, _digest(executable.read_bytes())


def _recursive_result(plan, store):
    child = make_rup_clause_record(
        plan,
        [-plan.assumptions[1], -1],
        dependency_assumptions=[plan.assumptions[1]],
        source_worker="official-child",
        worker_epoch=0,
        sequence=0,
    )
    child_digest, _created = store.publish(child)
    imported_id = len(plan.clauses) + 1
    parent = {
        "schema": CLAUSE_RECORD_SCHEMA,
        "fragment_schema": PROJECT_LRUP_DAG_SCHEMA,
        "protocol": CLAUSE_PROTOCOL,
        "formula_sha256": plan.formula_sha256,
        "cnf_sha256": child["cnf_sha256"],
        "base_clause_count": len(plan.clauses),
        "max_variable": plan.max_variable,
        "source_worker": "official-parent",
        "worker_epoch": 1,
        "sequence": 0,
        "dependency_assumptions": list(plan.assumptions),
        "imports": [
            {
                "receipt_sha256": child_digest,
                "local_clause_id": imported_id,
                "clause": child["shared_clause"],
            }
        ],
        "proof_steps": [
            {
                "clause_id": imported_id + 1,
                "clause": [-2, -3],
                "hints": [imported_id, 2],
            }
        ],
        "shared_clause": [-2, -3],
    }
    parent["record_sha256"] = _canonical_digest(parent)
    parent_digest, _created = store.publish(parent)
    return make_unsat_result_receipt(plan, parent_digest, plan.assumptions)


def run(args: argparse.Namespace) -> dict[str, object]:
    started = time.monotonic_ns()
    lidrup_path, lidrup_sha256 = _require_tool(args.lidrup_check, LIDRUP_CHECKER_COMMIT)
    palrup_path, palrup_sha256 = _require_tool(
        args.palrup_converter, PALRUP_CHECKER_COMMIT
    )
    plan = _plan()
    with tempfile.TemporaryDirectory(prefix="symcc-proof-wire-oracle-") as directory:
        store = IncrementalProofStore(Path(directory) / "proofs")
        result = _recursive_result(plan, store)
        lidrup = LidrupExternalChecker(
            lidrup_path,
            checker_sha256=lidrup_sha256,
        )
        artifacts, lidrup_receipt = lidrup.verify(plan, result, store)
        lidrup.validate_receipt(plan, result, store, artifacts, lidrup_receipt)
        imported, imported_result = import_lidrup_artifacts(
            plan,
            artifacts.interaction,
            artifacts.proof,
            source_worker="official-roundtrip",
            worker_epoch=2,
            sequence=0,
        )
        imported_digest, _created = store.publish(imported)
        imported_authorization = IncrementalProofChecker(store).verify_result_receipt(
            plan, imported_result
        )
        if imported_digest != imported_authorization.clause_receipt_sha256:
            raise RuntimeError("official LIDRUP round trip changed record identity")

    directives = (
        PalrupImport(4, (1, -2)),
        PalrupProduce(7, (-3,), (1, 4)),
        PalrupDelete((4, 7)),
    )
    fragment = encode_palrup_fragment(directives)
    palrup = PalrupFragmentOracle(
        palrup_path,
        converter_sha256=palrup_sha256,
    )
    converted, palrup_receipt = palrup.verify(fragment)
    result_body: dict[str, object] = {
        "schema": ORACLE_SCHEMA,
        "status": "passed",
        "lidrup": {
            "checker_sha256": lidrup_sha256,
            "checker_source_commit": LIDRUP_CHECKER_COMMIT,
            "checker_version": lidrup_receipt["checker_version"],
            "checker_mode": lidrup_receipt["checker_mode"],
            "receipt_sha256": lidrup_receipt["receipt_sha256"],
            "artifact_sha256": artifacts.metadata()["artifact_sha256"],
            "interaction_bytes": len(artifacts.interaction),
            "proof_bytes": len(artifacts.proof),
            "flattened_learned_clauses": artifacts.learned_clause_count,
            "recursive_imports": 1,
            "round_trip_record_sha256": imported_digest,
        },
        "palrup": {
            "converter_sha256": palrup_sha256,
            "checker_source_commit": PALRUP_CHECKER_COMMIT,
            "receipt_sha256": palrup_receipt["receipt_sha256"],
            "fragment_sha256": _digest(fragment),
            "fragment_bytes": len(fragment),
            "converted_text_sha256": _digest(converted),
            "directive_count": len(directives),
            "scope": palrup_receipt["scope"],
        },
        "elapsed_us": (time.monotonic_ns() - started) // 1000,
    }
    result_body["result_sha256"] = _canonical_digest(result_body)
    return result_body


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--lidrup-check",
        type=Path,
        default=Path.home()
        / ".local"
        / "opt"
        / "lidrup-check-0.0.7"
        / "bin"
        / "lidrup-check",
    )
    parser.add_argument(
        "--palrup-converter",
        type=Path,
        default=Path.home()
        / ".local"
        / "opt"
        / "palrup-check-sat2026"
        / "bin"
        / "proof_fragment_to_txt",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run(args)
    encoded = json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="ascii")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
