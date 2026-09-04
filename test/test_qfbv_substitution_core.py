#!/usr/bin/env python3
# RUN: env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -p no:cacheprovider %s
"""Tests for proof-carrying UNSAT-core reuse modulo variable substitution."""

from __future__ import annotations

import hashlib
import json
import os
import fcntl
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from qfbv_substitution_core import (  # noqa: E402
    QfbvSubstitutionCoreExchange,
    QfbvSubstitutionCoreStore,
    SubstitutionCoreError,
    bloom_for_footprints,
    bloom_maybe_subset,
    clause_footprints,
    exact_substitution_match,
    structural_fingerprint,
    substituted_root_digests,
)
from qf_bv_backend import (  # noqa: E402
    PersistentSmtLibQfbvSolver,
    SmtLibQfbvSolver,
    lower_qfbv_proof_problem,
    normalize_qfbv_capabilities,
)
from qfbv_proof_receipt import QfbvProofStore, QfbvProofVerifier  # noqa: E402
from qfbv_artifact_lifecycle import (  # noqa: E402
    ArtifactLifecycleRegistry,
    ArtifactRef,
)
from query_store import QueryStore  # noqa: E402
from symcc_query_service import _load_portfolio  # noqa: E402


_PINNED_TOOL_ROOT = Path(
    os.environ.get(
        "SYMCC_TEST_CPC_TOOL_ROOT",
        str(Path.home() / ".local" / "share" / "symcc-cpc-1.3.4"),
    )
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()


class _Graph:
    def __init__(self) -> None:
        self.expressions: dict[str, dict] = {}

    def node(
        self,
        op: str,
        bits: int,
        children: tuple[str, ...] = (),
        attrs: dict | None = None,
    ) -> str:
        body = {
            "schema": "symcc-expr-node-v1",
            "op": op,
            "bits": bits,
            "children": list(children),
            "attrs": {} if attrs is None else attrs,
        }
        digest = _digest(body)
        self.expressions[digest] = body
        return digest

    def read(self, index: int) -> str:
        return self.node("read", 8, attrs={"index": index})

    def byte(self, value: int) -> str:
        return self.node("constant", 8, attrs={"value_hex": f"{value:02x}"})

    def predicate(self, op: str, left: str, right: str) -> str:
        return self.node(op, 1, (left, right))


def test_alpha_fingerprint_and_exact_renaming() -> None:
    source = _Graph()
    source_read = source.read(0)
    source_roots = (
        source.predicate("equal", source_read, source.byte(0x41)),
        source.predicate("equal", source_read, source.byte(0x42)),
    )
    target = _Graph()
    target_read = target.read(73)
    target_roots = (
        target.predicate("equal", target_read, target.byte(0x41)),
        target.predicate("equal", target_read, target.byte(0x42)),
    )

    assert clause_footprints(source_roots, source.expressions) == clause_footprints(
        target_roots, target.expressions
    )
    match = exact_substitution_match(
        source_roots,
        source.expressions,
        target_roots,
        target.expressions,
    )
    assert match is not None
    assert match.mapping == {0: 73}
    assert match.substituted_roots == target_roots


def test_constants_and_child_order_remain_structural() -> None:
    left = _Graph()
    read_left = left.read(0)
    left_root = left.predicate("ult", read_left, left.byte(9))
    different_constant = _Graph()
    read_right = different_constant.read(4)
    constant_root = different_constant.predicate(
        "ult", read_right, different_constant.byte(10)
    )
    reversed_children = _Graph()
    read_reversed = reversed_children.read(4)
    reversed_root = reversed_children.predicate(
        "ult", reversed_children.byte(9), read_reversed
    )

    assert structural_fingerprint(left_root, left.expressions) != structural_fingerprint(
        constant_root, different_constant.expressions
    )
    assert structural_fingerprint(left_root, left.expressions) != structural_fingerprint(
        reversed_root, reversed_children.expressions
    )
    assert (
        exact_substitution_match(
            (left_root,),
            left.expressions,
            (constant_root, reversed_root),
            {**different_constant.expressions, **reversed_children.expressions},
        )
        is None
    )


def test_repeated_source_read_requires_one_consistent_target() -> None:
    source = _Graph()
    read = source.read(1)
    pair = source.node("concat", 16, (read, read))
    source_root = source.predicate(
        "equal", pair, source.node("constant", 16, attrs={"value_hex": "4141"})
    )
    target = _Graph()
    target_pair = target.node("concat", 16, (target.read(8), target.read(9)))
    target_root = target.predicate(
        "equal", target_pair, target.node("constant", 16, attrs={"value_hex": "4141"})
    )

    assert clause_footprints((source_root,), source.expressions) == clause_footprints(
        (target_root,), target.expressions
    )
    assert (
        exact_substitution_match(
            (source_root,), source.expressions, (target_root,), target.expressions
        )
        is None
    )


def test_non_injective_mapping_and_clause_collapse_are_sound() -> None:
    source = _Graph()
    x = source.read(0)
    y = source.read(1)
    zero = source.byte(0)
    source_roots = (
        source.predicate("equal", x, zero),
        source.predicate("equal", y, zero),
        source.predicate("distinct", x, y),
    )
    target = _Graph()
    z = target.read(17)
    target_zero = target.byte(0)
    target_roots = (
        target.predicate("equal", z, target_zero),
        target.predicate("distinct", z, z),
    )

    match = exact_substitution_match(
        source_roots,
        source.expressions,
        target_roots,
        target.expressions,
    )
    assert match is not None
    assert match.mapping == {0: 17, 1: 17}
    assert set(match.substituted_roots) <= set(target_roots)
    assert len(match.substituted_roots) > len(set(match.substituted_roots))


def test_join_rejects_cross_clause_mapping_conflict() -> None:
    source = _Graph()
    read = source.read(0)
    source_roots = (
        source.predicate("equal", read, source.byte(1)),
        source.predicate("equal", read, source.byte(2)),
    )
    target = _Graph()
    target_roots = (
        target.predicate("equal", target.read(10), target.byte(1)),
        target.predicate("equal", target.read(11), target.byte(2)),
    )

    assert (
        exact_substitution_match(
            source_roots,
            source.expressions,
            target_roots,
            target.expressions,
        )
        is None
    )


def test_bloom_filter_is_only_a_subset_prefilter() -> None:
    source = _Graph()
    source_root = source.predicate("equal", source.read(0), source.byte(7))
    target = _Graph()
    target_root = target.predicate("equal", target.read(9), target.byte(8))
    source_footprint = clause_footprints((source_root,), source.expressions)
    target_footprint = clause_footprints((target_root,), target.expressions)

    assert not bloom_maybe_subset(
        bloom_for_footprints(source_footprint),
        bloom_for_footprints(target_footprint),
    )
    # Even an adversarial all-ones prefilter cannot make exact matching succeed.
    assert bloom_maybe_subset(
        bloom_for_footprints(source_footprint), "f" * 256
    )
    assert (
        exact_substitution_match(
            (source_root,),
            source.expressions,
            (target_root,),
            target.expressions,
        )
        is None
    )


def test_substituted_digest_requires_a_total_mapping() -> None:
    graph = _Graph()
    root = graph.predicate("equal", graph.read(4), graph.byte(1))
    with pytest.raises(SubstitutionCoreError, match="does not cover"):
        substituted_root_digests((root,), graph.expressions, {})


def test_tampered_content_addressed_node_fails_closed() -> None:
    graph = _Graph()
    root = graph.predicate("equal", graph.read(0), graph.byte(1))
    graph.expressions[root]["op"] = "distinct"
    with pytest.raises(SubstitutionCoreError, match="digest mismatch"):
        exact_substitution_match(
            (root,), graph.expressions, (root,), graph.expressions
        )


def test_join_budget_is_enforced() -> None:
    source = _Graph()
    source_root = source.predicate("equal", source.read(0), source.byte(1))
    target = _Graph()
    target_roots = tuple(
        target.predicate("equal", target.read(index), target.byte(1))
        for index in range(4)
    )
    with pytest.raises(SubstitutionCoreError, match="join budget"):
        exact_substitution_match(
            (source_root,),
            source.expressions,
            target_roots,
            target.expressions,
            max_join_states=1,
        )


def test_match_deadline_covers_graph_normalization_and_fingerprinting() -> None:
    source = _Graph()
    source_roots = tuple(
        source.predicate("equal", source.read(index), source.byte(index % 4))
        for index in range(4096)
    )
    with pytest.raises(TimeoutError, match="deadline"):
        exact_substitution_match(
            source_roots,
            source.expressions,
            source_roots,
            source.expressions,
            timeout_ms=1,
        )


def test_portfolio_configuration_requires_bounded_proof_carrying_core_policy() -> None:
    proof = {
        "generator_command": ["cvc5", "--dump-proofs", "{query}"],
        "checker_command": ["ethos", "{proof}"],
        "signature_root": "/opt/cpc",
    }
    configured = _load_portfolio(
        json.dumps(
            {
                "solvers": [
                    {
                        "name": "core-cache",
                        "kind": "smtlib-qfbv",
                        "persistent": False,
                        "command": ["cvc5", "{query}"],
                        "unsat_proof": proof,
                        "substitution_cores": {
                            "extractor_command": [
                                "cvc5",
                                "--produce-unsat-cores",
                                "{query}",
                            ],
                            "max_candidates": 17,
                            "max_join_states": 1234,
                            "max_unification_pairs": 5678,
                            "lookup_timeout_ms": 321,
                            "publish_timeout_ms": 654,
                        },
                    }
                ]
            }
        )
    )
    assert configured[0]["substitution_cores"] == {
        "extractor_command": [
            "cvc5",
            "--produce-unsat-cores",
            "{query}",
        ],
        "max_candidates": 17,
        "max_candidate_scan": 4096,
        "max_join_states": 1234,
        "max_unification_pairs": 5678,
        "verified_core_cache_entries": 1024,
        "lookup_timeout_ms": 321,
        "publish_timeout_ms": 654,
    }
    without_proof = {
        "solvers": [
            {
                "kind": "smtlib-qfbv",
                "command": ["cvc5", "{query}"],
                "substitution_cores": {"extractor_command": ["cvc5", "{query}"]},
            }
        ]
    }
    with pytest.raises(RuntimeError, match="requires an unsat_proof"):
        _load_portfolio(json.dumps(without_proof))
    invalid_budget = {
        "solvers": [
            {
                "kind": "smtlib-qfbv",
                "command": ["cvc5", "{query}"],
                "unsat_proof": proof,
                "substitution_cores": {
                    "extractor_command": ["cvc5", "{query}"],
                    "max_join_states": 0,
                },
            }
        ]
    }
    with pytest.raises(RuntimeError, match="bounds"):
        _load_portfolio(json.dumps(invalid_budget))


def _real_verifier(root: Path) -> QfbvProofVerifier:
    return QfbvProofVerifier(
        QfbvProofStore(root / "proofs"),
        generator_command=[
            str(_PINNED_TOOL_ROOT / "bin" / "cvc5"),
            "--lang=smt2",
            "--safe-mode=safe",
            "--proof-granularity=dsl-rewrite",
            "--dump-proofs",
            "{query}",
        ],
        checker_command=[
            str(_PINNED_TOOL_ROOT / "bin" / "ethos"),
            "{proof}",
        ],
        signature_root=_PINNED_TOOL_ROOT / "share" / "cpc",
    )


def _query_envelope(index: int) -> dict:
    return {
        "schema": "symcc-query-ir-v1",
        "producer": "substitution-core-test",
        "nodes": [
            {
                "id": 0,
                "op": "read",
                "bits": 8,
                "children": [],
                "attrs": {"index": index},
            },
            {
                "id": 1,
                "op": "constant",
                "bits": 8,
                "children": [],
                "attrs": {"value_hex": "41"},
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
                "attrs": {"value_hex": "42"},
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
        "input_hex": "00" * (index + 1),
        "timeout_ms": 30_000,
        "metadata": {"source": "substitution-core-test"},
        "smt2": "(assert true)\n",
        "prefix_smt2": "(assert true)\n",
        "target_smt2": "(assert true)\n",
    }


@pytest.mark.skipif(
    not (_PINNED_TOOL_ROOT / "bin" / "cvc5").is_file()
    or not (_PINNED_TOOL_ROOT / "bin" / "ethos").is_file(),
    reason="pinned cvc5 1.3.4 and Ethos are not installed",
)
def test_real_extractor_producer_and_fresh_proof_checking_consumer() -> None:
    source = _Graph()
    source_read = source.read(0)
    source_roots = (
        source.predicate("equal", source_read, source.byte(0x41)),
        source.predicate("equal", source_read, source.byte(0x42)),
    )
    target = _Graph()
    target_read = target.read(91)
    target_roots = (
        target.predicate("equal", target_read, target.byte(0x41)),
        target.predicate("equal", target_read, target.byte(0x42)),
    )
    capabilities = normalize_qfbv_capabilities(None)
    source_query_id = _digest(
        {"schema": "test-source-v1", "roots": list(source_roots)}
    )
    (
        _smt2,
        _proof_query,
        _reference,
        _certificate,
        offsets,
        root_terms,
        _context,
    ) = lower_qfbv_proof_problem(
        source_query_id,
        source_roots,
        source.expressions,
        capabilities,
    )
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        extractor = [
            str(_PINNED_TOOL_ROOT / "bin" / "cvc5"),
            "--lang=smt2",
            "--safe-mode=safe",
            "{query}",
        ]
        producer = QfbvSubstitutionCoreExchange(
            QfbvSubstitutionCoreStore(root / "cores"),
            _real_verifier(root),
            extractor,
        )
        record, created, extractor_us, proof_us = producer.publish_from_unsat(
            source_query_id=source_query_id,
            roots=source_roots,
            expressions=source.expressions,
            root_terms=root_terms,
            offsets=offsets,
            capabilities=capabilities,
        )
        assert created
        assert extractor_us > 0
        assert proof_us > 0
        assert record["source_clause_count"] == 2

        # Recreate every object. The consumer cannot inherit an in-memory proof
        # decision and must replay Ethos before accepting the exact substitution.
        consumer = QfbvSubstitutionCoreExchange(
            QfbvSubstitutionCoreStore(root / "cores"),
            _real_verifier(root),
            extractor,
        )
        authorization = consumer.lookup(
            target_roots,
            target.expressions,
            capabilities,
        )
        assert authorization is not None
        assert authorization.record["record_sha256"] == record["record_sha256"]
        assert authorization.match.mapping == {0: 91}
        assert authorization.checker_elapsed_us > 0
        assert not authorization.proof_reused
        cached_authorization = consumer.lookup(
            target_roots,
            target.expressions,
            capabilities,
        )
        assert cached_authorization is not None
        assert cached_authorization.proof_reused
        assert cached_authorization.checker_elapsed_us == 0

        concurrent_store = QfbvSubstitutionCoreStore(root / "concurrent-cores")
        with ThreadPoolExecutor(max_workers=8) as pool:
            publication_results = list(
                pool.map(lambda _index: concurrent_store.publish(record), range(16))
            )
        assert publication_results.count(True) == 1
        assert publication_results.count(False) == 15
        assert concurrent_store.stats()["records"] == 1
        with concurrent_store._connect() as database:
            database.execute(
                "UPDATE records SET last_used = 1.0 WHERE record_sha256 = ?",
                (record["record_sha256"],),
            )
            for index in range(10):
                digest = hashlib.sha256(f"bloom-miss-{index}".encode()).hexdigest()
                database.execute(
                    "INSERT INTO records(record_sha256, capability_sha256, "
                    "exchange_policy_sha256, source_clause_count, "
                    "source_bloom_hex, encoded_bytes, relative_path, created, "
                    "last_used) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        digest,
                        record["capability_sha256"],
                        record["exchange_policy_sha256"],
                        1,
                        "f" * 256,
                        1,
                        f"objects/{digest[:2]}/{digest}.json",
                        2.0 + index,
                        2.0 + index,
                    ),
                )
        candidate_ids, scanned = concurrent_store.candidate_digests(
            capability_sha256=record["capability_sha256"],
            exchange_policy_sha256=record["exchange_policy_sha256"],
            target_clause_count=2,
            target_bloom_hex=record["source_bloom_hex"],
            limit=1,
            scan_limit=16,
        )
        assert candidate_ids == [record["record_sha256"]]
        assert scanned == 11

        blocked_store = QfbvSubstitutionCoreStore(root / "blocked-cores")
        lock_descriptor = os.open(
            blocked_store.lock_path,
            os.O_RDWR | os.O_CREAT,
            0o600,
        )
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with pytest.raises(TimeoutError, match="publication lock timeout"):
                blocked_store.publish(record, lock_timeout_ms=10)
        finally:
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            os.close(lock_descriptor)

        quota_store = QfbvSubstitutionCoreStore(
            root / "quota-cores", max_bytes=4096
        )
        with pytest.raises(SubstitutionCoreError, match="quota"):
            quota_store.publish(record)

        shard_store = QfbvSubstitutionCoreStore(root / "symlink-shard-cores")
        outside_shard = root / "outside-shard"
        outside_shard.mkdir()
        (
            shard_store.object_dir / record["record_sha256"][:2]
        ).symlink_to(outside_shard, target_is_directory=True)
        with pytest.raises(SubstitutionCoreError, match="shard directory"):
            shard_store.publish(record)

        lifecycle = ArtifactLifecycleRegistry(root / "lifecycle")
        lease = lifecycle.start_job(
            "core-test", "producer", lease_seconds=60.0
        )
        proof_ref = ArtifactRef(
            "proof", record["proof_receipt"]["proof_sha256"]
        )
        receipt_ref = ArtifactRef("receipt", record["proof_receipt_sha256"])
        lifecycle.record_artifact(proof_ref, encoded_bytes=0, now=1.0)
        lifecycle.record_artifact(
            receipt_ref, encoded_bytes=0, edges=(proof_ref,), now=1.0
        )
        managed_store = QfbvSubstitutionCoreStore(
            root / "managed-cores",
            lifecycle=lifecycle,
            lifecycle_lease=lease,
        )
        assert managed_store.publish(record)
        assert managed_store.synchronize_lifecycle(max_entries=1)["complete"]
        live = lifecycle.collect(
            lambda _kind, _digest, size: size,
            grace_seconds=0.0,
            max_objects=3,
            max_bytes=1 << 20,
            time_budget_ms=1_000,
            now=time.time() + 1.0,
        )
        assert live.deleted == ()
        assert lifecycle.release_job(lease)
        deletion_order: list[str] = []

        def delete_managed(kind: str, digest: str, size: int) -> int:
            deletion_order.append(kind)
            if kind == "core":
                return managed_store.delete_lifecycle_artifact(kind, digest, size)
            return size

        dead = lifecycle.collect(
            delete_managed,
            grace_seconds=0.0,
            max_objects=3,
            max_bytes=1 << 20,
            time_budget_ms=1_000,
            now=time.time() + 2.0,
        )
        assert [artifact.kind for artifact in dead.deleted] == [
            "core",
            "receipt",
            "proof",
        ]
        assert deletion_order == ["core", "receipt", "proof"]
        unmanaged_reopen = QfbvSubstitutionCoreStore(root / "managed-cores")
        with pytest.raises(SubstitutionCoreError, match="requires its artifact lifecycle"):
            unmanaged_reopen.stats()

        record_path = consumer.store._record_path(record["record_sha256"])
        original = record_path.read_bytes()
        tampered = original.replace(b'"source_clause_count":2', b'"source_clause_count":1')
        assert tampered != original
        record_path.write_bytes(tampered)
        with pytest.raises(SubstitutionCoreError):
            consumer.store.load(record["record_sha256"])


def test_store_root_symlink_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / "actual").mkdir()
        (root / "alias").symlink_to(root / "actual", target_is_directory=True)
        with pytest.raises(SubstitutionCoreError, match="must not be a symlink"):
            QfbvSubstitutionCoreStore(root / "alias")


def test_extractor_timeout_output_bound_parser_and_cancellation() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)

        def exchange(command: list[str]) -> QfbvSubstitutionCoreExchange:
            return QfbvSubstitutionCoreExchange(
                QfbvSubstitutionCoreStore(root / hashlib.sha256(
                    repr(command).encode("utf-8")
                ).hexdigest()),
                SimpleNamespace(policy_sha256="0" * 64),
                command,
            )

        captured: list[object] = []
        cancelled = threading.Event()

        def register(process: object) -> tuple[int, threading.Event]:
            captured.append(process)
            return 1, cancelled

        started = time.monotonic()
        with pytest.raises(TimeoutError, match="extractor timeout"):
            exchange(
                [sys.executable, "-c", "import time; time.sleep(30)"]
            )._extract_indices(
                ("true",),
                (),
                timeout_ms=50,
                register_process=register,
                unregister_process=lambda _token: None,
            )
        assert time.monotonic() - started < 1.0
        assert captured and captured[-1].poll() is not None

        with pytest.raises(SubstitutionCoreError, match="output exceeds"):
            exchange(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.buffer.write(b'x' * (8*1024*1024+1))",
                ]
            )._extract_indices(
                ("true",),
                (),
                timeout_ms=2_000,
                register_process=None,
                unregister_process=None,
            )

        with pytest.raises(SubstitutionCoreError, match="invalid name"):
            exchange(
                [
                    sys.executable,
                    "-c",
                    "print('unsat\\n(symcc_core_9)')",
                ]
            )._extract_indices(
                ("true",),
                (),
                timeout_ms=2_000,
                register_process=None,
                unregister_process=None,
            )

        cancelled.set()
        with pytest.raises(SubstitutionCoreError, match="cancelled"):
            exchange(
                [
                    sys.executable,
                    "-c",
                    "print('unsat\\n(symcc_core_0)')",
                ]
            )._extract_indices(
                ("true",),
                (),
                timeout_ms=2_000,
                register_process=register,
                unregister_process=lambda _token: None,
            )


@pytest.mark.skipif(
    not (_PINNED_TOOL_ROOT / "bin" / "cvc5").is_file()
    or not (_PINNED_TOOL_ROOT / "bin" / "ethos").is_file(),
    reason="pinned cvc5 1.3.4 and Ethos are not installed",
)
def test_backend_and_query_store_repeat_core_authorization() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        store = QueryStore(root / "queries")
        core_store_root = root / "cores"
        extractor = [
            str(_PINNED_TOOL_ROOT / "bin" / "cvc5"),
            "--lang=smt2",
            "--safe-mode=safe",
            "{query}",
        ]
        solver = [
            str(_PINNED_TOOL_ROOT / "bin" / "cvc5"),
            "--lang=smt2",
            "--safe-mode=safe",
            "{query}",
        ]
        capabilities = normalize_qfbv_capabilities(
            {"incremental": True, "accept_unsat": False}
        )

        source_id, _ = store.ingest(_query_envelope(0))
        source_verifier = _real_verifier(root)
        source_exchange = QfbvSubstitutionCoreExchange(
            QfbvSubstitutionCoreStore(core_store_root),
            source_verifier,
            extractor,
        )
        store.register_qfbv_proof_verifier(source_verifier)
        store.register_qfbv_substitution_core_exchange(source_exchange)
        source_backend = SmtLibQfbvSolver(
            store,
            solver,
            name="cvc5-core-producer",
            capabilities=capabilities,
            proof_verifier=source_verifier,
            substitution_core_exchange=source_exchange,
        )
        source_lease = store.claim("source-worker")
        assert source_lease is not None and source_lease.query_id == source_id
        source_result = dict(source_backend(source_lease))
        assert source_result["status"] == "unsat"
        assert source_result["backend_substitution_core_published"]
        assert store.complete(source_lease, "source-worker", source_result)

        target_id, _ = store.ingest(_query_envelope(23))
        assert target_id != source_id
        # Recreate the verifier/exchange to exclude an in-memory authorization.
        target_verifier = _real_verifier(root)
        target_exchange = QfbvSubstitutionCoreExchange(
            QfbvSubstitutionCoreStore(core_store_root),
            target_verifier,
            extractor,
        )
        store.register_qfbv_proof_verifier(target_verifier)
        store.register_qfbv_substitution_core_exchange(target_exchange)
        target_backend = SmtLibQfbvSolver(
            store,
            ["/definitely/not/a/solver", "{query}"],
            name="must-not-run",
            capabilities=capabilities,
            proof_verifier=target_verifier,
            substitution_core_exchange=target_exchange,
        )
        target_lease = store.claim("target-worker")
        assert target_lease is not None and target_lease.query_id == target_id
        target_result = dict(target_backend(target_lease))
        assert target_result["status"] == "unsat"
        assert target_result["backend_substitution_core_hit"]
        assert not target_result["backend_substitution_core_proof_reused"]
        assert target_result["backend_substitution_core_mapping"] == [[0, 23]]
        assert store.complete(target_lease, "target-worker", target_result)
        stored = json.loads(
            (
                root
                / "queries"
                / "results"
                / target_id[:2]
                / f"{target_id}.json"
            ).read_text(encoding="ascii")
        )
        assert stored["store_substitution_core_verified"]
        assert stored["store_substitution_core_proof_reused"]
        assert stored["store_substitution_core_checker_elapsed_us"] == 0

        persistent_id, _ = store.ingest(_query_envelope(24))
        persistent_verifier = _real_verifier(root)
        persistent_exchange = QfbvSubstitutionCoreExchange(
            QfbvSubstitutionCoreStore(core_store_root),
            persistent_verifier,
            extractor,
        )
        store.register_qfbv_proof_verifier(persistent_verifier)
        store.register_qfbv_substitution_core_exchange(persistent_exchange)
        with PersistentSmtLibQfbvSolver(
            store,
            ["/definitely/not/a/persistent/solver"],
            name="persistent-must-not-start",
            capabilities=capabilities,
            proof_verifier=persistent_verifier,
            substitution_core_exchange=persistent_exchange,
        ) as persistent_backend:
            persistent_lease = store.claim("persistent-worker")
            assert (
                persistent_lease is not None
                and persistent_lease.query_id == persistent_id
            )
            persistent_result = dict(persistent_backend(persistent_lease))
            assert persistent_result["status"] == "unsat"
            assert persistent_result["backend_substitution_core_hit"]
            assert persistent_result["backend_substitution_core_mapping"] == [
                [0, 24]
            ]
            assert store.complete(
                persistent_lease, "persistent-worker", persistent_result
            )

        forged_id, _ = store.ingest(_query_envelope(25))
        forged_backend = SmtLibQfbvSolver(
            store,
            ["/definitely/not/a/solver", "{query}"],
            name="forged-mapping",
            capabilities=capabilities,
            proof_verifier=target_verifier,
            substitution_core_exchange=target_exchange,
        )
        forged_lease = store.claim("forged-worker")
        assert forged_lease is not None and forged_lease.query_id == forged_id
        forged_result = dict(forged_backend(forged_lease))
        assert forged_result["backend_substitution_core_hit"]
        forged_result["backend_substitution_core_mapping"] = [[0, 999]]
        with pytest.raises(ValueError, match="independent store verification"):
            store.complete(forged_lease, "forged-worker", forged_result)
