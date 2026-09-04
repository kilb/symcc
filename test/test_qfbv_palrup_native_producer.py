#!/usr/bin/env python3
# RUN: python3 %s

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from qfbv_palrup_native_producer import (  # noqa: E402
    NATIVE_PALRUP_RECEIPT_SCHEMA,
    NATIVE_PALRUP_PROTOCOL,
    NativePalrupProducer,
)
from qfbv_palrup_pipeline import PalrupGlobalChecker  # noqa: E402
from qfbv_proof_wire import ProofWireError  # noqa: E402


NATIVE_LIBRARY = Path(
    "/home/ubuntu/.local/opt/cadical-palrup-sat2026/lib/"
    "libsymcc_qfbv_palrup_pool.so"
)
PALRUP_BIN = Path("/home/ubuntu/.local/opt/palrup-check-sat2026/bin")
NATIVE_HELPER = ROOT / "util" / "qfbv_palrup_native_pool_worker.py"


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _write_executable(root: Path, name: str, content: str) -> tuple[Path, str]:
    path = root / name
    path.write_text(content, encoding="ascii")
    path.chmod(0o755)
    return path, _digest(path.read_bytes())


def _checker_tool(root: Path, name: str, stage: str) -> tuple[Path, str]:
    content = f"""#!/usr/bin/env python3
import hashlib
import math
from pathlib import Path
import sys

STAGE = {stage!r}
options = {{}}
for raw in sys.argv[1:]:
    key, value = raw.split('=', 1)
    options[key] = value
rank = int(options['-pal-id'])
solvers = int(options['-num-solvers'])
width = math.isqrt(solvers)
if width * width < solvers:
    width += 1
working = Path(options['-working-path'])
worker = working / str(rank // width) / str(rank)
if STAGE == 'local':
    fragment = (Path(options['-palrup-path']) / str(rank // width) /
                str(rank) / 'out.palrup')
    payload = fragment.read_bytes()
    fragment.with_name('out.palrup.hash').write_bytes(
        hashlib.sha256(payload).digest()[:16]
    )
    worker.joinpath('out.palrup_proxy').write_bytes(
        b'P' + rank.to_bytes(7, 'little') + hashlib.sha256(payload).digest()[:16]
    )
    if rank == 0:
        (working / '.unsat_found' / '0').mkdir(parents=True)
elif STAGE == 'redistribute':
    worker.joinpath('out.palrup_import').write_bytes(
        b'I' + rank.to_bytes(7, 'little') + bytes(16)
    )
elif STAGE == 'confirm':
    worker.joinpath('.check_ok').mkdir()
else:
    raise SystemExit(19)
print(STAGE, rank)
"""
    return _write_executable(root, name, content)


def _checker(root: Path) -> PalrupGlobalChecker:
    local, local_hash = _checker_tool(root, "palrup_local_check", "local")
    redist, redist_hash = _checker_tool(root, "palrup_redistribute", "redistribute")
    confirm, confirm_hash = _checker_tool(root, "palrup_confirm", "confirm")
    return PalrupGlobalChecker(
        local,
        redist,
        confirm,
        local_checker_sha256=local_hash,
        redistribute_sha256=redist_hash,
        confirm_sha256=confirm_hash,
        timeout_ms=10_000,
        max_parallel=4,
        read_buffer_kib=4,
        write_buffer_kib=4,
        merge_buffer_kib=4,
        queue_kib=4,
    )


def _fake_helper(root: Path, mode: str = "unsat") -> tuple[Path, str]:
    content = f"""#!/usr/bin/env python3
import argparse
import json
import os
from pathlib import Path
import signal
import sys

MODE = {mode!r}
parser = argparse.ArgumentParser()
for name in ('library', 'formula'):
    parser.add_argument('--' + name, required=True)
parser.add_argument('--output', action='append', required=True)
for name in ('solver-count', 'original-clause-count', 'skipped-epochs',
             'timeout-ms', 'maximum-shared-clause-length',
             'queue-capacity-clauses'):
    parser.add_argument('--' + name, required=True, type=int)
args = parser.parse_args()
solvers = args.solver_count
clauses = args.original_clause_count

if MODE == 'sigsegv':
    os.kill(os.getpid(), signal.SIGSEGV)

def varint(value):
    value *= 2
    result = bytearray()
    while value & ~0x7f:
        result.append((value & 0x7f) | 0x80)
        value >>= 7
    result.append(value)
    return bytes(result)

workers = []
status = 10 if MODE == 'sat' else 20
for rank, output in enumerate(args.output):
    produced = clauses + 1
    while produced % solvers != rank:
        produced += 1
    if MODE == 'wrong-rank':
        produced += 1
    Path(output).write_bytes(b'a' + varint(produced) + b'\\0\\0')
    workers.append({{
        'rank': rank,
        'native_status': status,
        'statistics': {{
            'variables': 2 if MODE == 'wrong-variable' else 1,
            'original_clauses': clauses, 'conflicts': 1,
            'decisions': 0, 'propagations': 1, 'restarts': 0,
            'imported': 0, 'discarded': 0, 'exported': 0,
            'delivered': 0, 'dropped': 0,
            'pending': args.queue_capacity_clauses + 1
            if MODE == 'wrong-pending' else 0,
            'elapsed_us': 1,
        }},
    }})
if MODE == 'crash':
    raise SystemExit(17)
payload = {{
    'schema': 'symcc-qfbv-native-palrup-pool-result-v1',
    'protocol': 'symcc-qfbv-native-palrup-clause-sharing-pool-v1',
    'source_commit': 'be7a0f84190b3216c589696b2010e8cbf8a8252e',
    'native_status': status,
    'solver_count': solvers,
    'skipped_epochs': args.skipped_epochs,
    'maximum_shared_clause_length': args.maximum_shared_clause_length,
    'queue_capacity_clauses': args.queue_capacity_clauses,
    'workers': workers,
}}
if MODE == 'bool-count':
    payload['solver_count'] = True
print(json.dumps(payload, ensure_ascii=True, separators=(',', ':'), sort_keys=True))
"""
    return _write_executable(root, f"worker-{mode}.py", content)


def _formula(root: Path) -> Path:
    path = root / "input.cnf"
    path.write_bytes(b"c fixture\np cnf 1 2\n1 0\n-1 0\n")
    return path


def _pigeonhole_formula(root: Path, pigeons: int = 9, holes: int = 8) -> Path:
    def variable(pigeon: int, hole: int) -> int:
        return pigeon * holes + hole + 1

    clauses: list[tuple[int, ...]] = []
    for pigeon in range(pigeons):
        clauses.append(tuple(variable(pigeon, hole) for hole in range(holes)))
        for first in range(holes):
            for second in range(first + 1, holes):
                clauses.append(
                    (-variable(pigeon, first), -variable(pigeon, second))
                )
    for hole in range(holes):
        for first in range(pigeons):
            for second in range(first + 1, pigeons):
                clauses.append((-variable(first, hole), -variable(second, hole)))
    path = root / "pigeonhole.cnf"
    lines = [f"p cnf {pigeons * holes} {len(clauses)}"]
    lines.extend(" ".join(map(str, clause)) + " 0" for clause in clauses)
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
    return path


def _producer(root: Path, mode: str = "unsat") -> NativePalrupProducer:
    library = root / "producer.so"
    library.write_bytes(b"fake-static-native-library")
    helper, helper_hash = _fake_helper(root, mode)
    return NativePalrupProducer(
        library,
        helper,
        library_sha256=_digest(library.read_bytes()),
        helper_sha256=helper_hash,
        timeout_ms=2_000,
        max_parallel=4,
    )


class NativePalrupProducerTest(unittest.TestCase):
    def test_complete_bundle_is_atomically_published_and_rechecked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            producer = _producer(root)
            checker = _checker(root)
            target = root / "published"
            with mock.patch.object(
                checker,
                "validate_receipt",
                wraps=checker.validate_receipt,
            ) as staging_recheck:
                receipt = producer.produce(
                    _formula(root), target, 3, checker=checker
                )
            self.assertEqual(staging_recheck.call_count, 1)
            self.assertEqual(receipt["schema"], NATIVE_PALRUP_RECEIPT_SCHEMA)
            self.assertEqual(receipt["protocol"], NATIVE_PALRUP_PROTOCOL)
            self.assertEqual(receipt["status"], "solver-native-global-unsat-confirmed")
            self.assertFalse(receipt["clause_sharing_active"])
            self.assertEqual([item["rank"] for item in receipt["fragments"]], [0, 1, 2])
            self.assertTrue(all(item["empty_clauses"] == 1 for item in receipt["fragments"]))
            self.assertEqual(list(target.rglob(".out.palrup.native")), [])
            self.assertEqual(list(root.glob(".symcc-palrup-native-*")), [])
            validated = producer.validate_receipt(
                receipt, target, checker=checker, recheck=True
            )
            self.assertEqual(validated, receipt)

    def test_sat_crash_and_wrong_namespace_never_publish(self):
        for mode, message in (
            ("sat", "did not prove UNSAT"),
            ("crash", "failed closed"),
            ("sigsegv", "failed closed"),
            ("wrong-rank", "ID namespace"),
        ):
            with self.subTest(mode=mode):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    producer = _producer(root, mode)
                    checker = _checker(root)
                    target = root / "published"
                    with self.assertRaisesRegex(ProofWireError, message):
                        producer.produce(_formula(root), target, 3, checker=checker)
                    self.assertFalse(target.exists())
                    self.assertEqual(list(root.glob(".symcc-palrup-native-*")), [])

    def test_malformed_pool_identity_and_formula_statistics_fail_closed(self):
        for mode, message in (
            ("bool-count", "outside its integer bound"),
            ("wrong-variable", "variable count differs"),
            ("wrong-pending", "pending queue exceeds capacity"),
        ):
            with self.subTest(mode=mode):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    producer = _producer(root, mode)
                    target = root / "published"
                    with self.assertRaisesRegex(ProofWireError, message):
                        producer.produce(
                            _formula(root), target, 3, checker=_checker(root)
                        )
                    self.assertFalse(target.exists())

    def test_native_rank_count_is_bounded_by_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "producer.so"
            library.write_bytes(b"fake-static-native-library")
            helper, helper_hash = _fake_helper(root)
            producer = NativePalrupProducer(
                library,
                helper,
                library_sha256=_digest(library.read_bytes()),
                helper_sha256=helper_hash,
                timeout_ms=2_000,
                max_parallel=2,
            )
            with self.assertRaisesRegex(ProofWireError, "solver count"):
                producer.produce(
                    _formula(root), root / "published", 3, checker=_checker(root)
                )
            self.assertFalse((root / "published").exists())

    def test_tool_replacement_target_collision_and_symlink_formula_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            producer = _producer(root)
            checker = _checker(root)
            producer.helper_path.write_text("changed", encoding="ascii")
            with self.assertRaisesRegex(ProofWireError, "identity changed"):
                producer.produce(_formula(root), root / "changed", 2, checker=checker)
            self.assertFalse((root / "changed").exists())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            producer = _producer(root)
            checker = _checker(root)
            target = root / "published"
            target.mkdir()
            marker = target / "owner"
            marker.write_text("preserve", encoding="ascii")
            with self.assertRaisesRegex(ProofWireError, "already exists"):
                producer.produce(_formula(root), target, 2, checker=checker)
            self.assertEqual(marker.read_text(encoding="ascii"), "preserve")

            formula = _formula(root)
            alias = root / "formula-link"
            alias.symlink_to(formula)
            with self.assertRaisesRegex(ProofWireError, "cannot open native PalRUP formula"):
                producer.produce(alias, root / "symlink", 2, checker=checker)
            self.assertFalse((root / "symlink").exists())

    def test_persisted_receipt_and_fragment_tampering_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            producer = _producer(root)
            checker = _checker(root)
            target = root / "published"
            receipt = producer.produce(_formula(root), target, 2, checker=checker)
            receipt_path = target / "native-palrup-receipt.json"
            original_receipt = receipt_path.read_bytes()
            receipt_path.write_bytes(original_receipt + b" ")
            with self.assertRaisesRegex(ProofWireError, "persisted receipt changed"):
                producer.validate_receipt(receipt, target, checker=checker, recheck=False)
            receipt_path.write_bytes(original_receipt)
            fragment = target / "0" / "0" / "out.palrup"
            fragment.write_bytes(fragment.read_bytes() + b"d\\x02\\0")
            with self.assertRaises(ProofWireError):
                producer.validate_receipt(receipt, target, checker=checker, recheck=False)

    def test_resealed_worker_variable_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            producer = _producer(root)
            checker = _checker(root)
            target = root / "published"
            receipt = producer.produce(_formula(root), target, 2, checker=checker)
            tampered = json.loads(json.dumps(receipt))
            tampered["workers"][0]["statistics"]["variables"] = 2
            tampered.pop("receipt_sha256")
            canonical = json.dumps(
                tampered,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
            tampered["receipt_sha256"] = _digest(canonical)
            persisted = json.dumps(
                tampered,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii") + b"\n"
            target.joinpath("native-palrup-receipt.json").write_bytes(persisted)
            with self.assertRaisesRegex(ProofWireError, "variable count changed"):
                producer.validate_receipt(
                    tampered, target, checker=checker, recheck=False
                )

            tampered = json.loads(json.dumps(receipt))
            tampered["workers"][0]["statistics"]["pending"] = 65_537
            tampered.pop("receipt_sha256")
            canonical = json.dumps(
                tampered,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
            tampered["receipt_sha256"] = _digest(canonical)
            target.joinpath("native-palrup-receipt.json").write_text(
                json.dumps(
                    tampered,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n",
                encoding="ascii",
            )
            with self.assertRaisesRegex(ProofWireError, "exceeds capacity"):
                producer.validate_receipt(
                    tampered, target, checker=checker, recheck=False
                )

    def test_unexpected_bundle_artifact_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            producer = _producer(root)
            checker = _checker(root)
            target = root / "published"
            receipt = producer.produce(_formula(root), target, 2, checker=checker)
            target.joinpath("unattested-artifact").write_bytes(b"unexpected")
            with self.assertRaisesRegex(ProofWireError, "root layout changed"):
                producer.validate_receipt(
                    receipt, target, checker=checker, recheck=False
                )

    def test_resealed_formula_metadata_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            producer = _producer(root)
            checker = _checker(root)
            target = root / "published"
            receipt = producer.produce(_formula(root), target, 2, checker=checker)
            tampered = json.loads(json.dumps(receipt))
            tampered["formula"]["variables"] = 2
            for worker in tampered["workers"]:
                worker["statistics"]["variables"] = 2
            tampered.pop("receipt_sha256")
            canonical = json.dumps(
                tampered,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
            tampered["receipt_sha256"] = _digest(canonical)
            target.joinpath("native-palrup-receipt.json").write_text(
                json.dumps(
                    tampered,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n",
                encoding="ascii",
            )
            with self.assertRaisesRegex(ProofWireError, "formula metadata changed"):
                producer.validate_receipt(
                    tampered, target, checker=checker, recheck=False
                )

    def test_checker_object_is_mandatory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            producer = _producer(root)
            with self.assertRaisesRegex(ProofWireError, "requires PalrupGlobalChecker"):
                producer.produce(
                    _formula(root), root / "published", 1, checker=object()  # type: ignore[arg-type]
                )


@unittest.skipUnless(
    NATIVE_LIBRARY.is_file()
    and NATIVE_HELPER.is_file()
    and all((PALRUP_BIN / name).is_file() for name in (
        "palrup_local_check", "palrup_redistribute", "palrup_confirm"
    )),
    "pinned native PalRUP producer and checker are not installed",
)
class NativePalrupInstalledIntegrationTest(unittest.TestCase):
    def test_pinned_native_workers_pass_the_official_global_checker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = [
                PALRUP_BIN / "palrup_local_check",
                PALRUP_BIN / "palrup_redistribute",
                PALRUP_BIN / "palrup_confirm",
            ]
            checker = PalrupGlobalChecker(
                *tools,
                local_checker_sha256=_digest(tools[0].read_bytes()),
                redistribute_sha256=_digest(tools[1].read_bytes()),
                confirm_sha256=_digest(tools[2].read_bytes()),
                timeout_ms=30_000,
                max_parallel=4,
                read_buffer_kib=1024,
                write_buffer_kib=1024,
                merge_buffer_kib=1024,
                queue_kib=16 * 1024,
            )
            producer = NativePalrupProducer(
                NATIVE_LIBRARY,
                NATIVE_HELPER,
                library_sha256=_digest(NATIVE_LIBRARY.read_bytes()),
                helper_sha256=_digest(NATIVE_HELPER.read_bytes()),
                timeout_ms=10_000,
                max_parallel=4,
            )
            target = root / "proof"
            receipt = producer.produce(
                _pigeonhole_formula(root), target, 4, checker=checker
            )
            self.assertEqual(receipt["official_checker"]["status"], "global-unsat-confirmed")
            self.assertEqual(receipt["official_checker"]["confirmed_ranks"], [0, 1, 2, 3])
            self.assertTrue(receipt["clause_sharing_active"])
            self.assertGreater(
                sum(item["imported"] for item in receipt["fragments"]), 0
            )
            producer.validate_receipt(receipt, target, checker=checker, recheck=True)

    def test_bounded_clause_queue_drops_without_invalidating_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = [
                PALRUP_BIN / "palrup_local_check",
                PALRUP_BIN / "palrup_redistribute",
                PALRUP_BIN / "palrup_confirm",
            ]
            checker = PalrupGlobalChecker(
                *tools,
                local_checker_sha256=_digest(tools[0].read_bytes()),
                redistribute_sha256=_digest(tools[1].read_bytes()),
                confirm_sha256=_digest(tools[2].read_bytes()),
                timeout_ms=30_000,
                max_parallel=4,
                read_buffer_kib=1024,
                write_buffer_kib=1024,
                merge_buffer_kib=1024,
                queue_kib=16 * 1024,
            )
            producer = NativePalrupProducer(
                NATIVE_LIBRARY,
                NATIVE_HELPER,
                library_sha256=_digest(NATIVE_LIBRARY.read_bytes()),
                helper_sha256=_digest(NATIVE_HELPER.read_bytes()),
                timeout_ms=10_000,
                max_parallel=4,
                queue_capacity_clauses=1,
            )
            target = root / "proof"
            receipt = producer.produce(
                _pigeonhole_formula(root), target, 4, checker=checker
            )
            statistics = [item["statistics"] for item in receipt["workers"]]
            self.assertGreater(sum(item["dropped"] for item in statistics), 0)
            self.assertTrue(receipt["clause_sharing_active"])
            self.assertEqual(
                receipt["official_checker"]["status"], "global-unsat-confirmed"
            )
            producer.validate_receipt(
                receipt, target, checker=checker, recheck=False
            )

    def test_isolated_pool_helper_rejects_invalid_output_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            formula = _formula(root)
            command = [
                sys.executable,
                "-I",
                str(NATIVE_HELPER),
                "--library",
                str(NATIVE_LIBRARY),
                "--formula",
                str(formula),
                "--output",
                str(root / "only-one-output"),
                "--solver-count",
                "2",
                "--original-clause-count",
                "2",
                "--skipped-epochs",
                "0",
                "--timeout-ms",
                "1000",
                "--maximum-shared-clause-length",
                "32",
                "--queue-capacity-clauses",
                "64",
            ]
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 70)
            self.assertEqual(result.stderr, b"")
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "worker-error")
            self.assertFalse((root / "only-one-output").exists())


if __name__ == "__main__":
    unittest.main()
