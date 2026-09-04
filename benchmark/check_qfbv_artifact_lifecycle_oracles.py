#!/usr/bin/env python3
"""Independent graph and live-store oracle for F429 artifact lifecycle GC."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from cross_worker_context import (  # noqa: E402
    CrossWorkerContextStore,
    build_context_manifests,
)
from qf_bv_backend import normalize_qfbv_capabilities  # noqa: E402
from qfbv_artifact_lifecycle import (  # noqa: E402
    ARTIFACT_KINDS,
    ArtifactLifecycleError,
    ArtifactLifecycleRegistry,
    ArtifactRef,
)
from qfbv_lemma_exchange import (  # noqa: E402
    QfbvLemmaExchange,
    QfbvLemmaStore,
    _canonical_json as _lemma_json,
)
from qfbv_proof_receipt import (  # noqa: E402
    QfbvProofStore,
    QfbvProofVerifier,
)


def _digest(value: str | bytes) -> str:
    encoded = value if isinstance(value, bytes) else value.encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _independent_reachable(
    roots: set[ArtifactRef],
    edges: dict[ArtifactRef, set[ArtifactRef]],
) -> set[ArtifactRef]:
    reachable: set[ArtifactRef] = set()
    pending = list(roots)
    while pending:
        current = pending.pop()
        if current in reachable:
            continue
        reachable.add(current)
        pending.extend(edges.get(current, ()))
    return reachable


def _finite_graph_oracle(graphs: int) -> dict[str, int]:
    false_deletions = 0
    missed_deletions = 0
    order_violations = 0
    total_nodes = 0
    for seed in range(graphs):
        rng = random.Random(seed)
        nodes = [
            ArtifactRef(
                sorted(ARTIFACT_KINDS)[index % len(ARTIFACT_KINDS)],
                _digest(f"graph-{seed}-node-{index}"),
            )
            for index in range(16)
        ]
        edges: dict[ArtifactRef, set[ArtifactRef]] = {}
        for index, node in enumerate(nodes):
            candidates = nodes[:index]
            rng.shuffle(candidates)
            edges[node] = set(candidates[: rng.randrange(0, min(3, index) + 1)])
        active_roots = set(rng.sample(nodes, 3))
        recent_roots = set(rng.sample(nodes, 2))
        expected_live = _independent_reachable(active_roots | recent_roots, edges)
        with tempfile.TemporaryDirectory(prefix="symcc-f429-graph-") as directory:
            registry = ArtifactLifecycleRegistry(directory)
            for node in nodes:
                registry.record_artifact(
                    node,
                    encoded_bytes=1,
                    edges=tuple(sorted(edges[node])),
                    now=95.0 if node in recent_roots else 1.0,
                )
            lease = registry.start_job(
                f"graph-{seed}", "oracle", lease_seconds=100.0, now=100.0
            )
            registry.touch(tuple(sorted(active_roots)), lease=lease, now=100.0)
            first = registry.collect(
                lambda _kind, _digest_value, size: size,
                grace_seconds=10.0,
                max_objects=64,
                max_bytes=64,
                time_budget_ms=5_000,
                now=101.0,
            )
            deleted = set(first.deleted)
            expected_dead = set(nodes) - expected_live
            false_deletions += len(deleted & expected_live)
            missed_deletions += len(expected_dead - deleted)
            positions = {node: index for index, node in enumerate(first.deleted)}
            for source, targets in edges.items():
                for target in targets:
                    if source in positions and target in positions:
                        order_violations += int(
                            positions[source] >= positions[target]
                        )
            registry.release_job(lease, now=102.0)
            second = registry.collect(
                lambda _kind, _digest_value, size: size,
                grace_seconds=10.0,
                max_objects=64,
                max_bytes=64,
                time_budget_ms=5_000,
                now=200.0,
            )
            missed_deletions += registry.stats(now=200.0)["artifacts"]
            if len(first.deleted) + len(second.deleted) != len(nodes):
                missed_deletions += 1
        total_nodes += len(nodes)
    return {
        "graphs": graphs,
        "nodes": total_nodes,
        "false_deletions": false_deletions,
        "missed_deletions": missed_deletions,
        "dependency_order_violations": order_violations,
    }


def _verifier(tool_root: Path, proof_store: QfbvProofStore) -> QfbvProofVerifier:
    return QfbvProofVerifier(
        proof_store,
        generator_command=[
            str(tool_root / "bin/cvc5"),
            "--lang=smt2",
            "--safe-mode=safe",
            "--proof-granularity=dsl-rewrite",
            "--dump-proofs",
            "{query}",
        ],
        checker_command=[str(tool_root / "bin/ethos"), "{proof}"],
        signature_root=tool_root / "share/cpc",
    )


def _live_store_oracle(tool_root: Path) -> dict[str, Any]:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="symcc-f429-live-") as directory:
        root = Path(directory)
        registry = ArtifactLifecycleRegistry(root / "lifecycle")
        lease = registry.start_job(
            "live-oracle", "producer", lease_seconds=60.0
        )
        contexts = CrossWorkerContextStore(
            root / "contexts",
            lifecycle=registry,
            lifecycle_lease=lease,
        )
        proofs = QfbvProofStore(
            root / "proofs",
            lifecycle=registry,
            lifecycle_lease=lease,
        )
        verifier = _verifier(tool_root, proofs)
        lemmas = QfbvLemmaStore(
            root / "lemmas",
            lifecycle=registry,
            lifecycle_lease=lease,
        )
        exchange = QfbvLemmaExchange(lemmas, contexts, verifier)
        terms = (
            "(= symcc_input_0 (_ bv65 8))",
            "(or (= symcc_input_1 (_ bv66 8)) "
            "(= symcc_input_0 (_ bv64 8)))",
        )
        capabilities = normalize_qfbv_capabilities(
            {"incremental": True, "accept_unsat": False}
        )
        publication = contexts.publish_chain(
            tuple(_digest(term) for term in terms),
            terms,
            capability_sha256=str(capabilities["capability_sha256"]),
        )
        assert publication is not None
        authorization = exchange.certify_and_publish(
            publication.context_sha256,
            "(= symcc_input_1 #b01000010)",
            category="preprocess",
            timeout_ms=5_000,
        )
        inventories = {
            "context": contexts.synchronize_lifecycle(max_entries=10),
            "proof": proofs.synchronize_lifecycle(max_entries=10),
            "lemma": lemmas.synchronize_lifecycle(max_entries=10),
        }
        before = registry.stats()

        def delete(kind: str, digest: str, size: int) -> int:
            if kind == "context":
                return contexts.delete_lifecycle_artifact(kind, digest, size)
            if kind in {"proof", "receipt"}:
                return proofs.delete_lifecycle_artifact(kind, digest, size)
            return lemmas.delete_lifecycle_artifact(kind, digest, size)

        protected = registry.collect(
            delete,
            grace_seconds=0.0,
            max_objects=10,
            max_bytes=before["artifact_bytes"] + 1,
            time_budget_ms=5_000,
            now=time.time() + 1.0,
        )
        registry.release_job(lease)
        collected = registry.collect(
            delete,
            grace_seconds=0.0,
            max_objects=10,
            max_bytes=before["artifact_bytes"] + 1,
            time_budget_ms=5_000,
            now=time.time() + 1.0,
        )
        after = {
            "lifecycle": registry.stats(),
            "contexts": contexts.stats(),
            "proofs": proofs.stats(),
            "lemmas": lemmas.stats(),
        }

        stale_first = registry.start_job(
            "fenced", "old-worker", lease_seconds=60.0
        )
        registry.start_job("fenced", "new-worker", lease_seconds=60.0)
        stale_rejected = 0
        try:
            registry.touch(
                (ArtifactRef("lemma", authorization.record["record_sha256"]),),
                lease=stale_first,
            )
        except ArtifactLifecycleError:
            stale_rejected = 1

        orphan_registry = ArtifactLifecycleRegistry(root / "orphan-lifecycle")
        orphan_contexts = CrossWorkerContextStore(
            root / "orphan-contexts", lifecycle=orphan_registry
        )
        orphan_term = "(= symcc_input_0 (_ bv1 8))"
        orphan_manifest = build_context_manifests(
            (_digest(orphan_term),),
            (orphan_term,),
            capability_sha256=_digest("orphan-capability"),
        )[0]
        orphan_encoded = _lemma_json(orphan_manifest) + b"\n"
        orphan_digest = str(orphan_manifest["context_sha256"])
        orphan_registry.record_artifact(
            ArtifactRef("context", orphan_digest),
            encoded_bytes=len(orphan_encoded),
            now=1.0,
        )
        orphan_contexts._publish_object(orphan_digest, orphan_encoded)
        orphan_collected = orphan_registry.collect(
            orphan_contexts.delete_lifecycle_artifact,
            grace_seconds=0.0,
            max_objects=1,
            max_bytes=len(orphan_encoded),
            time_budget_ms=5_000,
            now=10.0,
        )
        return {
            "before": before,
            "inventories": inventories,
            "active_protected": protected.protected,
            "active_deleted": len(protected.deleted),
            "released_deleted": len(collected.deleted),
            "released_deleted_bytes": collected.deleted_bytes,
            "released_stop_reason": collected.stop_reason,
            "after": after,
            "stale_generation_rejected": stale_rejected,
            "orphan_deleted": len(orphan_collected.deleted),
            "orphan_store_contexts": orphan_contexts.stats()["contexts"],
            "elapsed_us": int((time.monotonic() - started) * 1_000_000),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tool-root",
        type=Path,
        default=Path.home() / ".local/share/symcc-cpc-1.3.4",
    )
    parser.add_argument("--graphs", type=int, default=64)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    graphs = max(1, min(args.graphs, 256))
    tool_root = args.tool_root.resolve()
    cvc5 = tool_root / "bin/cvc5"
    ethos = tool_root / "bin/ethos"
    if not cvc5.is_file() or not ethos.is_file():
        raise SystemExit("run install_cvc5_cpc_ethos_1_3_4.sh first")
    payload = {
        "schema": "symcc-f429-qfbv-artifact-lifecycle-oracle-v1",
        "finite_graphs": _finite_graph_oracle(graphs),
        "live": _live_store_oracle(tool_root),
        "toolchain": {
            "cvc5_version": subprocess.run(
                [str(cvc5), "--version"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()[0],
            "cvc5_sha256": _sha256(cvc5),
            "ethos_sha256": _sha256(ethos),
            "signature_files": len(list((tool_root / "share/cpc").rglob("*.eo"))),
        },
        "claim": (
            "graph semantics, live-store deletion, fencing, and crash-window "
            "evidence only; not solver or coverage speedup"
        ),
    }
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(encoded, encoding="ascii")
    print(encoded, end="")
    checks = payload["finite_graphs"]
    live = payload["live"]
    return 0 if (
        checks["false_deletions"] == 0
        and checks["missed_deletions"] == 0
        and checks["dependency_order_violations"] == 0
        and live["active_protected"] == 5
        and live["active_deleted"] == 0
        and live["released_deleted"] == 5
        and live["stale_generation_rejected"] == 1
        and live["orphan_deleted"] == 1
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
