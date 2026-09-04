#!/usr/bin/env python3
"""Exercise identity-closed cluster-lock qualification failure modes."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
import threading
from unittest import mock


EVIDENCE = Path(__file__).resolve().parent
REPO = EVIDENCE.parents[3]
UTIL = REPO / "util"
if str(UTIL) not in sys.path:
    sys.path.insert(0, str(UTIL))

import mpi_filesystem_qualification as qualification  # noqa: E402
from distributed_state import probe_shared_state_filesystem  # noqa: E402


class _CompletedRequest:
    def Test(self) -> bool:
        return True


class _MessageBus:
    def __init__(self, size: int):
        self.size = size
        self.lock = threading.Lock()
        self.queues = defaultdict(deque)

    def communicator(self, rank: int):
        return _ThreadCommunicator(self, rank)


class _ThreadCommunicator:
    def __init__(self, bus: _MessageBus, rank: int):
        self.bus = bus
        self.rank = rank

    def Get_rank(self) -> int:
        return self.rank

    def Get_size(self) -> int:
        return self.bus.size

    def isend(self, message, *, dest: int, tag: int):
        with self.bus.lock:
            self.bus.queues[(dest, self.rank, tag)].append(message)
        return _CompletedRequest()

    def iprobe(self, *, source: int, tag: int) -> bool:
        with self.bus.lock:
            return bool(self.bus.queues[(self.rank, source, tag)])

    def recv(self, *, source: int, tag: int):
        with self.bus.lock:
            return self.bus.queues[(self.rank, source, tag)].popleft()


def _run_cluster(
    root: str,
    processors: tuple[str, ...],
    *,
    publication_root: str | None = None,
    mutate_capability=None,
    close_hook=None,
):
    capability = probe_shared_state_filesystem(
        root,
        timeout=2.0,
        publication_root=publication_root,
    )
    if mutate_capability is not None:
        capability = mutate_capability(capability)
    bus = _MessageBus(len(processors))
    results = [None] * len(processors)
    errors = []

    def run(rank: int) -> None:
        try:
            results[rank] = qualification.qualify_mpi_cluster_advisory_lock(
                bus.communicator(rank),
                capability,
                root=root,
                epoch=hashlib.sha256(b"F351-integration-epoch").hexdigest(),
                global_rank=rank,
                expected_master_ranks=tuple(range(len(processors))),
                processor_name=processors[rank],
                timeout=3.0,
            )
        except BaseException as error:
            errors.append(f"{type(error).__name__}: {error}")

    patcher = (
        mock.patch.object(
            qualification,
            "_verify_lock_namespace_anchor",
            side_effect=close_hook,
        )
        if close_hook is not None else mock.patch.object(
            qualification,
            "_verify_lock_namespace_anchor",
            wraps=qualification._verify_lock_namespace_anchor,
        )
    )
    with patcher:
        threads = [
            threading.Thread(target=run, args=(rank,))
            for rank in range(len(processors))
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5.0)
    if errors or any(thread.is_alive() for thread in threads):
        raise RuntimeError({
            "errors": errors,
            "threads_alive": [thread.is_alive() for thread in threads],
        })
    return capability, tuple(results)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="symcc-f351-integration-") as tmp:
        workspace = Path(tmp)

        normal_root = workspace / "normal"
        local_capability, normal = _run_cluster(
            str(normal_root), ("synthetic-node-a", "synthetic-node-b", "synthetic-node-b")
        )
        normal_snapshot = normal[0].capability.snapshot()

        replacement_root = workspace / "same-content-replacement"
        replacement_lock = threading.Lock()
        replacement = {}
        original_close = qualification._verify_lock_namespace_anchor

        def replace_before_close(*args, **kwargs):
            with replacement_lock:
                if not replacement:
                    public_lock = (
                        replacement_root / qualification._LOCK_FILENAME
                    )
                    exact_content = public_lock.read_bytes()
                    old_inode = public_lock.stat().st_ino
                    competitor = replacement_root / "competitor.lock"
                    with competitor.open("xb") as stream:
                        stream.write(exact_content)
                        stream.flush()
                        os.fsync(stream.fileno())
                    competitor_inode = competitor.stat().st_ino
                    os.replace(competitor, public_lock)
                    replacement.update({
                        "old_inode": old_inode,
                        "new_inode": competitor_inode,
                        "sha256": hashlib.sha256(exact_content).hexdigest(),
                    })
            return original_close(*args, **kwargs)

        _, replaced = _run_cluster(
            str(replacement_root),
            ("synthetic-node-a", "synthetic-node-b"),
            close_hook=replace_before_close,
        )

        stale_root = workspace / "stale-root-binding"
        _, stale_root_results = _run_cluster(
            str(stale_root),
            ("synthetic-node-a", "synthetic-node-b"),
            mutate_capability=lambda capability: replace(
                capability, device=capability.device + 1),
        )

        stale_publication_root = workspace / "stale-publication-state"
        publication_root = workspace / "publication"
        _, stale_publication_results = _run_cluster(
            str(stale_publication_root),
            ("synthetic-node-a", "synthetic-node-b"),
            publication_root=str(publication_root),
            mutate_capability=lambda capability: replace(
                capability,
                publication_filesystem_id=(
                    capability.publication_filesystem_id + 1
                ),
            ),
        )

        same_host_root = workspace / "same-host"
        _, same_host = _run_cluster(
            str(same_host_root),
            ("one-real-processor-name", "one-real-processor-name"),
        )

        controller = qualification.ClusterLockRenewalController(
            epoch=hashlib.sha256(b"F351-legacy-controller").hexdigest(),
            interval=5.0,
            timeout=2.0,
            completed_at=0.0,
            require_configuration_consensus=False,
        )
        request = controller.begin_request()
        legacy_result = qualification.ClusterLockQualificationResult(
            clean=True,
            verified=True,
            capability=replace(
                normal[0].capability,
                probe_scope="cross-host-mpi-lock-v1",
                cluster_lock_identity_checks=0,
            ),
            members=((0, "synthetic-node-a"), (1, "synthetic-node-b")),
            representatives=(0, 1),
            rounds=2,
            contention_checks=2,
            release_checks=2,
            elapsed=0.1,
            identity_checks=0,
        )
        legacy_accepted = controller.complete(
            request["generation"], legacy_result, completed_at=5.1)

        replacement_content = (
            replacement_root / qualification._LOCK_FILENAME
        ).read_bytes()
        checks = {
            "normal_litmus_closes_every_master_namespace": (
                all(result.clean and result.verified for result in normal)
                and all(result.rounds == 2 for result in normal)
                and all(result.contention_checks == 4 for result in normal)
                and all(result.release_checks == 2 for result in normal)
                and all(result.identity_checks == 3 for result in normal)
            ),
            "upgraded_snapshot_is_v3_and_v2_scoped": (
                normal_snapshot["schema"]
                == "symcc-shared-filesystem-capabilities-v3"
                and normal_snapshot["probe_scope"]
                == "cross-host-mpi-lock-v2"
                and normal_snapshot["cluster_lock_identity_checks"] == 3
            ),
            "local_probe_remains_explicitly_same_host": (
                local_capability.snapshot()["schema"]
                == "symcc-shared-filesystem-capabilities-v1"
                and local_capability.probe_scope == "same-host-subprocess-v1"
                and not local_capability.cluster_lock_verified
            ),
            "same_content_replacement_fails_every_master": (
                replacement["old_inode"] != replacement["new_inode"]
                and hashlib.sha256(replacement_content).hexdigest()
                == replacement["sha256"]
                and all(not result.clean and not result.verified
                        for result in replaced)
                and all(result.rounds == 2 and result.identity_checks == 0
                        for result in replaced)
                and all("cluster lock path identity changed" in result.error
                        for result in replaced)
            ),
            "stale_state_filesystem_binding_fails_before_rounds": (
                all(not result.clean and result.rounds == 0
                    for result in stale_root_results)
                and all("state root filesystem identity changed" in result.error
                        for result in stale_root_results)
            ),
            "stale_publication_binding_fails_before_rounds": (
                all(not result.clean and result.rounds == 0
                    for result in stale_publication_results)
                and all("publication root filesystem identity changed"
                        in result.error
                        for result in stale_publication_results)
            ),
            "one_processor_name_never_claims_cross_host_proof": (
                all(result.clean and not result.verified
                    for result in same_host)
                and all(result.identity_checks == 0 for result in same_host)
                and not (
                    same_host_root / qualification._LOCK_FILENAME
                ).exists()
            ),
            "runtime_controller_rejects_legacy_v1_evidence": (
                not legacy_accepted
                and controller.failures == 1
                and controller.successes == 0
            ),
        }
        output = {
            "schema": "symcc-f351-identity-closed-lock-evidence-v1",
            "environment": {
                "python": platform.python_version(),
                "kernel": platform.release(),
                "machine": platform.machine(),
                "filesystem_type": normal_snapshot["filesystem_type"],
                "actual_mpi_transport": False,
                "actual_multi_host": False,
                "synthetic_processor_names": True,
            },
            "normal": {
                "members": list(normal[0].members),
                "representatives": list(normal[0].representatives),
                "rounds": normal[0].rounds,
                "contention_checks": normal[0].contention_checks,
                "release_checks": normal[0].release_checks,
                "identity_checks": normal[0].identity_checks,
                "capability": normal_snapshot,
            },
            "same_content_replacement": {
                **replacement,
                "public_sha256_after_replace": hashlib.sha256(
                    replacement_content).hexdigest(),
                "errors": [result.error for result in replaced],
                "rounds": [result.rounds for result in replaced],
                "identity_checks": [
                    result.identity_checks for result in replaced
                ],
            },
            "stale_state_binding_errors": [
                result.error for result in stale_root_results
            ],
            "stale_publication_binding_errors": [
                result.error for result in stale_publication_results
            ],
            "same_host": {
                "clean": [result.clean for result in same_host],
                "verified": [result.verified for result in same_host],
                "identity_checks": [
                    result.identity_checks for result in same_host
                ],
            },
            "legacy_controller": controller.snapshot(),
            "checks": checks,
            "all_checks_passed": all(checks.values()),
        }

    encoded = json.dumps(
        output,
        indent=2,
        sort_keys=True,
        ensure_ascii=True,
    ) + "\n"
    (EVIDENCE / "identity-closed-lock-integration.json").write_text(
        encoded, encoding="ascii")
    (EVIDENCE / "identity-closed-lock-integration.log").write_text(
        encoded, encoding="ascii")
    print(encoded, end="")
    return 0 if output["all_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
