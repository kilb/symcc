#!/usr/bin/env python3
# RUN: env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -p no:cacheprovider %s

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from qfbv_artifact_lifecycle import (  # noqa: E402
    ArtifactLifecycleError,
    ArtifactLifecycleRegistry,
)
from qfbv_incremental_proof import (  # noqa: E402
    IncrementalProofChecker,
    IncrementalProofStore,
    make_rup_clause_record,
)
from qfbv_incremental_sat import bitblast_qfbv_query  # noqa: E402
from qfbv_malleable_workers import (  # noqa: E402
    MalleableJobSignal,
    MalleableWorkerPolicy,
    recommend_job_slots,
)
from qfbv_proof_prefix_partition import (  # noqa: E402
    PARTITION_PROTOCOL,
    ProofPrefixPartitionError,
    ProofPrefixPartitionPolicy,
    ProofPrefixPartitionStore,
    build_proof_prefix_partition,
    partition_job_catalog,
    verify_proof_prefix_partition,
)
from qfbv_realtime_stream import (  # noqa: E402
    make_checked_import_ack,
    make_clause_activity_receipt,
)
from query_store import QueryStore  # noqa: E402
from symcc_qfbv_partition import (  # noqa: E402
    _load_bounded_json,
    main as partition_cli_main,
)
from symcc_query_service import main as query_service_main  # noqa: E402


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def _reseal(value: dict) -> None:
    body = {key: item for key, item in value.items() if key != "partition_sha256"}
    value["partition_sha256"] = hashlib.sha256(_canonical_json(body)).hexdigest()


def _plan(query_id: str = "f448-input-zero"):
    return bitblast_qfbv_query(
        query_id,
        ["root"],
        {
            "input": {
                "op": "read",
                "bits": 8,
                "children": [],
                "attrs": {"index": 0},
            },
            "zero": {
                "op": "constant",
                "bits": 8,
                "children": [],
                "attrs": {"value_hex": "00"},
            },
            "root": {
                "op": "equal",
                "bits": 1,
                "children": ["input", "zero"],
                "attrs": {},
            },
        },
    )


def _activity_evidence(
    plan,
    root: Path,
    levels=(0, 7, 15),
    *,
    lifecycle=None,
    lifecycle_lease=None,
):
    proof_store = IncrementalProofStore(
        root / "proofs",
        lifecycle=lifecycle,
        lifecycle_lease=lifecycle_lease,
    )
    checker = IncrementalProofChecker(proof_store)
    activation = plan.assumptions[0]
    input_literals = plan.input_literals[0][1]
    evidence = []
    for sequence, (literal, level) in enumerate(zip(input_literals, levels), 1):
        shared = (-activation, -literal)
        record = make_rup_clause_record(
            plan,
            shared,
            dependency_assumptions=[activation],
            source_worker=f"source-{sequence}",
            worker_epoch=1,
            sequence=sequence,
        )
        digest, _created = proof_store.publish(record)
        authorization = checker.verify_clause_record(plan, proof_store.load(digest))
        ack = make_checked_import_ack(
            plan,
            authorization,
            stream_id=hashlib.sha256(b"f448-stream").hexdigest(),
            token=sequence,
            event_sequence=sequence,
            solve_generation=1,
            delivery_ordinal=sequence,
            authorized_monotonic_ns=sequence,
            native_signature="symcc-qfbv-realtime-v1|cadical-3.0.1-test",
            checker_policy_sha256=checker.policy_sha256,
        )
        receipt = make_clause_activity_receipt(
            plan,
            authorization,
            ack,
            token=sequence,
            solve_generation=1,
            activity_ordinal=sequence,
            decision_level=level,
            kind="unit",
            unit_literal=-literal,
            falsifying_assignments=[activation],
            native_signature="symcc-qfbv-realtime-v1|cadical-3.0.1-test",
        )
        evidence.append((receipt, ack))
    return checker, evidence


def _matches(cube: dict, assignment: dict[int, bool]) -> bool:
    return all(assignment[abs(literal)] == (literal > 0) for literal in cube["literals"])


def _query_envelope() -> dict:
    return {
        "schema": "symcc-query-ir-v1",
        "producer": "f448-cli-test",
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
                "attrs": {"value_hex": "00"},
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
                "op": "bool",
                "bits": 1,
                "children": [],
                "attrs": {"value": True},
            },
        ],
        "prefix_roots": [3],
        "target_root": 2,
        "input_hex": "00",
        "timeout_ms": 2000,
        "metadata": {"source": "f448-cli-test"},
        "smt2": "(assert true)\n",
        "prefix_smt2": "(assert true)\n",
        "target_smt2": "(assert true)\n",
    }


def test_policy_is_strict_and_content_addressed() -> None:
    policy = ProofPrefixPartitionPolicy(cube_count=5, max_depth=3)
    assert policy.as_dict()["protocol"] == PARTITION_PROTOCOL
    assert policy.sha256 == ProofPrefixPartitionPolicy.from_sealed(
        policy.as_dict()
    ).sha256
    with pytest.raises(ProofPrefixPartitionError):
        ProofPrefixPartitionPolicy(cube_count=True)
    with pytest.raises(ProofPrefixPartitionError):
        ProofPrefixPartitionPolicy(cube_count="5")
    with pytest.raises(ProofPrefixPartitionError):
        ProofPrefixPartitionPolicy(cube_count=5, max_depth=3.0)
    with pytest.raises(ProofPrefixPartitionError):
        ProofPrefixPartitionPolicy(cube_count=9, max_depth=3)
    changed = policy.as_dict()
    changed["unknown"] = 1
    with pytest.raises(ProofPrefixPartitionError):
        ProofPrefixPartitionPolicy.from_sealed(changed)


def test_digest_and_cli_json_types_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        store = ProofPrefixPartitionStore(root / "partitions")
        with pytest.raises(ProofPrefixPartitionError, match="lowercase SHA-256"):
            store.delete(int("1" * 64), 0)  # type: ignore[arg-type]

        activity = root / "activity.json"
        activity.write_text('{"metric":NaN}', encoding="ascii")
        with pytest.raises(ProofPrefixPartitionError, match="non-finite"):
            _load_bounded_json(activity)


def test_static_partition_is_deterministic_exhaustive_and_disjoint() -> None:
    plan = _plan()
    policy = ProofPrefixPartitionPolicy(cube_count=5, max_depth=3)
    first = build_proof_prefix_partition(plan, policy)
    second = build_proof_prefix_partition(plan, policy)
    assert first == second
    assert first["selection_source"] == "static-input-fallback"
    assert len(first["cubes"]) == 5
    variables = first["split_variables"]
    for values in itertools.product((False, True), repeat=len(variables)):
        assignment = dict(zip(variables, values))
        assert sum(_matches(cube, assignment) for cube in first["cubes"]) == 1
    assert verify_proof_prefix_partition(plan, first) == first


def test_checked_activity_controls_variable_order_without_soundness_trust() -> None:
    plan = _plan()
    with tempfile.TemporaryDirectory() as directory:
        checker, evidence = _activity_evidence(plan, Path(directory))
        certificate = build_proof_prefix_partition(
            plan,
            ProofPrefixPartitionPolicy(cube_count=8, max_depth=3),
            activity_evidence=evidence,
            checker=checker,
        )
        store = ProofPrefixPartitionStore(Path(directory) / "partitions")
        digest, created = store.publish(plan, certificate, checker=checker)
        assert created
        assert store.load(plan, digest, checker=checker) == certificate
        with pytest.raises(ProofPrefixPartitionError):
            store.load(plan, digest)
    assert certificate["selection_source"] == "checked-proof-activity"
    assert len(certificate["activity_receipts"]) == 3
    assert certificate["variable_ranking"][0]["score"] > certificate[
        "variable_ranking"
    ][1]["score"]
    assert certificate["split_variables"] == [
        row["variable"] for row in certificate["variable_ranking"]
    ]


def test_activity_can_be_combined_with_static_input_fallback() -> None:
    plan = _plan()
    with tempfile.TemporaryDirectory() as directory:
        checker, evidence = _activity_evidence(plan, Path(directory), levels=(0,))
        certificate = build_proof_prefix_partition(
            plan,
            ProofPrefixPartitionPolicy(cube_count=8, max_depth=3),
            activity_evidence=evidence,
            checker=checker,
        )
    assert certificate["selection_source"] == (
        "checked-proof-activity-plus-static-fallback"
    )
    assert certificate["split_variables"][0] == certificate[
        "variable_ranking"
    ][0]["variable"]


def test_activity_requires_checker_and_rejects_tampering_and_duplicates() -> None:
    plan = _plan()
    with tempfile.TemporaryDirectory() as directory:
        checker, evidence = _activity_evidence(plan, Path(directory), levels=(0,))
        policy = ProofPrefixPartitionPolicy(cube_count=2, max_depth=1)
        with pytest.raises(ProofPrefixPartitionError):
            build_proof_prefix_partition(
                plan, policy, activity_evidence=evidence, checker=None
            )
        altered = copy.deepcopy(evidence)
        altered[0][0]["decision_level"] = 1
        with pytest.raises(ProofPrefixPartitionError):
            build_proof_prefix_partition(
                plan, policy, activity_evidence=altered, checker=checker
            )
        with pytest.raises(ProofPrefixPartitionError):
            build_proof_prefix_partition(
                plan,
                policy,
                activity_evidence=[evidence[0], evidence[0]],
                checker=checker,
            )


def test_no_fallback_fails_when_proof_prefix_has_too_few_variables() -> None:
    plan = _plan()
    with tempfile.TemporaryDirectory() as directory:
        checker, evidence = _activity_evidence(plan, Path(directory), levels=(0,))
        with pytest.raises(ProofPrefixPartitionError):
            build_proof_prefix_partition(
                plan,
                ProofPrefixPartitionPolicy(
                    cube_count=4,
                    max_depth=2,
                    allow_static_fallback=False,
                ),
                activity_evidence=evidence,
                checker=checker,
            )


@pytest.mark.parametrize(
    "mutation",
    [
        "identity",
        "missing-child",
        "wrong-variable",
        "duplicate-leaf",
        "wrong-assumption",
        "wrong-load",
        "noninteger-load",
        "wrong-policy",
    ],
)
def test_certificate_tampering_fails_closed(mutation: str) -> None:
    plan = _plan()
    certificate = build_proof_prefix_partition(
        plan, ProofPrefixPartitionPolicy(cube_count=4, max_depth=2)
    )
    altered = copy.deepcopy(certificate)
    if mutation == "identity":
        altered["partition_sha256"] = "0" * 64
    elif mutation == "missing-child":
        altered["split_steps"][0]["negative_child"] = []
        _reseal(altered)
    elif mutation == "wrong-variable":
        altered["split_steps"][0]["split_variable"] += 1
        _reseal(altered)
    elif mutation == "duplicate-leaf":
        altered["cubes"][1] = copy.deepcopy(altered["cubes"][0])
        altered["cubes"][1]["ordinal"] = 1
        _reseal(altered)
    elif mutation == "wrong-assumption":
        altered["cubes"][0]["assumptions"][-1] *= -1
        _reseal(altered)
    elif mutation == "wrong-load":
        altered["cubes"][0]["estimated_load_denominator"] *= 2
        _reseal(altered)
    elif mutation == "noninteger-load":
        altered["cubes"][0]["estimated_load_numerator"] = "1"
        _reseal(altered)
    elif mutation == "wrong-policy":
        altered["policy"]["cube_count"] = 3
        _reseal(altered)
    with pytest.raises(ProofPrefixPartitionError):
        verify_proof_prefix_partition(plan, altered)


def test_certificate_is_bound_to_the_exact_bitblast_plan() -> None:
    first = _plan("first")
    second = _plan("second")
    certificate = build_proof_prefix_partition(
        first, ProofPrefixPartitionPolicy(cube_count=2, max_depth=1)
    )
    with pytest.raises(ProofPrefixPartitionError):
        verify_proof_prefix_partition(second, certificate)


def test_store_publish_load_and_concurrent_convergence() -> None:
    plan = _plan()
    certificate = build_proof_prefix_partition(
        plan, ProofPrefixPartitionPolicy(cube_count=8, max_depth=3)
    )
    with tempfile.TemporaryDirectory() as directory:
        store = ProofPrefixPartitionStore(Path(directory) / "partitions")
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(
                executor.map(
                    lambda _index: store.publish(plan, certificate), range(32)
                )
            )
        assert len({digest for digest, _created in results}) == 1
        assert sum(created for _digest_value, created in results) == 1
        digest = results[0][0]
        assert store.load(plan, digest) == certificate


def test_store_rejects_symlinked_shard_and_noncanonical_object() -> None:
    plan = _plan()
    certificate = build_proof_prefix_partition(
        plan, ProofPrefixPartitionPolicy(cube_count=2, max_depth=1)
    )
    digest = certificate["partition_sha256"]
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "partitions"
        store = ProofPrefixPartitionStore(root)
        target = Path(directory) / "target"
        target.mkdir()
        (root / "objects" / digest[:2]).symlink_to(target, target_is_directory=True)
        with pytest.raises(ProofPrefixPartitionError):
            store.publish(plan, certificate)


def test_store_inventory_ignores_protocol_temporary_files_only() -> None:
    plan = _plan()
    certificate = build_proof_prefix_partition(
        plan, ProofPrefixPartitionPolicy(cube_count=2, max_depth=1)
    )
    with tempfile.TemporaryDirectory() as directory:
        store = ProofPrefixPartitionStore(Path(directory) / "partitions")
        digest, _created = store.publish(plan, certificate)
        shard = store.objects / digest[:2]
        temporary = shard / f".{digest[2:]}.json.123.{'a' * 16}.tmp"
        temporary.write_bytes(b"unfinished")
        assert store.stats()["partitions"] == 1

        (shard / "foreign.tmp").write_bytes(b"unexpected")
        with pytest.raises(ProofPrefixPartitionError, match="noncanonical object"):
            store.stats()


def test_managed_partition_store_fences_legacy_and_different_registries() -> None:
    plan = _plan()
    certificate = build_proof_prefix_partition(
        plan, ProofPrefixPartitionPolicy(cube_count=2, max_depth=1)
    )
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        store_root = root / "partitions"
        legacy = ProofPrefixPartitionStore(store_root)
        first = ArtifactLifecycleRegistry(root / "lifecycle-a")
        managed = ProofPrefixPartitionStore(store_root, lifecycle=first)
        managed.publish(plan, certificate)
        with pytest.raises(
            ProofPrefixPartitionError,
            match="requires its artifact lifecycle",
        ):
            legacy.publish(plan, certificate)
        with pytest.raises(
            ProofPrefixPartitionError,
            match="requires its artifact lifecycle",
        ):
            ProofPrefixPartitionStore(store_root)
        second = ArtifactLifecycleRegistry(root / "lifecycle-b")
        with pytest.raises(
            ProofPrefixPartitionError,
            match="metadata mismatch",
        ):
            ProofPrefixPartitionStore(store_root, lifecycle=second)


def test_lifecycle_lease_protects_then_collects_partition() -> None:
    plan = _plan()
    certificate = build_proof_prefix_partition(
        plan, ProofPrefixPartitionPolicy(cube_count=4, max_depth=2)
    )
    with tempfile.TemporaryDirectory() as directory:
        registry = ArtifactLifecycleRegistry(Path(directory) / "lifecycle")
        lease = registry.start_job(
            "f448-partition", "coordinator-a", lease_seconds=60.0
        )
        store = ProofPrefixPartitionStore(
            Path(directory) / "partitions",
            lifecycle=registry,
            lifecycle_lease=lease,
        )
        digest, created = store.publish(plan, certificate)
        assert created
        protected = registry.collect(
            lambda _kind, _digest_value, _size: pytest.fail(
                "active partition was collected"
            ),
            grace_seconds=0,
            max_objects=4,
            max_bytes=MAX_BYTES,
            time_budget_ms=1000,
            now=time.time() + 0.01,
        )
        assert protected.deleted == ()
        assert registry.release_job(lease, now=time.time() + 0.02)
        collected = registry.collect(
            lambda kind, value, size: (
                store.delete_lifecycle_artifact(kind, value, size)
                if kind == "partition" and value == digest
                else pytest.fail("unexpected lifecycle artifact")
            ),
            grace_seconds=0,
            max_objects=4,
            max_bytes=MAX_BYTES,
            time_budget_ms=1000,
            now=time.time() + 1.0,
        )
        assert [item.digest for item in collected.deleted] == [digest]
        with pytest.raises(FileNotFoundError):
            store.load(plan, digest)


def test_checked_partition_keeps_proof_dependencies_until_dependent_first_gc() -> None:
    plan = _plan()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        registry = ArtifactLifecycleRegistry(root / "lifecycle")
        lease = registry.start_job(
            "f448-checked-partition", "coordinator-a", lease_seconds=60.0
        )
        checker, evidence = _activity_evidence(
            plan,
            root,
            lifecycle=registry,
            lifecycle_lease=lease,
        )
        certificate = build_proof_prefix_partition(
            plan,
            ProofPrefixPartitionPolicy(cube_count=8, max_depth=3),
            activity_evidence=evidence,
            checker=checker,
        )
        partitions = ProofPrefixPartitionStore(
            root / "partitions",
            lifecycle=registry,
            lifecycle_lease=lease,
        )
        digest, _created = partitions.publish(
            plan, certificate, checker=checker
        )
        assert registry.stats()["artifacts"] == 4
        assert registry.stats()["edges"] == 3
        assert registry.release_job(lease, now=time.time() + 0.01)
        deleted = []

        def remove(kind, value, size):
            deleted.append((kind, value))
            if kind == "partition":
                return partitions.delete_lifecycle_artifact(kind, value, size)
            return checker.store.delete_lifecycle_artifact(kind, value, size)

        result = registry.collect(
            remove,
            grace_seconds=0,
            max_objects=8,
            max_bytes=MAX_BYTES,
            time_budget_ms=1000,
            now=time.time() + 1.0,
        )
        assert len(result.deleted) == 4
        assert deleted[0] == ("partition", digest)
        assert {kind for kind, _value in deleted[1:]} == {"sat-proof"}


def test_partition_lifecycle_inventory_rebuilds_exact_proof_edges() -> None:
    plan = _plan()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        checker, evidence = _activity_evidence(plan, root)
        certificate = build_proof_prefix_partition(
            plan,
            ProofPrefixPartitionPolicy(cube_count=8, max_depth=3),
            activity_evidence=evidence,
            checker=checker,
        )
        partitions = ProofPrefixPartitionStore(root / "partitions")
        digest, _created = partitions.publish(
            plan, certificate, checker=checker
        )
        registry = ArtifactLifecycleRegistry(root / "lifecycle")
        proof_store = IncrementalProofStore(
            root / "proofs", lifecycle=registry
        )
        restored = ProofPrefixPartitionStore(
            root / "partitions", lifecycle=registry
        )
        inventory = restored.synchronize_lifecycle(max_entries=8)
        assert inventory == {"indexed": 1, "total": 1, "complete": True}
        assert restored.stats()["partitions"] == 1
        assert registry.stats()["artifacts"] == 4
        assert registry.stats()["edges"] == 3
        assert restored.load(
            plan, digest, checker=IncrementalProofChecker(proof_store)
        ) == certificate


def test_partition_inventory_rejects_missing_proof_dependencies_atomically() -> None:
    plan = _plan()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        checker, evidence = _activity_evidence(plan, root)
        certificate = build_proof_prefix_partition(
            plan,
            ProofPrefixPartitionPolicy(cube_count=2, max_depth=1),
            activity_evidence=evidence[:1],
            checker=checker,
        )
        unmanaged = ProofPrefixPartitionStore(root / "partitions")
        unmanaged.publish(plan, certificate, checker=checker)

        registry = ArtifactLifecycleRegistry(root / "lifecycle")
        restored = ProofPrefixPartitionStore(
            root / "partitions", lifecycle=registry
        )
        with pytest.raises(ArtifactLifecycleError, match="unknown lifecycle artifact"):
            restored.synchronize_lifecycle(max_entries=8)
        assert registry.stats()["artifacts"] == 0
        assert registry.stats()["edges"] == 0


def test_query_service_gc_routes_partition_before_checked_proof_dependencies() -> None:
    plan = _plan()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        lifecycle_root = root / "lifecycle"
        proof_root = root / "proofs"
        partition_root = root / "partitions"
        registry = ArtifactLifecycleRegistry(lifecycle_root)
        lease = registry.start_job(
            "f448-service-gc", "producer", lease_seconds=60.0
        )
        checker, evidence = _activity_evidence(
            plan,
            root,
            lifecycle=registry,
            lifecycle_lease=lease,
        )
        certificate = build_proof_prefix_partition(
            plan,
            ProofPrefixPartitionPolicy(cube_count=8, max_depth=3),
            activity_evidence=evidence,
            checker=checker,
        )
        partitions = ProofPrefixPartitionStore(
            partition_root,
            lifecycle=registry,
            lifecycle_lease=lease,
        )
        partitions.publish(plan, certificate, checker=checker)
        assert registry.release_job(lease)
        output = StringIO()
        with redirect_stdout(output):
            status = query_service_main(
                [
                    "--store",
                    str(root / "queries"),
                    "--qfbv-incremental-proof-store",
                    str(proof_root),
                    "--qfbv-partition-store",
                    str(partition_root),
                    "--qfbv-artifact-lifecycle-store",
                    str(lifecycle_root),
                    "--qfbv-artifact-gc-only",
                    "--qfbv-artifact-gc-grace-seconds",
                    "0",
                    "--qfbv-artifact-gc-max-objects",
                    "8",
                    "--qfbv-artifact-gc-max-bytes",
                    str(MAX_BYTES),
                    "--qfbv-artifact-gc-time-ms",
                    "5000",
                ]
            )
        assert status == 0
        payload = json.loads(output.getvalue())
        assert payload["qfbv_artifact_gc"]["deleted_objects"] == 4
        assert payload["qfbv_artifact_gc"]["deleted"][0]["kind"] == "partition"
        assert payload["qfbv_artifact_gc_inventory"]["partition"] == {
            "indexed": 1,
            "total": 1,
            "complete": True,
        }
        assert registry.stats()["artifacts"] == 0


def test_partition_jobs_feed_malleable_allocation_with_slot_conservation() -> None:
    plan = _plan()
    certificate = build_proof_prefix_partition(
        plan, ProofPrefixPartitionPolicy(cube_count=4, max_depth=2)
    )
    catalog = partition_job_catalog(
        plan, certificate, backlog_per_cube=3
    )
    signals = [
        MalleableJobSignal(
            job_id=job,
            formula_family_sha256=scope[0],
            backlog=scope[1],
        )
        for job, scope in catalog.items()
    ]
    allocation = recommend_job_slots(
        MalleableWorkerPolicy(total_slots=8), signals
    )
    assert set(allocation) == set(catalog)
    assert sum(allocation.values()) == 8
    assert all(slots >= 1 for slots in allocation.values())


def test_production_cli_builds_and_independently_replays_query_store_partition() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        queries = QueryStore(root / "queries")
        query_id, _created = queries.ingest(_query_envelope())
        generated = root / "generated.json"
        assert partition_cli_main(
            [
                "--query-store",
                str(root / "queries"),
                "--query-id",
                query_id,
                "--proof-store",
                str(root / "proofs"),
                "--partition-store",
                str(root / "partitions"),
                "--cubes",
                "5",
                "--max-depth",
                "3",
                "--backlog-per-cube",
                "2",
                "--output",
                str(generated),
            ]
        ) == 0
        first = json.loads(generated.read_text(encoding="ascii"))
        assert first["mode"] == "generated"
        assert first["created"]
        assert first["selection_source"] == "static-input-fallback"
        assert first["cube_count"] == 5
        assert len(first["jobs"]) == 5
        assert {job["backlog"] for job in first["jobs"]} == {2}
        replayed = root / "replayed.json"
        assert partition_cli_main(
            [
                "--query-store",
                str(root / "queries"),
                "--query-id",
                query_id,
                "--proof-store",
                str(root / "proofs"),
                "--partition-store",
                str(root / "partitions"),
                "--verify-digest",
                first["partition_sha256"],
                "--backlog-per-cube",
                "2",
                "--output",
                str(replayed),
            ]
        ) == 0
        second = json.loads(replayed.read_text(encoding="ascii"))
        assert second["mode"] == "verified"
        assert second["partition_sha256"] == first["partition_sha256"]
        assert second["certificate"] == first["certificate"]
        assert second["jobs"] == first["jobs"]


def test_executable_oracle_covers_arbitrary_cubes_and_lifecycle() -> None:
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "oracle.json"
        completed = subprocess.run(
            [
                sys.executable,
                str(
                    ROOT
                    / "benchmark/check_qfbv_proof_prefix_partition_oracles.py"
                ),
                "--rounds",
                "1",
                "--cube-counts",
                "1,3,5,8,32",
                "--output",
                str(output),
            ],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        payload = json.loads(output.read_text(encoding="ascii"))
        assert payload["status"] == "pass"
        assert [case["cube_count"] for case in payload["cases"]] == [
            1,
            3,
            5,
            8,
            32,
        ]
        assert all(case["exhaustive"] for case in payload["cases"])
        assert all(case["pairwise_disjoint"] for case in payload["cases"])
        assert payload["concurrent_publications"] == {
            "attempts": 32,
            "distinct_identities": 1,
            "new_objects": 1,
        }
        assert payload["lifecycle"]["dependent_first"]


MAX_BYTES = 1 << 20
