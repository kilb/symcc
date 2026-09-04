#!/usr/bin/env python3
"""Exercise generation-bound cluster-lock proof transcript failure modes."""

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
    epoch: str,
    generation: int,
    mutate_transcript_rank: int | None = None,
):
    capability = probe_shared_state_filesystem(root, timeout=2.0)
    bus = _MessageBus(len(processors))
    results = [None] * len(processors)
    errors = []
    active_rank = threading.local()
    proof_transcript = qualification._qualification_proof_transcript

    def mutate_transcript(*args, **kwargs):
        transcript = proof_transcript(*args, **kwargs)
        if getattr(active_rank, "value", None) == mutate_transcript_rank:
            return ("0" if transcript[0] != "0" else "1") + transcript[1:]
        return transcript

    def run(rank: int) -> None:
        try:
            active_rank.value = rank
            results[rank] = qualification.qualify_mpi_cluster_advisory_lock(
                bus.communicator(rank),
                capability,
                root=root,
                epoch=epoch,
                global_rank=rank,
                expected_master_ranks=tuple(range(len(processors))),
                processor_name=processors[rank],
                qualification_generation=generation,
                timeout=3.0,
            )
        except BaseException as error:
            errors.append(f"{type(error).__name__}: {error}")

    patcher = (
        mock.patch.object(
            qualification,
            "_qualification_proof_transcript",
            side_effect=mutate_transcript,
        )
        if mutate_transcript_rank is not None
        else mock.patch.object(
            qualification,
            "_qualification_proof_transcript",
            wraps=qualification._qualification_proof_transcript,
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
        raise RuntimeError(errors or "qualification thread did not terminate")
    if any(result is None for result in results):
        raise RuntimeError("qualification result is missing")
    return results


def main() -> int:
    epoch = hashlib.sha256(b"F352-proof-transcript-epoch").hexdigest()
    processors = ("synthetic-node-a", "synthetic-node-b", "synthetic-node-b")
    generation = 7
    with tempfile.TemporaryDirectory(prefix="symcc-f352-integration-") as tmp:
        normal = _run_cluster(
            os.path.join(tmp, "normal"),
            processors,
            epoch=epoch,
            generation=generation,
        )
        divergent = _run_cluster(
            os.path.join(tmp, "divergent"),
            processors,
            epoch=epoch,
            generation=generation,
            mutate_transcript_rank=1,
        )

        normal_result = normal[0]
        assert normal_result is not None
        assert normal_result.capability is not None
        expected_ranks = tuple(range(len(processors)))
        controller = qualification.ClusterLockRenewalController(
            epoch=epoch,
            interval=5.0,
            timeout=2.0,
            completed_at=0.0,
            generation=generation - 1,
            require_configuration_consensus=False,
            expected_master_ranks=expected_ranks,
            expected_capability=normal_result.capability,
        )
        request = controller.begin_request()
        first_success = controller.complete(
            request["generation"], normal_result, completed_at=1.0)
        replay_request = controller.begin_request()
        replay_success = controller.complete(
            replay_request["generation"], normal_result, completed_at=2.0)

        def accepts(result) -> bool:
            candidate = qualification.ClusterLockRenewalController(
                epoch=epoch,
                interval=5.0,
                timeout=2.0,
                completed_at=0.0,
                generation=generation - 1,
                require_configuration_consensus=False,
                expected_master_ranks=expected_ranks,
                expected_capability=normal_result.capability,
            )
            candidate_request = candidate.begin_request()
            return candidate.complete(
                candidate_request["generation"], result, completed_at=1.0)

        zero_round_success = accepts(replace(normal_result, rounds=0))
        foreign_capability_success = accepts(replace(
            normal_result,
            capability=replace(
                normal_result.capability,
                root=normal_result.capability.root + "-foreign",
            ),
        ))

        transcript = qualification._qualification_proof_transcript
        base = normal_result.proof_transcript
        mutations = {
            transcript(
                hashlib.sha256(b"other-epoch").hexdigest(),
                generation,
                normal_result.members,
                normal_result.representatives,
                2, 4, 2, 3,
            ),
            transcript(
                epoch, generation + 1, normal_result.members,
                normal_result.representatives, 2, 4, 2, 3),
            transcript(
                epoch, generation,
                ((0, "synthetic-node-a"), (1, "synthetic-node-c"),
                 (2, "synthetic-node-b")),
                normal_result.representatives, 2, 4, 2, 3),
            transcript(
                epoch, generation, normal_result.members, (1, 0),
                2, 4, 2, 3),
            transcript(
                epoch, generation, normal_result.members,
                normal_result.representatives, 1, 4, 2, 3),
            transcript(
                epoch, generation, normal_result.members,
                normal_result.representatives, 2, 3, 2, 3),
            transcript(
                epoch, generation, normal_result.members,
                normal_result.representatives, 2, 4, 1, 3),
            transcript(
                epoch, generation, normal_result.members,
                normal_result.representatives, 2, 4, 2, 2),
        }
        recomputed = transcript(
            epoch,
            generation,
            normal_result.members,
            normal_result.representatives,
            normal_result.rounds,
            normal_result.contention_checks,
            normal_result.release_checks,
            normal_result.identity_checks,
        )

        checks = {
            "normal_production_proof_commits": all(
                result.clean and result.verified for result in normal),
            "normal_transcript_equal_all_masters": len({
                result.proof_transcript for result in normal
            }) == 1,
            "normal_transcript_recomputes": base == recomputed,
            "transcript_disagreement_fails_every_master": all(
                not result.clean
                and not result.verified
                and "transcript mismatch" in result.error
                for result in divergent
            ),
            "current_generation_result_is_accepted": first_success,
            "previous_generation_result_replay_is_rejected": not replay_success,
            "zero_round_result_splice_is_rejected": not zero_round_success,
            "foreign_capability_splice_is_rejected": (
                not foreign_capability_success),
            "transcript_binds_every_declared_field": (
                len(mutations) == 8 and base not in mutations),
            "renewal_metrics_conserve_attempt_outcomes": (
                controller.attempts == 2
                and controller.successes == 1
                and controller.failures == 1
            ),
        }
        output = {
            "schema": "symcc-f352-generation-bound-lock-proof-v1",
            "environment": {
                "python": platform.python_version(),
                "kernel": platform.release(),
                "machine": platform.machine(),
                "filesystem_type": normal_result.capability.filesystem_type,
                "actual_mpi_transport": False,
                "actual_multi_host": False,
                "synthetic_processor_names": True,
            },
            "configuration": {
                "epoch": epoch,
                "generation": generation,
                "members": [list(member) for member in normal_result.members],
                "representatives": list(normal_result.representatives),
                "rounds": normal_result.rounds,
                "contention_checks": normal_result.contention_checks,
                "release_checks": normal_result.release_checks,
                "identity_checks": normal_result.identity_checks,
            },
            "normal": {
                "transcript": base,
                "transcripts": [result.proof_transcript for result in normal],
                "result_generations": [
                    result.qualification_generation for result in normal
                ],
                "clean": [result.clean for result in normal],
                "verified": [result.verified for result in normal],
            },
            "divergent": {
                "transcripts": [
                    result.proof_transcript for result in divergent
                ],
                "errors": [result.error for result in divergent],
                "rounds": [result.rounds for result in divergent],
                "identity_checks": [
                    result.identity_checks for result in divergent
                ],
            },
            "renewal": {
                "current_generation_accepted": first_success,
                "previous_generation_replay_accepted": replay_success,
                "zero_round_splice_accepted": zero_round_success,
                "foreign_capability_splice_accepted": (
                    foreign_capability_success),
                "metrics": controller.snapshot(),
            },
            "transcript_mutation_cardinality": len(mutations),
            "checks": checks,
            "all_checks_passed": all(checks.values()),
            "proof_boundary": (
                "local production-mechanism evidence with a thread control plane "
                "and synthetic processor names; no real MPI, multi-host storage, "
                "Byzantine-resistance, throughput, coverage, bug-discovery, or "
                "LAVA-M uplift claim"
            ),
        }
    rendered = json.dumps(output, indent=2, sort_keys=True) + "\n"
    (EVIDENCE / "generation-bound-transcript-integration.json").write_text(
        rendered, encoding="utf-8")
    (EVIDENCE / "generation-bound-transcript-integration.log").write_text(
        rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if output["all_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
