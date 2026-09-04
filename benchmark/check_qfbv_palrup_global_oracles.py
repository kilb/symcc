#!/usr/bin/env python3
"""Run the official SAT 2026 PalRUP three-stage checker end to end."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from qfbv_palrup_pipeline import PalrupGlobalChecker  # noqa: E402
from qfbv_proof_wire import PALRUP_CHECKER_COMMIT  # noqa: E402


ORACLE_SCHEMA = "symcc-qfbv-palrup-global-official-oracle-v1"


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical_digest(value: object) -> str:
    return _digest(
        json.dumps(
            value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("ascii")
    )


def _require_tool(path: Path) -> tuple[Path, str]:
    executable = path.expanduser().resolve(strict=True)
    if not executable.is_file() or executable.stat().st_mode & 0o111 == 0:
        raise RuntimeError(f"PalRUP tool is not executable: {executable}")
    commit_file = executable.parent.parent / "share" / "source-commit"
    if (
        not commit_file.is_file()
        or commit_file.read_text(encoding="ascii").strip()
        != PALRUP_CHECKER_COMMIT
    ):
        raise RuntimeError(f"PalRUP tool is not pinned: {executable}")
    return executable, _digest(executable.read_bytes())


def _require_source(path: Path) -> Path:
    source = path.expanduser().resolve(strict=True)
    observed = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    if observed != PALRUP_CHECKER_COMMIT:
        raise RuntimeError("PalRUP fixture source is not pinned")
    clean = subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "diff",
            "--quiet",
            "HEAD",
            "--",
            "formulas/r3unsat_200.cnf",
            "proofs/r3unsat_200",
        ],
        check=False,
        timeout=10,
    )
    if clean.returncode != 0:
        raise RuntimeError("PalRUP fixture bytes differ from the pinned commit")
    return source


def run(arguments: argparse.Namespace) -> dict[str, object]:
    started = time.monotonic_ns()
    local, local_sha256 = _require_tool(arguments.local_check)
    redistribute, redistribute_sha256 = _require_tool(arguments.redistribute)
    confirm, confirm_sha256 = _require_tool(arguments.confirm)
    source = _require_source(arguments.source_root)
    formula = source / "formulas" / "r3unsat_200.cnf"
    proof = source / "proofs" / "r3unsat_200"
    if not formula.is_file() or not proof.is_dir():
        raise RuntimeError("official PalRUP r3unsat_200 fixture is unavailable")
    checker = PalrupGlobalChecker(
        local,
        redistribute,
        confirm,
        local_checker_sha256=local_sha256,
        redistribute_sha256=redistribute_sha256,
        confirm_sha256=confirm_sha256,
        timeout_ms=arguments.timeout_ms,
        max_parallel=arguments.max_parallel,
        read_buffer_kib=256,
        write_buffer_kib=256,
        merge_buffer_kib=256,
        queue_kib=1024,
    )
    receipt = checker.verify(formula, proof, 12)
    checker.validate_receipt(
        receipt,
        formula_path=formula,
        proof_root=proof,
        recheck=True,
    )
    result: dict[str, object] = {
        "schema": ORACLE_SCHEMA,
        "status": "passed",
        "source_commit": PALRUP_CHECKER_COMMIT,
        "fixture": "r3unsat_200",
        "tool_sha256": {
            "palrup_local_check": local_sha256,
            "palrup_redistribute": redistribute_sha256,
            "palrup_confirm": confirm_sha256,
        },
        "global_receipt": receipt,
        "independent_recheck": True,
        "claim_boundary": (
            "official 12-fragment mechanism oracle; no solver speedup, "
            "coverage, or multi-node scaling claim"
        ),
        "elapsed_us": (time.monotonic_ns() - started) // 1000,
    }
    result["result_sha256"] = _canonical_digest(result)
    return result


def main() -> int:
    prefix = Path.home() / ".local" / "opt" / "palrup-check-sat2026"
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path.home() / ".cache" / "symcc" / "palrup-check-sat2026",
    )
    parser.add_argument(
        "--local-check", type=Path, default=prefix / "bin" / "palrup_local_check"
    )
    parser.add_argument(
        "--redistribute",
        type=Path,
        default=prefix / "bin" / "palrup_redistribute",
    )
    parser.add_argument(
        "--confirm", type=Path, default=prefix / "bin" / "palrup_confirm"
    )
    parser.add_argument("--timeout-ms", type=int, default=300_000)
    parser.add_argument("--max-parallel", type=int, default=4)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    result = run(arguments)
    encoded = json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(encoded, encoding="ascii")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
