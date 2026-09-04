#!/usr/bin/env python3
# RUN: python3 %s

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from qfbv_palrup_pipeline import (  # noqa: E402
    PALRUP_GLOBAL_PROTOCOL,
    PALRUP_GLOBAL_RECEIPT_SCHEMA,
    PalrupGlobalChecker,
)
from qfbv_proof_wire import ProofWireError  # noqa: E402


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical_digest(value: object) -> str:
    return _digest(
        json.dumps(
            value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("ascii")
    )


def _write_tool(
    root: Path,
    name: str,
    stage: str,
    *,
    skip_confirm_rank: int = -1,
    skip_unsat: bool = False,
    flood: bool = False,
    extra_confirm: bool = False,
    fail: bool = False,
    diagnostic: bool = False,
    delay_ms: int = 0,
    hash_bytes: int = 16,
) -> tuple[Path, str]:
    content = f"""#!/usr/bin/env python3
import hashlib
import math
from pathlib import Path
import sys
import time

STAGE = {stage!r}
SKIP_CONFIRM_RANK = {skip_confirm_rank}
SKIP_UNSAT = {skip_unsat!r}
FLOOD = {flood!r}
EXTRA_CONFIRM = {extra_confirm!r}
FAIL = {fail!r}
DIAGNOSTIC = {diagnostic!r}
DELAY_MS = {delay_ms}
HASH_BYTES = {hash_bytes}

options = {{}}
for raw in sys.argv[1:]:
    key, value = raw.split('=', 1)
    options[key] = value

rank = int(options['-pal-id'])
solvers = int(options['-num-solvers'])
if rank == 0 and DELAY_MS:
    time.sleep(DELAY_MS / 1000)
if rank == 0 and FAIL:
    raise SystemExit(17)
if rank == 0 and DIAGNOSTIC:
    print('unexpected diagnostic', file=sys.stderr)
width = math.isqrt(solvers)
if width * width < solvers:
    width += 1
working = Path(options['-working-path'])
worker = working / str(rank // width) / str(rank)

if STAGE == 'local':
    formula = Path(options['-formula-path']).read_bytes()
    if not formula:
        raise SystemExit(7)
    fragment = (Path(options['-palrup-path']) / str(rank // width) /
                str(rank) / 'out.palrup')
    payload = fragment.read_bytes()
    fragment.with_name('out.palrup.hash').write_bytes(
        hashlib.sha256(payload).digest()[:HASH_BYTES]
    )
    worker.joinpath('out.palrup_proxy').write_bytes(
        b'P' + rank.to_bytes(7, 'little') + hashlib.sha256(payload).digest()[:16]
    )
    if rank == 0 and not SKIP_UNSAT:
        (working / '.unsat_found' / '0').mkdir(parents=True)
    if FLOOD and rank == 0:
        print('x' * 70000)
elif STAGE == 'redistribute':
    worker.joinpath('out.palrup_import').write_bytes(
        b'I' + rank.to_bytes(7, 'little') + bytes(16)
    )
elif STAGE == 'confirm':
    fragment_hash = (Path(options['-palrup-path']) / str(rank // width) /
                     str(rank) / 'out.palrup.hash')
    if len(fragment_hash.read_bytes()) != 16:
        raise SystemExit(8)
    column = rank % width
    for row in range(width):
        source = row * width + column
        path = working / str(row) / str(source) / 'out.palrup_import'
        if not path.is_file():
            raise SystemExit(9)
    if rank != SKIP_CONFIRM_RANK:
        worker.joinpath('.check_ok').mkdir()
    if rank == 0 and EXTRA_CONFIRM:
        (working / 'extra' / '.check_ok').mkdir(parents=True)
else:
    raise SystemExit(10)
print(f'{{STAGE}} rank={{rank}}')
"""
    path = root / name
    path.write_text(content, encoding="ascii")
    path.chmod(0o755)
    return path, _digest(path.read_bytes())


def _proof_fixture(root: Path, solvers: int = 3) -> tuple[Path, Path]:
    formula = root / "input.cnf"
    formula.write_bytes(b"p cnf 1 2\n1 0\n-1 0\n")
    proof = root / "proof"
    width = 1
    while width * width < solvers:
        width += 1
    for rank in range(solvers):
        directory = proof / str(rank // width) / str(rank)
        directory.mkdir(parents=True)
        content = b"" if rank == 1 else f"fragment-{rank}".encode("ascii")
        directory.joinpath("out.palrup").write_bytes(content)
    return formula, proof


def _checker(
    root: Path,
    *,
    skip_confirm_rank: int = -1,
    skip_unsat: bool = False,
    flood: bool = False,
    extra_confirm: bool = False,
    fail_local: bool = False,
    diagnostic_local: bool = False,
    delay_local_ms: int = 0,
    timeout_ms: int = 10_000,
    hash_bytes: int = 16,
) -> tuple[PalrupGlobalChecker, dict[str, Path]]:
    local, local_digest = _write_tool(
        root,
        "palrup_local_check",
        "local",
        skip_unsat=skip_unsat,
        flood=flood,
        fail=fail_local,
        diagnostic=diagnostic_local,
        delay_ms=delay_local_ms,
        hash_bytes=hash_bytes,
    )
    redist, redist_digest = _write_tool(
        root, "palrup_redistribute", "redistribute"
    )
    confirm, confirm_digest = _write_tool(
        root,
        "palrup_confirm",
        "confirm",
        skip_confirm_rank=skip_confirm_rank,
        extra_confirm=extra_confirm,
    )
    checker = PalrupGlobalChecker(
        local,
        redist,
        confirm,
        local_checker_sha256=local_digest,
        redistribute_sha256=redist_digest,
        confirm_sha256=confirm_digest,
        timeout_ms=timeout_ms,
        max_parallel=3,
        read_buffer_kib=4,
        write_buffer_kib=4,
        merge_buffer_kib=4,
        queue_kib=4,
    )
    return checker, {"local": local, "redist": redist, "confirm": confirm}


class PalrupGlobalPipelineTest(unittest.TestCase):
    def test_complete_pipeline_issues_and_independently_rechecks_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            formula, proof = _proof_fixture(root)
            checker, _tools = _checker(root)
            receipt = checker.verify(formula, proof, 3)
            self.assertEqual(receipt["schema"], PALRUP_GLOBAL_RECEIPT_SCHEMA)
            self.assertEqual(receipt["protocol"], PALRUP_GLOBAL_PROTOCOL)
            self.assertEqual(receipt["status"], "global-unsat-confirmed")
            self.assertEqual(receipt["bundle"]["matrix_width"], 2)
            self.assertEqual(receipt["bundle"]["matrix_tasks"], 4)
            self.assertEqual(len(receipt["phases"]["local_check"]), 3)
            self.assertEqual(len(receipt["phases"]["redistribute"]), 4)
            self.assertEqual(len(receipt["phases"]["confirm"]), 3)
            self.assertEqual(receipt["confirmed_ranks"], [0, 1, 2])
            self.assertEqual(receipt["unsat_witness_ranks"], [0])
            checker.validate_receipt(receipt, recheck=False)
            checker.validate_receipt(
                receipt,
                formula_path=formula,
                proof_root=proof,
                recheck=True,
            )
            self.assertEqual(list(proof.rglob("*.hash")), [])

    def test_receipt_schema_and_nested_conservation_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            formula, proof = _proof_fixture(root)
            checker, _tools = _checker(root)
            receipt = checker.verify(formula, proof, 3)

            extra = dict(receipt)
            extra["unexpected"] = True
            extra_body = dict(extra)
            extra_body.pop("receipt_sha256")
            extra["receipt_sha256"] = _canonical_digest(extra_body)
            with self.assertRaisesRegex(ProofWireError, "fields differ"):
                checker.validate_receipt(extra, recheck=False)

            broken = json.loads(json.dumps(receipt))
            broken["workspace"]["proxies"].pop()
            workspace_body = dict(broken["workspace"])
            workspace_body.pop("workspace_sha256")
            broken["workspace"]["workspace_sha256"] = _canonical_digest(
                workspace_body
            )
            receipt_body = dict(broken)
            receipt_body.pop("receipt_sha256")
            broken["receipt_sha256"] = _canonical_digest(receipt_body)
            with self.assertRaisesRegex(ProofWireError, "proxy count"):
                checker.validate_receipt(broken, recheck=False)

            noncanonical = json.loads(json.dumps(receipt))
            noncanonical["unsat_witness_ranks"] = [2, 0]
            receipt_body = dict(noncanonical)
            receipt_body.pop("receipt_sha256")
            noncanonical["receipt_sha256"] = _canonical_digest(receipt_body)
            with self.assertRaisesRegex(ProofWireError, "witness set"):
                checker.validate_receipt(noncanonical, recheck=False)

    def test_recheck_accepts_a_different_valid_unsat_marker_race_winner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            formula, proof = _proof_fixture(root)
            checker, _tools = _checker(root)
            receipt = checker.verify(formula, proof, 3)
            rechecked = json.loads(json.dumps(receipt))
            rechecked["unsat_witness_ranks"] = [2]
            body = dict(rechecked)
            body.pop("receipt_sha256")
            rechecked["receipt_sha256"] = _canonical_digest(body)
            with mock.patch.object(checker, "verify", return_value=rechecked):
                validated = checker.validate_receipt(
                    receipt,
                    formula_path=formula,
                    proof_root=proof,
                    recheck=True,
                )
            self.assertEqual(validated["unsat_witness_ranks"], [0])

    def test_missing_confirmation_marker_cannot_authorize_unsat(self):
        cases = (
            ({"skip_confirm_rank": 1}, "missing PalRUP confirmation marker 1"),
            ({"skip_unsat": True}, "missing PalRUP UNSAT marker"),
        )
        for options, diagnostic in cases:
            with self.subTest(options=options):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    formula, proof = _proof_fixture(root)
                    checker, _tools = _checker(root, **options)
                    with self.assertRaisesRegex(ProofWireError, diagnostic):
                        checker.verify(formula, proof, 3)

    def test_input_bounds_symlinks_and_tool_replacement_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            formula, proof = _proof_fixture(root)
            checker, tools = _checker(root)
            with self.assertRaisesRegex(ProofWireError, "aggregate byte bound"):
                checker.verify(
                    formula,
                    proof,
                    3,
                    max_total_fragment_bytes=1,
                )

            formula_link = root / "formula-link"
            formula_link.symlink_to(formula)
            with self.assertRaisesRegex(ProofWireError, "cannot open PalRUP formula"):
                checker.verify(formula_link, proof, 3)

            tools["local"].write_text(
                tools["local"].read_text(encoding="ascii") + "# changed\n",
                encoding="ascii",
            )
            with self.assertRaisesRegex(ProofWireError, "identity changed"):
                checker.verify(formula, proof, 3)

    def test_process_output_budget_is_enforced_during_parallel_phase(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            formula, proof = _proof_fixture(root)
            checker, _tools = _checker(root, flood=True)
            with self.assertRaisesRegex(
                ProofWireError, "local-check task 0 did not complete"
            ):
                checker.verify(formula, proof, 3)

    def test_process_failures_extra_markers_and_empty_formula_fail_closed(self):
        cases = (
            ({"fail_local": True}, "local-check task 0 failed"),
            ({"diagnostic_local": True}, "local-check task 0 failed"),
            (
                {"delay_local_ms": 250, "timeout_ms": 30},
                "local-check task 0 did not complete",
            ),
            ({"extra_confirm": True}, "marker set is not exact"),
            ({"hash_bytes": 15}, "fragment hash 0 has invalid byte size"),
        )
        for options, diagnostic in cases:
            with self.subTest(options=options):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    formula, proof = _proof_fixture(root)
                    checker, _tools = _checker(root, **options)
                    with self.assertRaisesRegex(ProofWireError, diagnostic):
                        checker.verify(formula, proof, 3)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            formula, proof = _proof_fixture(root)
            formula.write_bytes(b"")
            checker, _tools = _checker(root)
            with self.assertRaisesRegex(ProofWireError, "formula is empty"):
                checker.verify(formula, proof, 3)


if __name__ == "__main__":
    unittest.main()
