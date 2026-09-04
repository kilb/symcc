# RUN: python3 %s

from collections import defaultdict, deque
from contextlib import nullcontext
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

import mpi_filesystem_qualification as qualification  # noqa: E402
from distributed_state import probe_shared_state_filesystem  # noqa: E402


class _CompletedRequest:
    def Test(self):
        return True


class _MessageBus:
    def __init__(self, size):
        self.size = size
        self._lock = threading.Lock()
        self._queues = defaultdict(deque)

    def communicator(self, rank):
        return _ThreadCommunicator(self, rank)

    def send(self, source, destination, tag, message):
        with self._lock:
            self._queues[(destination, source, tag)].append(message)

    def probe(self, destination, source, tag):
        with self._lock:
            return bool(self._queues[(destination, source, tag)])

    def receive(self, destination, source, tag):
        with self._lock:
            return self._queues[(destination, source, tag)].popleft()


class _ThreadCommunicator:
    def __init__(self, bus, rank):
        self.bus = bus
        self.rank = rank

    def Get_rank(self):
        return self.rank

    def Get_size(self):
        return self.bus.size

    def isend(self, message, *, dest, tag):
        self.bus.send(self.rank, dest, tag, message)
        return _CompletedRequest()

    def iprobe(self, *, source, tag):
        return self.bus.probe(self.rank, source, tag)

    def recv(self, *, source, tag):
        return self.bus.receive(self.rank, source, tag)


class ClusterFilesystemQualificationTests(unittest.TestCase):
    def _run_cluster(
        self,
        root,
        processors,
        *,
        patch_lock=False,
        failed_local_rank=None,
        epoch="a" * 64,
        qualification_generation=0,
        replace_lock_before_identity_closure=False,
        mutate_capability=None,
        mutate_transcript_rank=None,
    ):
        masters = tuple(range(len(processors)))
        bus = _MessageBus(len(masters))
        capability = probe_shared_state_filesystem(root, timeout=2.0)
        if mutate_capability is not None:
            capability = mutate_capability(capability)
        results = [None] * len(masters)
        errors = []
        replacement_lock = threading.Lock()
        replacement_observation = {}
        verify_anchor = qualification._verify_lock_namespace_anchor
        proof_transcript = qualification._qualification_proof_transcript
        active_rank = threading.local()

        def replace_then_verify(*args, **kwargs):
            with replacement_lock:
                if not replacement_observation:
                    lock_path = os.path.join(root, qualification._LOCK_FILENAME)
                    replacement = lock_path + ".same-content-replacement"
                    with open(lock_path, "rb") as stream:
                        exact_content = stream.read()
                    old_identity = os.stat(
                        lock_path, follow_symlinks=False).st_ino
                    with open(replacement, "xb") as stream:
                        stream.write(exact_content)
                        stream.flush()
                        os.fsync(stream.fileno())
                    replacement_identity = os.stat(
                        replacement, follow_symlinks=False).st_ino
                    os.replace(replacement, lock_path)
                    replacement_observation.update({
                        "old_identity": old_identity,
                        "replacement_identity": replacement_identity,
                        "content": exact_content,
                    })
            return verify_anchor(*args, **kwargs)

        def run(rank):
            try:
                active_rank.value = rank
                results[rank] = qualification.qualify_mpi_cluster_advisory_lock(
                    bus.communicator(rank),
                    None if rank == failed_local_rank else capability,
                    root=root,
                    epoch=epoch,
                    global_rank=rank,
                    expected_master_ranks=masters,
                    processor_name=processors[rank],
                    qualification_generation=(
                        qualification_generation[rank]
                        if isinstance(qualification_generation, tuple)
                        else qualification_generation
                    ),
                    timeout=3.0,
                    local_error=(
                        "injected local probe failure"
                        if rank == failed_local_rank else ""
                    ),
                )
            except BaseException as error:  # surfaced by the assertions below
                errors.append(error)

        def mutate_transcript(*args, **kwargs):
            transcript = proof_transcript(*args, **kwargs)
            if getattr(active_rank, "value", None) == mutate_transcript_rank:
                return ("0" if transcript[0] != "0" else "1") + transcript[1:]
            return transcript

        patcher = (
            mock.patch.object(
                qualification,
                "_try_exclusive_lock",
                return_value=("acquired", ""),
            )
            if patch_lock else nullcontext()
        )
        identity_patcher = (
            mock.patch.object(
                qualification,
                "_verify_lock_namespace_anchor",
                side_effect=replace_then_verify,
            )
            if replace_lock_before_identity_closure else nullcontext()
        )
        transcript_patcher = (
            mock.patch.object(
                qualification,
                "_qualification_proof_transcript",
                side_effect=mutate_transcript,
            )
            if mutate_transcript_rank is not None else nullcontext()
        )
        with patcher, identity_patcher, transcript_patcher:
            threads = [threading.Thread(target=run, args=(rank,))
                       for rank in masters]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5.0)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertTrue(all(result is not None for result in results))
        if replace_lock_before_identity_closure:
            self.assertTrue(replacement_observation)
            self.assertNotEqual(
                replacement_observation["old_identity"],
                replacement_observation["replacement_identity"],
            )
            with open(
                os.path.join(root, qualification._LOCK_FILENAME),
                "rb",
            ) as stream:
                self.assertEqual(
                    stream.read(), replacement_observation["content"])
        return results

    def _successful_renewal_result(self, epoch, generation=1):
        members = ((0, "node-a"), (1, "node-b"))
        representatives = (0, 1)
        with tempfile.TemporaryDirectory() as tmp:
            capability = probe_shared_state_filesystem(tmp, timeout=2.0)
            capability = qualification.qualify_shared_filesystem_cluster_lock(
                capability,
                members=members,
                representatives=representatives,
                rounds=2,
                contention_checks=2,
                release_checks=2,
                identity_checks=2,
            )
        transcript = qualification._qualification_proof_transcript(
            epoch,
            generation,
            members,
            representatives,
            2,
            2,
            2,
            2,
        )
        return qualification.ClusterLockQualificationResult(
            clean=True,
            verified=True,
            capability=capability,
            members=members,
            representatives=representatives,
            rounds=2,
            contention_checks=2,
            release_checks=2,
            elapsed=0.1,
            identity_checks=2,
            qualification_generation=generation,
            proof_transcript=transcript,
        )

    def _run_renewal_configuration_consensus(self, configurations):
        bus = _MessageBus(len(configurations))
        epoch = hashlib.sha256(b"renewal-config-consensus").hexdigest()
        results = [None] * len(configurations)
        errors = []

        def run(rank):
            try:
                controller = qualification.ClusterLockRenewalController(
                    epoch=epoch,
                    completed_at=0.0,
                    **configurations[rank],
                )
                results[rank] = (
                    qualification.qualify_cluster_lock_renewal_configuration(
                        bus.communicator(rank),
                        controller,
                        timeout=1.0,
                    )
                )
            except BaseException as error:  # surfaced by assertions below
                errors.append(error)

        threads = [
            threading.Thread(target=run, args=(rank,))
            for rank in range(len(configurations))
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2.0)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertTrue(all(result is not None for result in results))
        return results

    def test_every_host_holds_and_every_master_observes_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = self._run_cluster(tmp, ("node-a", "node-b", "node-b"))

        transcripts = {result.proof_transcript for result in results}
        self.assertEqual(len(transcripts), 1)
        for result in results:
            self.assertTrue(result.clean)
            self.assertTrue(result.verified)
            self.assertEqual(result.members, (
                (0, "node-a"), (1, "node-b"), (2, "node-b")))
            self.assertEqual(result.representatives, (0, 1))
            self.assertEqual(result.rounds, 2)
            self.assertEqual(result.contention_checks, 4)
            self.assertEqual(result.release_checks, 2)
            self.assertEqual(result.identity_checks, 3)
            self.assertEqual(result.qualification_generation, 0)
            self.assertTrue(qualification._valid_token(result.proof_transcript))
            self.assertTrue(result.capability.cluster_lock_verified)
            self.assertEqual(
                result.capability.probe_scope, "cross-host-mpi-lock-v2")
            self.assertEqual(
                result.capability.cluster_lock_identity_checks, 3)

    def test_transcript_disagreement_fails_closed_on_every_master(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = self._run_cluster(
                tmp,
                ("node-a", "node-b"),
                qualification_generation=1,
                mutate_transcript_rank=1,
            )

        for result in results:
            self.assertFalse(result.clean)
            self.assertFalse(result.verified)
            self.assertEqual(result.rounds, 2)
            self.assertEqual(result.identity_checks, 2)
            self.assertEqual(result.qualification_generation, 1)
            self.assertIn("transcript mismatch", result.error)

    def test_proof_transcript_binds_generation_topology_and_all_counts(self):
        epoch = hashlib.sha256(b"proof-transcript-a").hexdigest()
        other_epoch = hashlib.sha256(b"proof-transcript-b").hexdigest()

        def transcript(
            *,
            selected_epoch=epoch,
            generation=1,
            members=((0, "node-a"), (1, "node-b")),
            representatives=(0, 1),
            rounds=2,
            contention=2,
            release=2,
            identity=2,
        ):
            return qualification._qualification_proof_transcript(
                selected_epoch,
                generation,
                members,
                representatives,
                rounds,
                contention,
                release,
                identity,
            )

        baseline = transcript()
        mutations = {
            "epoch": transcript(selected_epoch=other_epoch),
            "generation": transcript(generation=2),
            "member-rank": transcript(
                members=((0, "node-a"), (2, "node-b"))),
            "processor": transcript(
                members=((0, "node-a"), (1, "node-c"))),
            "representatives": transcript(representatives=(1, 0)),
            "rounds": transcript(rounds=1),
            "contention": transcript(contention=1),
            "release": transcript(release=1),
            "identity": transcript(identity=1),
        }
        self.assertTrue(qualification._valid_token(baseline))
        for field, observed in mutations.items():
            with self.subTest(field=field):
                self.assertNotEqual(observed, baseline)
        self.assertEqual(len(set(mutations.values()) | {baseline}), 10)

    def test_same_content_lock_replacement_fails_namespace_identity_closure(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = self._run_cluster(
                tmp,
                ("node-a", "node-b"),
                replace_lock_before_identity_closure=True,
            )

        for result in results:
            self.assertFalse(result.clean)
            self.assertFalse(result.verified)
            self.assertEqual(result.rounds, 2)
            self.assertEqual(result.identity_checks, 0)
            self.assertIn("cluster lock path identity changed", result.error)
            self.assertFalse(result.capability.cluster_lock_verified)

    def test_stale_capability_filesystem_identity_fails_before_lock_rounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = self._run_cluster(
                tmp,
                ("node-a", "node-b"),
                mutate_capability=lambda capability: qualification.replace(
                    capability,
                    device=capability.device + 1,
                ),
            )

        for result in results:
            self.assertFalse(result.clean)
            self.assertFalse(result.verified)
            self.assertEqual(result.rounds, 0)
            self.assertIn("state root filesystem identity changed", result.error)

    def test_broken_exclusion_fails_closed_on_every_master(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = self._run_cluster(
                tmp, ("node-a", "node-b"), patch_lock=True)

        for result in results:
            self.assertFalse(result.clean)
            self.assertFalse(result.verified)
            self.assertIn("acquired a held cluster lock", result.error)

    def test_same_epoch_can_be_requalified_with_a_new_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            initial = self._run_cluster(
                tmp,
                ("node-a", "node-b"),
                qualification_generation=0,
            )
            lock_path = os.path.join(tmp, qualification._LOCK_FILENAME)
            with open(lock_path, "rb") as stream:
                initial_identity = stream.read()
            renewed = self._run_cluster(
                tmp,
                ("node-a", "node-b"),
                qualification_generation=1,
            )
            with open(lock_path, "rb") as stream:
                renewed_identity = stream.read()

        self.assertTrue(all(result.verified for result in initial))
        self.assertTrue(all(result.verified for result in renewed))
        self.assertEqual(renewed_identity, initial_identity)

    def test_generation_disagreement_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = self._run_cluster(
                tmp,
                ("node-a", "node-b"),
                qualification_generation=(1, 2),
            )

        for result in results:
            self.assertFalse(result.clean)
            self.assertFalse(result.verified)
            self.assertTrue(
                "malformed" in result.error or "timed out" in result.error,
                result.error,
            )

    def test_runtime_semantic_drift_is_detected_on_the_next_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            initial = self._run_cluster(
                tmp,
                ("node-a", "node-b"),
                qualification_generation=0,
            )
            drifted = self._run_cluster(
                tmp,
                ("node-a", "node-b"),
                qualification_generation=1,
                patch_lock=True,
            )

        self.assertTrue(all(result.verified for result in initial))
        for result in drifted:
            self.assertFalse(result.clean)
            self.assertFalse(result.verified)
            self.assertIn("acquired a held cluster lock", result.error)

    def test_invalid_generation_reaches_fail_closed_consensus(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = self._run_cluster(
                tmp,
                ("node-a", "node-b"),
                qualification_generation=-1,
            )

        for result in results:
            self.assertFalse(result.clean)
            self.assertFalse(result.verified)
            self.assertIn("invalid cluster lock qualification generation",
                          result.error)

    def test_same_processor_names_never_claim_cross_host_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = self._run_cluster(tmp, ("node-a", "node-a"))
            self.assertFalse(os.path.exists(os.path.join(
                tmp, qualification._LOCK_FILENAME)))

        for result in results:
            self.assertTrue(result.clean)
            self.assertFalse(result.verified)
            self.assertFalse(result.capability.cluster_lock_verified)
            self.assertEqual(result.capability.cluster_lock_members, (
                (0, "node-a"), (1, "node-a")))
            self.assertEqual(
                result.capability.cluster_lock_representatives, (0,))
            self.assertEqual(result.rounds, 0)

    def test_one_local_probe_failure_reaches_consensus_without_lock_rounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = self._run_cluster(
                tmp,
                ("node-a", "node-b", "node-c"),
                failed_local_rank=1,
            )

        for result in results:
            self.assertFalse(result.clean)
            self.assertFalse(result.verified)
            self.assertEqual(result.rounds, 0)
            self.assertIn(
                "master 1 local probe failed: injected local probe failure",
                result.error,
            )

    def test_mismatched_stable_lock_record_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(
                os.path.join(tmp, qualification._LOCK_FILENAME),
                "wb",
            ) as stream:
                stream.write(b"another cluster generation\n")
                stream.flush()
                os.fsync(stream.fileno())
            results = self._run_cluster(tmp, ("node-a", "node-b"))

        for result in results:
            self.assertFalse(result.clean)
            self.assertFalse(result.verified)
            self.assertEqual(result.rounds, 0)
            self.assertIn("identity mismatch", result.error)

    def test_missing_master_is_bounded_and_fails_closed(self):
        class MissingPeerCommunicator:
            def Get_rank(self):
                return 0

            def Get_size(self):
                return 2

            def iprobe(self, *, source, tag):
                return False

            def isend(self, message, *, dest, tag):
                return _CompletedRequest()

        clock = [0.0]

        def monotonic():
            return clock[0]

        def sleep(delay):
            clock[0] += delay

        with tempfile.TemporaryDirectory() as tmp:
            capability = probe_shared_state_filesystem(tmp, timeout=2.0)
            result = qualification.qualify_mpi_cluster_advisory_lock(
                MissingPeerCommunicator(),
                capability,
                root=tmp,
                epoch="b" * 64,
                global_rank=0,
                expected_master_ranks=(0, 1),
                processor_name="node-a",
                timeout=0.025,
                monotonic=monotonic,
                sleep=sleep,
            )

        self.assertFalse(result.clean)
        self.assertFalse(result.verified)
        self.assertIn("timed out", result.error)
        self.assertAlmostEqual(result.elapsed, 0.025)

    def test_qualification_boundary_contains_rank_exceptions(self):
        class UnexpectedCommunicator:
            def Get_rank(self):
                raise LookupError("injected communicator adapter failure")

            def Get_size(self):
                return 2

        with tempfile.TemporaryDirectory() as tmp:
            capability = probe_shared_state_filesystem(tmp, timeout=2.0)
            result = qualification.qualify_mpi_cluster_advisory_lock(
                UnexpectedCommunicator(),
                capability,
                root=tmp,
                epoch="b" * 64,
                global_rank=0,
                expected_master_ranks=(0, 1),
                processor_name="node-a",
                qualification_generation=7,
                timeout=0.025,
            )

        self.assertFalse(result.clean)
        self.assertFalse(result.verified)
        self.assertIs(result.capability, capability)
        self.assertEqual(result.qualification_generation, 7)
        self.assertEqual(result.elapsed, 0.0)
        self.assertEqual(
            result.error,
            "cluster lock qualification raised: "
            "injected communicator adapter failure",
        )

        long_error = LookupError("x" * 4096)
        with mock.patch.object(
            qualification,
            "_qualify_mpi_cluster_advisory_lock",
            side_effect=long_error,
        ):
            bounded = qualification.qualify_mpi_cluster_advisory_lock(
                object(),
                capability,
                root="unused",
                epoch="b" * 64,
                global_rank=0,
                expected_master_ranks=(0, 1),
                processor_name="node-a",
                qualification_generation=True,
            )
        self.assertEqual(len(bounded.error), 512)
        self.assertEqual(bounded.qualification_generation, 0)
        self.assertTrue(bounded.error.startswith(
            "cluster lock qualification raised: "))

        class BrokenTextError(Exception):
            def __str__(self):
                raise RuntimeError("broken exception rendering")

        with mock.patch.object(
            qualification,
            "_qualify_mpi_cluster_advisory_lock",
            side_effect=BrokenTextError(),
        ):
            rendered = qualification.qualify_mpi_cluster_advisory_lock(
                object(),
                capability,
                root="unused",
                epoch="b" * 64,
                global_rank=0,
                expected_master_ranks=(0, 1),
                processor_name="node-a",
            )
        self.assertEqual(
            rendered.error,
            "cluster lock qualification raised: BrokenTextError",
        )

        long_type = type(
            "E" * 700 + "\n",
            (BrokenTextError,),
            {},
        )
        with mock.patch.object(
            qualification,
            "_qualify_mpi_cluster_advisory_lock",
            side_effect=long_type(),
        ):
            bounded_type = qualification.qualify_mpi_cluster_advisory_lock(
                object(),
                capability,
                root="unused",
                epoch="b" * 64,
                global_rank=0,
                expected_master_ranks=(0, 1),
                processor_name="node-a",
            )
        self.assertEqual(len(bounded_type.error), 512)
        self.assertNotIn("\n", bounded_type.error)
        self.assertNotIn("\x00", bounded_type.error)

        with mock.patch.object(
            qualification,
            "_qualify_mpi_cluster_advisory_lock",
            side_effect=KeyboardInterrupt("injected process control"),
        ), self.assertRaisesRegex(KeyboardInterrupt, "injected process control"):
            qualification.qualify_mpi_cluster_advisory_lock(
                object(),
                capability,
                root="unused",
                epoch="b" * 64,
                global_rank=0,
                expected_master_ranks=(0, 1),
                processor_name="node-a",
            )

    def test_qualification_input_observation_totalizes_ordinary_failures(self):
        events = []

        def failing_capability_probe():
            events.append("capability")
            raise LookupError("injected filesystem adapter failure")

        def failing_processor_probe():
            events.append("processor")
            raise LookupError("injected processor adapter failure")

        with tempfile.TemporaryDirectory() as tmp:
            capability = probe_shared_state_filesystem(tmp, timeout=2.0)
            clean = qualification.observe_cluster_lock_qualification_inputs(
                capability_probe=lambda: capability,
                processor_name_probe=lambda: "node-a",
            )
            failed = qualification.observe_cluster_lock_qualification_inputs(
                capability_probe=failing_capability_probe,
                processor_name_probe=failing_processor_probe,
            )
            existing = qualification.observe_cluster_lock_qualification_inputs(
                capability,
                processor_name_probe=lambda: "node-a",
                local_error="pre-renewal heartbeat failed\n",
            )
            invalid = qualification.observe_cluster_lock_qualification_inputs(
                object(),
                processor_name_probe=lambda: object(),
            )

            observed_processor_failure = (
                qualification.observe_cluster_lock_qualification_inputs(
                    capability,
                    processor_name_probe=lambda: (_ for _ in ()).throw(
                        LookupError("injected processor adapter failure")
                    ),
                )
            )
            consensus_failure = (
                qualification.qualify_mpi_cluster_advisory_lock(
                    _MessageBus(1).communicator(0),
                    observed_processor_failure.capability,
                    root=tmp,
                    epoch="b" * 64,
                    global_rank=0,
                    expected_master_ranks=(0,),
                    processor_name=observed_processor_failure.processor_name,
                    local_error=observed_processor_failure.error,
                    timeout=0.025,
                )
            )

            bus = _MessageBus(2)
            observations = (
                qualification.observe_cluster_lock_qualification_inputs(
                    capability,
                    processor_name_probe=lambda: "node-a",
                ),
                observed_processor_failure,
            )
            distributed_results = [None, None]
            distributed_errors = []

            def run(rank):
                observation = observations[rank]
                try:
                    distributed_results[rank] = (
                        qualification.qualify_mpi_cluster_advisory_lock(
                            bus.communicator(rank),
                            observation.capability,
                            root=tmp,
                            epoch="b" * 64,
                            global_rank=rank,
                            expected_master_ranks=(0, 1),
                            processor_name=observation.processor_name,
                            local_error=observation.error,
                            timeout=1.0,
                        )
                    )
                except BaseException as error:
                    distributed_errors.append(error)

            threads = [threading.Thread(target=run, args=(rank,))
                       for rank in (0, 1)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(2.0)

        self.assertIs(clean.capability, capability)
        self.assertEqual(clean.processor_name, "node-a")
        self.assertEqual(clean.error, "")
        self.assertEqual(events, ["capability", "processor"])
        self.assertIsNone(failed.capability)
        self.assertEqual(failed.processor_name, "")
        self.assertIn(
            "filesystem capability probe raised: "
            "injected filesystem adapter failure",
            failed.error,
        )
        self.assertIn(
            "processor identity probe raised: "
            "injected processor adapter failure",
            failed.error,
        )
        self.assertLessEqual(len(failed.error), 512)
        self.assertNotIn("\n", failed.error)
        self.assertNotIn("\x00", failed.error)
        self.assertIs(existing.capability, capability)
        self.assertEqual(existing.processor_name, "node-a")
        self.assertEqual(existing.error, "pre-renewal heartbeat failed ")
        self.assertIsNone(invalid.capability)
        self.assertEqual(invalid.processor_name, "")
        self.assertIn("invalid result", invalid.error)
        self.assertFalse(consensus_failure.clean)
        self.assertFalse(consensus_failure.verified)
        self.assertEqual(consensus_failure.members, ((0, ""),))
        self.assertIn(
            "injected processor adapter failure",
            consensus_failure.error,
        )
        self.assertNotIn("malformed cluster membership", consensus_failure.error)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(distributed_errors, [])
        self.assertTrue(all(result is not None for result in distributed_results))
        for result in distributed_results:
            self.assertFalse(result.clean)
            self.assertFalse(result.verified)
            self.assertEqual(result.members, ((0, "node-a"), (1, "")))
            self.assertIn(
                "master 1 local probe failed: processor identity probe raised: "
                "injected processor adapter failure",
                result.error,
            )

        long_failure = qualification.observe_cluster_lock_qualification_inputs(
            capability,
            processor_name_probe=lambda: (_ for _ in ()).throw(
                LookupError("x\n\x00" * 300)
            ),
        )
        self.assertEqual(len(long_failure.error), 512)
        self.assertNotIn("\n", long_failure.error)
        self.assertNotIn("\x00", long_failure.error)

        with self.assertRaisesRegex(KeyboardInterrupt, "capability signal"):
            qualification.observe_cluster_lock_qualification_inputs(
                capability_probe=lambda: (_ for _ in ()).throw(
                    KeyboardInterrupt("capability signal")
                ),
                processor_name_probe=lambda: "node-a",
            )
        with self.assertRaisesRegex(KeyboardInterrupt, "processor signal"):
            qualification.observe_cluster_lock_qualification_inputs(
                capability,
                processor_name_probe=lambda: (_ for _ in ()).throw(
                    KeyboardInterrupt("processor signal")
                ),
            )

    def test_work_lease_heartbeat_observation_is_total_and_bounded(self):
        first = "a" * 64
        second = "b" * 64
        clean = qualification.observe_work_lease_heartbeat(
            lambda: (first, second)
        )
        self.assertEqual(clean.lost_lease_count, 2)
        self.assertEqual(clean.error, "")

        calls = 0

        def failed_heartbeat():
            nonlocal calls
            calls += 1
            raise LookupError("injected heartbeat adapter failure")

        failed = qualification.observe_work_lease_heartbeat(failed_heartbeat)
        self.assertEqual(calls, 1)
        self.assertEqual(failed.lost_lease_count, 0)
        self.assertEqual(
            failed.error,
            "work lease heartbeat failed: injected heartbeat adapter failure",
        )

        for invalid in (
            [first],
            (first, first),
            ("not-a-work-hash",),
            (["unhashable-work-hash"],),
        ):
            with self.subTest(invalid=invalid):
                observed = qualification.observe_work_lease_heartbeat(
                    lambda invalid=invalid: invalid
                )
                self.assertEqual(observed.lost_lease_count, 0)
                self.assertEqual(
                    observed.error,
                    "work lease heartbeat returned an invalid result",
                )

        bounded = qualification.observe_work_lease_heartbeat(
            lambda: (_ for _ in ()).throw(LookupError("x\n\x00" * 300))
        )
        self.assertEqual(len(bounded.error), 512)
        self.assertNotIn("\n", bounded.error)
        self.assertNotIn("\x00", bounded.error)

        class BrokenTextError(Exception):
            def __str__(self):
                raise RuntimeError("broken heartbeat exception rendering")

        rendered = qualification.observe_work_lease_heartbeat(
            lambda: (_ for _ in ()).throw(BrokenTextError())
        )
        self.assertEqual(
            rendered.error,
            "work lease heartbeat failed: BrokenTextError",
        )
        with self.assertRaisesRegex(KeyboardInterrupt, "heartbeat signal"):
            qualification.observe_work_lease_heartbeat(
                lambda: (_ for _ in ()).throw(
                    KeyboardInterrupt("heartbeat signal")
                )
            )

    def test_runtime_renewal_controller_fences_requests_and_records_metrics(self):
        epoch = "c" * 64
        root = qualification.ClusterLockRenewalController(
            epoch=epoch,
            interval=5.0,
            timeout=2.0,
            completed_at=10.0,
            require_configuration_consensus=False,
            expected_master_ranks=(0, 1),
        )
        peer = qualification.ClusterLockRenewalController(
            epoch=epoch,
            interval=5.0,
            timeout=2.0,
            completed_at=10.0,
            require_configuration_consensus=False,
            expected_master_ranks=(0, 1),
        )
        self.assertFalse(root.due(14.999))
        self.assertFalse(root.due(15.0, safe_to_start=False))
        self.assertFalse(root.due(15.0, wall_remaining=3.0))
        self.assertTrue(root.due(15.0, wall_remaining=3.001))

        request = root.begin_request()
        generation, error = peer.accept_request(request)
        self.assertEqual((generation, error), (1, ""))
        result = qualification.replace(
            self._successful_renewal_result(epoch),
            elapsed=0.25,
        )
        self.assertTrue(root.complete(1, result, completed_at=15.25))
        self.assertTrue(peer.complete(1, result, completed_at=15.25))
        self.assertEqual(root.snapshot(), {
            "schema": "symcc-cluster-lock-renewal-metrics-v2",
            "interval": 5.0,
            "jitter_fraction": 0.0,
            "maximum_interval": 5.0,
            "last_scheduled_interval": 5.0,
            "next_scheduled_interval": 5.0,
            "timeout": 2.0,
            "generation": 1,
            "in_flight_generation": 0,
            "attempts": 1,
            "successes": 1,
            "failures": 0,
            "total_elapsed": 0.25,
            "last_elapsed": 0.25,
        })
        _, stale_error = peer.accept_request(request)
        self.assertIn("stale", stale_error)

        tampered = root.begin_request()
        tampered["token"] = "0" * 64
        _, token_error = peer.accept_request(tampered)
        self.assertIn("token mismatch", token_error)

    def test_runtime_renewal_rejects_legacy_unclosed_lock_evidence(self):
        controller = qualification.ClusterLockRenewalController(
            epoch=hashlib.sha256(b"legacy-lock-evidence").hexdigest(),
            interval=5.0,
            timeout=2.0,
            completed_at=0.0,
            require_configuration_consensus=False,
        )
        request = controller.begin_request()
        legacy = qualification.ClusterLockQualificationResult(
            clean=True,
            verified=True,
            capability=mock.Mock(
                cluster_lock_verified=True,
                probe_scope="cross-host-mpi-lock-v1",
                cluster_lock_identity_checks=0,
            ),
            members=((0, "node-a"), (1, "node-b")),
            representatives=(0, 1),
            rounds=2,
            contention_checks=2,
            release_checks=2,
            elapsed=0.1,
            identity_checks=0,
        )

        self.assertFalse(controller.complete(
            request["generation"],
            legacy,
            completed_at=5.1,
        ))
        self.assertEqual(controller.snapshot()["failures"], 1)

    def test_runtime_renewal_rejects_previous_generation_result_replay(self):
        epoch = hashlib.sha256(b"stale-result-replay").hexdigest()
        controller = qualification.ClusterLockRenewalController(
            epoch=epoch,
            interval=5.0,
            timeout=2.0,
            completed_at=0.0,
            require_configuration_consensus=False,
            expected_master_ranks=(0, 1),
        )
        first_request = controller.begin_request()
        first_result = self._successful_renewal_result(
            epoch, first_request["generation"])
        self.assertTrue(controller.complete(
            first_request["generation"],
            first_result,
            completed_at=5.1,
        ))

        second_request = controller.begin_request()
        self.assertFalse(controller.complete(
            second_request["generation"],
            first_result,
            completed_at=10.2,
        ))
        metrics = controller.snapshot()
        self.assertEqual(metrics["generation"], 2)
        self.assertEqual(metrics["attempts"], 2)
        self.assertEqual(metrics["successes"], 1)
        self.assertEqual(metrics["failures"], 1)

    def test_runtime_renewal_rejects_spliced_proof_fields(self):
        epoch = hashlib.sha256(b"spliced-result-fields").hexdigest()
        valid = self._successful_renewal_result(epoch)
        assert valid.capability is not None
        cases = {
            "zero-round-result": qualification.replace(valid, rounds=0),
            "different-members": qualification.replace(
                valid,
                members=((0, "node-a"), (2, "node-b")),
            ),
            "different-capability-count": qualification.replace(
                valid,
                capability=qualification.replace(
                    valid.capability,
                    cluster_lock_contention_checks=1,
                ),
            ),
            "different-capability-root": qualification.replace(
                valid,
                capability=qualification.replace(
                    valid.capability,
                    root=valid.capability.root + "-foreign",
                ),
            ),
        }
        for name, result in cases.items():
            with self.subTest(name=name):
                controller = qualification.ClusterLockRenewalController(
                    epoch=epoch,
                    interval=5.0,
                    timeout=2.0,
                    completed_at=0.0,
                    require_configuration_consensus=False,
                    expected_master_ranks=(0, 1),
                    expected_capability=valid.capability,
                )
                request = controller.begin_request()
                self.assertFalse(controller.complete(
                    request["generation"],
                    result,
                    completed_at=5.1,
                ))
                self.assertEqual(controller.snapshot()["failures"], 1)

    def test_runtime_renewal_totalizes_malformed_completion_fields(self):
        epoch = hashlib.sha256(b"malformed-renewal-completion").hexdigest()
        valid = self._successful_renewal_result(epoch)
        cases = {
            "non-iterable-members": qualification.replace(
                valid, members=object()),
            "malformed-member-tuple": qualification.replace(
                valid, members=(0,)),
            "non-iterable-representatives": qualification.replace(
                valid, representatives=object()),
            "non-numeric-elapsed": qualification.replace(
                valid, elapsed=object()),
            "nan-elapsed": qualification.replace(
                valid, elapsed=float("nan")),
        }
        for name, result in cases.items():
            with self.subTest(name=name):
                controller = qualification.ClusterLockRenewalController(
                    epoch=epoch,
                    interval=5.0,
                    timeout=2.0,
                    completed_at=0.0,
                    require_configuration_consensus=False,
                )
                request = controller.begin_request()
                self.assertFalse(controller.complete(
                    request["generation"],
                    result,
                    completed_at=5.1,
                ))
                metrics = controller.snapshot()
                self.assertEqual(metrics["generation"], 1)
                self.assertEqual(metrics["in_flight_generation"], 0)
                self.assertEqual(metrics["attempts"], 1)
                self.assertEqual(metrics["successes"], 0)
                self.assertEqual(metrics["failures"], 1)
                self.assertEqual(
                    metrics["attempts"],
                    metrics["successes"] + metrics["failures"],
                )

    def test_runtime_renewal_completion_is_failure_atomic(self):
        epoch = hashlib.sha256(b"failure-atomic-completion").hexdigest()
        valid = self._successful_renewal_result(epoch)

        for name, patcher in (
            (
                "validator",
                mock.patch.object(
                    qualification,
                    "_qualification_result_matches_request",
                    side_effect=RuntimeError("injected validator failure"),
                ),
            ),
            (
                "scheduler",
                mock.patch.object(
                    qualification,
                    "_scheduled_renewal_interval",
                    side_effect=RuntimeError("injected scheduler failure"),
                ),
            ),
        ):
            with self.subTest(name=name):
                controller = qualification.ClusterLockRenewalController(
                    epoch=epoch,
                    interval=5.0,
                    timeout=2.0,
                    completed_at=0.0,
                    require_configuration_consensus=False,
                )
                request = controller.begin_request()
                before = controller.snapshot()
                with patcher, self.assertRaisesRegex(
                    RuntimeError, f"injected {name} failure"
                ):
                    controller.complete(
                        request["generation"],
                        valid,
                        completed_at=5.1,
                    )
                self.assertEqual(controller.snapshot(), before)

        controller = qualification.ClusterLockRenewalController(
            epoch=epoch,
            interval=5.0,
            timeout=2.0,
            completed_at=0.0,
            require_configuration_consensus=False,
        )
        controller.begin_request()
        before = controller.snapshot()
        with self.assertRaisesRegex(RuntimeError, "generation mismatch"):
            controller.complete(True, valid, completed_at=5.1)
        self.assertEqual(controller.snapshot(), before)

    def test_runtime_renewal_completion_boundary_contains_rank_exceptions(self):
        epoch = hashlib.sha256(b"contained-renewal-completion").hexdigest()

        controller = qualification.ClusterLockRenewalController(
            epoch=epoch,
            interval=5.0,
            timeout=2.0,
            completed_at=0.0,
            require_configuration_consensus=False,
        )
        request = controller.begin_request()
        failed_result = qualification.ClusterLockQualificationResult(
            clean=False,
            verified=False,
            capability=None,
            members=(),
            representatives=(),
            rounds=0,
            contention_checks=0,
            release_checks=0,
            elapsed=0.1,
            qualification_generation=request["generation"],
        )
        successful, error = qualification.complete_cluster_lock_renewal(
            controller,
            request["generation"],
            failed_result,
            completed_at=5.1,
        )
        self.assertFalse(successful)
        self.assertEqual(error, "")
        self.assertEqual(controller.snapshot()["failures"], 1)

        overflow = qualification.ClusterLockRenewalController(
            epoch=epoch,
            interval=5.0,
            timeout=2.0,
            completed_at=0.0,
            require_configuration_consensus=False,
        )
        overflow.total_elapsed = float.fromhex("0x1.fffffffffffffp+1023")
        overflow_request = overflow.begin_request()
        overflow_result = qualification.replace(
            failed_result,
            elapsed=float.fromhex("0x1.fffffffffffffp+1023"),
            qualification_generation=overflow_request["generation"],
        )
        before = overflow.snapshot()
        successful, error = qualification.complete_cluster_lock_renewal(
            overflow,
            overflow_request["generation"],
            overflow_result,
            completed_at=5.1,
        )
        self.assertFalse(successful)
        self.assertIn("elapsed total overflow", error)
        self.assertEqual(overflow.snapshot(), before)

        unexpected = qualification.ClusterLockRenewalController(
            epoch=epoch,
            interval=5.0,
            timeout=2.0,
            completed_at=0.0,
            require_configuration_consensus=False,
        )
        unexpected_request = unexpected.begin_request()
        before = unexpected.snapshot()
        with mock.patch.object(
            qualification,
            "_scheduled_renewal_interval",
            side_effect=LookupError("injected rank-local failure"),
        ):
            successful, error = qualification.complete_cluster_lock_renewal(
                unexpected,
                unexpected_request["generation"],
                failed_result,
                completed_at=5.1,
            )
        self.assertFalse(successful)
        self.assertEqual(error, "injected rank-local failure")
        self.assertEqual(unexpected.snapshot(), before)

        self.assertEqual(
            qualification.complete_cluster_lock_renewal(
                object(), 1, failed_result, completed_at=5.1),
            (False, "invalid cluster lock renewal controller"),
        )

    def test_renewal_completion_observes_clock_inside_total_boundary(self):
        epoch = hashlib.sha256(b"contained-completion-clock").hexdigest()

        def controller_and_result():
            controller = qualification.ClusterLockRenewalController(
                epoch=epoch,
                interval=5.0,
                timeout=2.0,
                completed_at=0.0,
                require_configuration_consensus=False,
            )
            generation = controller.begin_request()["generation"]
            result = qualification.ClusterLockQualificationResult(
                clean=False,
                verified=False,
                capability=None,
                members=(),
                representatives=(),
                rounds=0,
                contention_checks=0,
                release_checks=0,
                elapsed=0.1,
                qualification_generation=generation,
            )
            return controller, generation, result

        completed, generation, result = controller_and_result()
        successful, error = qualification.complete_cluster_lock_renewal(
            completed,
            generation,
            result,
            monotonic=lambda: 5.1,
        )
        self.assertFalse(successful)
        self.assertEqual(error, "")
        self.assertEqual(completed.snapshot()["attempts"], 1)
        self.assertEqual(completed.snapshot()["failures"], 1)

        clock_failure, generation, result = controller_and_result()
        before = clock_failure.snapshot()
        clock_calls = 0

        def failed_clock():
            nonlocal clock_calls
            clock_calls += 1
            raise LookupError("injected completion clock failure")

        successful, error = qualification.complete_cluster_lock_renewal(
            clock_failure,
            generation,
            result,
            monotonic=failed_clock,
        )
        self.assertFalse(successful)
        self.assertEqual(error, "injected completion clock failure")
        self.assertEqual(clock_calls, 1)
        self.assertEqual(clock_failure.snapshot(), before)

        with self.assertRaisesRegex(KeyboardInterrupt, "completion signal"):
            qualification.complete_cluster_lock_renewal(
                clock_failure,
                generation,
                result,
                monotonic=lambda: (_ for _ in ()).throw(
                    KeyboardInterrupt("completion signal")
                ),
            )
        self.assertEqual(clock_failure.snapshot(), before)

    def test_runtime_renewal_jitter_is_bounded_reproducible_and_delay_only(self):
        epoch = hashlib.sha256(b"job-a").hexdigest()
        peer = qualification.ClusterLockRenewalController(
            epoch=epoch,
            interval=60.0,
            timeout=5.0,
            completed_at=10.0,
            jitter_fraction=0.1,
            require_configuration_consensus=False,
        )
        root = qualification.ClusterLockRenewalController(
            epoch=epoch,
            interval=60.0,
            timeout=5.0,
            completed_at=10.0,
            jitter_fraction=0.1,
            require_configuration_consensus=False,
        )
        scheduled = root.scheduled_interval
        self.assertEqual(scheduled, peer.scheduled_interval)
        self.assertGreaterEqual(scheduled, 60.0)
        self.assertLess(scheduled, 66.0)
        self.assertFalse(root.due(10.0 + scheduled - 1e-9))
        self.assertTrue(root.due(10.0 + scheduled))
        self.assertEqual(
            qualification._scheduled_renewal_interval(
                epoch, 1, 60.0, 0.0),
            60.0,
        )
        self.assertNotEqual(
            qualification._scheduled_renewal_interval(
                epoch, 1, 60.0, 0.1),
            qualification._scheduled_renewal_interval(
                epoch, 2, 60.0, 0.1),
        )
        self.assertNotEqual(
            scheduled,
            qualification._scheduled_renewal_interval(
                hashlib.sha256(b"job-b").hexdigest(),
                1,
                60.0,
                0.1,
            ),
        )
        maximum_digest = mock.Mock()
        maximum_digest.digest.return_value = b"\xff" * 32
        with mock.patch.object(
            qualification.hashlib,
            "sha256",
            return_value=maximum_digest,
        ):
            strict_upper = qualification._scheduled_renewal_interval(
                epoch,
                1,
                60.0,
                0.1,
            )
            large_strict_upper = qualification._scheduled_renewal_interval(
                epoch,
                1,
                1e300,
                0.5,
            )
        self.assertGreater(strict_upper, 65.999999)
        self.assertLess(strict_upper, 66.0)
        self.assertGreaterEqual(large_strict_upper, 1e300)
        self.assertLess(large_strict_upper, 1.5e300)
        self.assertEqual(
            qualification._scheduled_renewal_interval(
                epoch,
                1,
                1.0,
                float.fromhex("0x0.0000000000001p-1022"),
            ),
            1.0,
        )

    def test_runtime_renewal_jitter_rejects_unbounded_values(self):
        for value in (-0.001, 0.500001, float("inf"), float("nan"), "bad"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                qualification.ClusterLockRenewalController(
                    epoch="f" * 64,
                    interval=60.0,
                    timeout=5.0,
                    completed_at=0.0,
                    jitter_fraction=value,
                )

    def test_runtime_renewal_jitter_hash_is_cached_between_generations(self):
        scheduler = qualification._scheduled_renewal_interval
        with mock.patch.object(
            qualification,
            "_scheduled_renewal_interval",
            wraps=scheduler,
        ) as instrumented:
            controller = qualification.ClusterLockRenewalController(
                epoch=hashlib.sha256(b"cached-job").hexdigest(),
                interval=60.0,
                timeout=5.0,
                completed_at=0.0,
                jitter_fraction=0.1,
                require_configuration_consensus=False,
            )
            self.assertEqual(instrumented.call_count, 1)
            for now in (0.0, 1.0, 30.0, 59.0, 60.0, 66.0):
                controller.due(now)
            controller.snapshot()
            self.assertEqual(instrumented.call_count, 1)
            request = controller.begin_request()
            result = self._successful_renewal_result(
                controller.epoch,
                request["generation"],
            )
            self.assertTrue(controller.complete(
                request["generation"],
                result,
                completed_at=66.0,
            ))
            self.assertEqual(instrumented.call_count, 2)
            for now in (66.0, 90.0, 120.0, 132.0):
                controller.due(now)
                controller.snapshot()
            self.assertEqual(instrumented.call_count, 2)

    def test_runtime_renewal_jitter_dispersion_avoids_one_fixed_bucket(self):
        bucket_width = 0.1
        bucket_counts = defaultdict(int)
        delays = []
        for job in range(1024):
            epoch = hashlib.sha256(f"job-{job}".encode()).hexdigest()
            delay = qualification._scheduled_renewal_interval(
                epoch,
                1,
                60.0,
                0.1,
            )
            delays.append(delay)
            bucket_counts[int((delay - 60.0) / bucket_width)] += 1

        self.assertTrue(all(60.0 <= delay < 66.0 for delay in delays))
        self.assertGreater(len(set(delays)), 1000)
        self.assertGreater(len(bucket_counts), 55)
        self.assertLess(max(bucket_counts.values()), 40)

    def test_runtime_renewal_configuration_reaches_bounded_consensus(self):
        consistent = self._run_renewal_configuration_consensus((
            {"interval": 60.0, "timeout": 5.0, "jitter_fraction": -0.0},
            {"interval": 60.0, "timeout": 5.0, "jitter_fraction": 0.0},
            {"interval": 60.0, "timeout": 5.0, "jitter_fraction": 0.0},
        ))
        self.assertTrue(all(not error for _, error in consistent))
        self.assertEqual(len({fingerprint for fingerprint, _ in consistent}), 1)
        self.assertTrue(qualification._valid_token(consistent[0][0]))

    def test_runtime_renewal_configuration_rejects_parameter_drift(self):
        baseline = {
            "interval": 60.0,
            "timeout": 5.0,
            "jitter_fraction": 0.1,
        }
        for field, replacement in (
            ("interval", 61.0),
            ("timeout", 4.0),
            ("jitter_fraction", 0.2),
        ):
            peer = dict(baseline)
            peer[field] = replacement
            with self.subTest(field=field):
                results = self._run_renewal_configuration_consensus(
                    (baseline, peer))
                self.assertTrue(all(not fingerprint
                                    for fingerprint, _ in results))
                self.assertTrue(all("configuration mismatch" in error
                                    for _, error in results))

    def test_runtime_renewal_request_binds_consensus_configuration(self):
        epoch = hashlib.sha256(b"bound-renewal-config").hexdigest()
        root = qualification.ClusterLockRenewalController(
            epoch=epoch,
            interval=60.0,
            timeout=5.0,
            completed_at=0.0,
            jitter_fraction=0.1,
            require_configuration_consensus=False,
        )
        peer = qualification.ClusterLockRenewalController(
            epoch=epoch,
            interval=61.0,
            timeout=5.0,
            completed_at=0.0,
            jitter_fraction=0.1,
            require_configuration_consensus=False,
        )
        request = root.begin_request()
        self.assertEqual(request["schema"], "symcc-cluster-lock-renewal-v2")
        self.assertEqual(
            request["configuration"],
            root.configuration_fingerprint,
        )
        generation, error = peer.accept_request(request)
        self.assertEqual(generation, 0)
        self.assertIn("configuration mismatch", error)

        mutated = qualification.ClusterLockRenewalController(
            epoch=epoch,
            interval=60.0,
            timeout=5.0,
            completed_at=0.0,
            jitter_fraction=0.1,
            require_configuration_consensus=False,
        )
        mutated.jitter_fraction = 0.2
        with self.assertRaisesRegex(RuntimeError, "configuration changed"):
            mutated.begin_request()

    def test_runtime_renewal_requires_configuration_consensus_by_default(self):
        epoch = hashlib.sha256(b"consensus-typestate").hexdigest()
        controller = qualification.ClusterLockRenewalController(
            epoch=epoch,
            interval=60.0,
            timeout=5.0,
            completed_at=0.0,
            jitter_fraction=0.1,
        )
        self.assertFalse(controller.configuration_consensus_established)
        self.assertFalse(controller.due(66.0))
        with self.assertRaisesRegex(RuntimeError, "not established"):
            controller.begin_request()

        fingerprint, error = (
            qualification.qualify_cluster_lock_renewal_configuration(
                _MessageBus(1).communicator(0),
                controller,
                timeout=0.1,
            )
        )
        self.assertEqual(error, "")
        self.assertEqual(fingerprint, controller.configuration_fingerprint)
        self.assertTrue(controller.configuration_consensus_established)
        self.assertTrue(controller.due(66.0))
        request = controller.begin_request()

        unqualified_peer = qualification.ClusterLockRenewalController(
            epoch=epoch,
            interval=60.0,
            timeout=5.0,
            completed_at=0.0,
            jitter_fraction=0.1,
        )
        generation, peer_error = unqualified_peer.accept_request(request)
        self.assertEqual(generation, 0)
        self.assertIn("not established", peer_error)
        with self.assertRaises(ValueError):
            qualification.ClusterLockRenewalController(
                epoch=epoch,
                interval=60.0,
                timeout=5.0,
                completed_at=0.0,
                require_configuration_consensus=1,
            )
        positional = qualification.ClusterLockRenewalController(
            epoch,
            60.0,
            5.0,
            0.0,
            0.1,
            7,
        )
        self.assertEqual(positional.generation, 7)
        self.assertTrue(positional.require_configuration_consensus)

    def test_runtime_renewal_rejects_in_flight_configuration_mutation(self):
        controller = qualification.ClusterLockRenewalController(
            epoch=hashlib.sha256(b"in-flight-renewal-config").hexdigest(),
            interval=60.0,
            timeout=5.0,
            completed_at=0.0,
            jitter_fraction=0.1,
        )
        fingerprint, error = (
            qualification.qualify_cluster_lock_renewal_configuration(
                _MessageBus(1).communicator(0),
                controller,
                timeout=0.1,
            )
        )
        self.assertEqual((fingerprint, error), (
            controller.configuration_fingerprint, ""))
        request = controller.begin_request()
        controller.jitter_fraction = 0.2
        self.assertFalse(controller.configuration_consensus_established)
        result = qualification.ClusterLockQualificationResult(
            clean=True,
            verified=True,
            capability=mock.Mock(
                cluster_lock_verified=True,
                probe_scope="cross-host-mpi-lock-v2",
                cluster_lock_identity_checks=2,
            ),
            members=((0, "node-a"), (1, "node-b")),
            representatives=(0, 1),
            rounds=2,
            contention_checks=2,
            release_checks=2,
            elapsed=0.1,
            identity_checks=2,
        )

        with self.assertRaisesRegex(RuntimeError, "changed in flight"):
            controller.complete(
                request["generation"],
                result,
                completed_at=60.1,
            )

        metrics = controller.snapshot()
        self.assertEqual(metrics["generation"], 0)
        self.assertEqual(metrics["in_flight_generation"], 1)
        self.assertEqual(metrics["attempts"], 0)
        self.assertEqual(metrics["successes"], 0)
        self.assertEqual(metrics["failures"], 0)

    def test_runtime_renewal_configuration_rejects_malformed_record(self):
        controller = qualification.ClusterLockRenewalController(
            epoch=hashlib.sha256(b"malformed-renewal-config").hexdigest(),
            interval=60.0,
            timeout=5.0,
            completed_at=0.0,
            jitter_fraction=0.1,
        )
        malformed = controller.configuration_snapshot()
        malformed["maximum_interval"] = 67.0
        with mock.patch.object(
            qualification,
            "_bounded_master_exchange",
            return_value=((malformed,), ""),
        ):
            fingerprint, error = (
                qualification.qualify_cluster_lock_renewal_configuration(
                    object(),
                    controller,
                )
            )
        self.assertEqual(fingerprint, "")
        self.assertIn("malformed", error)

    def test_runtime_renewal_configuration_times_out_without_peer(self):
        bus = _MessageBus(2)
        controller = qualification.ClusterLockRenewalController(
            epoch=hashlib.sha256(b"missing-config-peer").hexdigest(),
            interval=60.0,
            timeout=5.0,
            completed_at=0.0,
            jitter_fraction=0.1,
        )
        fingerprint, error = (
            qualification.qualify_cluster_lock_renewal_configuration(
                bus.communicator(0),
                controller,
                timeout=0.01,
            )
        )
        self.assertEqual(fingerprint, "")
        self.assertIn("timed out", error)

    def test_runtime_renewal_transport_is_root_authorized_and_nonblocking(self):
        epoch = "d" * 64
        bus = _MessageBus(3)
        controllers = [
            qualification.ClusterLockRenewalController(
                epoch=epoch,
                interval=1.0,
                timeout=2.0,
                completed_at=0.0,
                require_configuration_consensus=False,
            )
            for _ in range(3)
        ]
        generation, sends, error = qualification.begin_cluster_lock_renewal(
            bus.communicator(0),
            controllers[0],
        )
        self.assertEqual((generation, error), (1, ""))
        self.assertEqual(len(sends), 2)
        self.assertTrue(all(send.Test() for send in sends))
        for rank in (1, 2):
            accepted, peer_error, observed = (
                qualification.poll_cluster_lock_renewal(
                    bus.communicator(rank),
                    controllers[rank],
                )
            )
            self.assertEqual(
                (accepted, peer_error, observed),
                (1, "", True),
            )
            self.assertEqual(controllers[rank].in_flight_generation, 1)

        rejected, rejected_sends, rejected_error = (
            qualification.begin_cluster_lock_renewal(
                bus.communicator(1),
                qualification.ClusterLockRenewalController(
                    epoch=epoch,
                    interval=1.0,
                    timeout=2.0,
                    completed_at=0.0,
                ),
            )
        )
        self.assertEqual(rejected, 0)
        self.assertEqual(rejected_sends, ())
        self.assertIn("must begin", rejected_error)

    def test_runtime_renewal_transport_surfaces_partial_send_failure(self):
        class PartialFailureCommunicator(_ThreadCommunicator):
            def isend(self, message, *, dest, tag):
                if dest == 2:
                    raise OSError("injected renewal send failure")
                return super().isend(message, dest=dest, tag=tag)

        epoch = "e" * 64
        bus = _MessageBus(3)
        controller = qualification.ClusterLockRenewalController(
            epoch=epoch,
            interval=1.0,
            timeout=2.0,
            completed_at=0.0,
            require_configuration_consensus=False,
        )
        generation, sends, error = qualification.begin_cluster_lock_renewal(
            PartialFailureCommunicator(bus, 0),
            controller,
        )
        self.assertEqual(generation, 1)
        self.assertEqual(len(sends), 1)
        self.assertIn("injected renewal send failure", error)
        accepted, peer_error, observed = qualification.poll_cluster_lock_renewal(
            bus.communicator(1),
            qualification.ClusterLockRenewalController(
                epoch=epoch,
                interval=1.0,
                timeout=2.0,
                completed_at=0.0,
                require_configuration_consensus=False,
            ),
        )
        self.assertEqual((accepted, peer_error, observed), (1, "", True))

    def test_runtime_renewal_begin_transport_is_total_and_preserves_typestate(self):
        epoch = hashlib.sha256(b"total-renewal-begin").hexdigest()

        def controller():
            return qualification.ClusterLockRenewalController(
                epoch=epoch,
                interval=1.0,
                timeout=2.0,
                completed_at=0.0,
                require_configuration_consensus=False,
            )

        class RankFailure:
            def Get_rank(self):
                raise LookupError("injected rank lookup failure")

        before_begin = controller()
        before = before_begin.snapshot()
        generation, sends, error = qualification.begin_cluster_lock_renewal(
            RankFailure(), before_begin)
        self.assertEqual((generation, sends), (0, ()))
        self.assertIn("injected rank lookup failure", error)
        self.assertEqual(before_begin.snapshot(), before)

        class SendFailure:
            def Get_rank(self):
                return 0

            def Get_size(self):
                return 2

            def isend(self, message, *, dest, tag):
                raise LookupError("injected send adapter failure")

        after_begin = controller()
        generation, sends, error = qualification.begin_cluster_lock_renewal(
            SendFailure(), after_begin)
        self.assertEqual((generation, sends), (1, ()))
        self.assertEqual(after_begin.in_flight_generation, 1)
        self.assertIn("injected send adapter failure", error)

        interrupted_prepare = controller()

        def fail_after_generation_commit():
            interrupted_prepare.in_flight_generation = 1
            raise LookupError("injected request preparation failure")

        with mock.patch.object(
            interrupted_prepare,
            "begin_request",
            side_effect=fail_after_generation_commit,
        ):
            generation, sends, error = (
                qualification.begin_cluster_lock_renewal(
                    SendFailure(), interrupted_prepare)
            )
        self.assertEqual((generation, sends), (1, ()))
        self.assertEqual(interrupted_prepare.in_flight_generation, 1)
        self.assertIn("injected request preparation failure", error)

        class BrokenTextError(Exception):
            def __str__(self):
                raise RuntimeError("broken transport exception rendering")

        class BrokenSend(SendFailure):
            def isend(self, message, *, dest, tag):
                raise BrokenTextError()

        broken = controller()
        generation, sends, error = qualification.begin_cluster_lock_renewal(
            BrokenSend(), broken)
        self.assertEqual((generation, sends), (1, ()))
        self.assertEqual(
            error,
            "cluster lock renewal request send failed: BrokenTextError",
        )

        class LongSend(SendFailure):
            def isend(self, message, *, dest, tag):
                raise LookupError("x\n\x00" * 300)

        bounded = controller()
        generation, sends, error = qualification.begin_cluster_lock_renewal(
            LongSend(), bounded)
        self.assertEqual((generation, sends), (1, ()))
        self.assertEqual(len(error), 512)
        self.assertNotIn("\n", error)
        self.assertNotIn("\x00", error)

        class InterruptedSend(SendFailure):
            def isend(self, message, *, dest, tag):
                raise KeyboardInterrupt("renewal send signal")

        interrupted = controller()
        with self.assertRaisesRegex(KeyboardInterrupt, "renewal send signal"):
            qualification.begin_cluster_lock_renewal(
                InterruptedSend(), interrupted)
        self.assertEqual(interrupted.in_flight_generation, 1)

    def test_runtime_renewal_poll_transport_is_total_and_classified(self):
        epoch = hashlib.sha256(b"total-renewal-poll").hexdigest()

        def controller():
            return qualification.ClusterLockRenewalController(
                epoch=epoch,
                interval=1.0,
                timeout=2.0,
                completed_at=0.0,
                require_configuration_consensus=False,
            )

        class PeerCommunicator:
            def Get_rank(self):
                return 1

            def Get_size(self):
                return 2

            def iprobe(self, *, source, tag):
                raise LookupError("injected probe adapter failure")

        peer = controller()
        before = peer.snapshot()
        generation, error, observed = (
            qualification.poll_cluster_lock_renewal(
                PeerCommunicator(), peer)
        )
        self.assertEqual((generation, observed), (0, False))
        self.assertIn("injected probe adapter failure", error)
        self.assertEqual(peer.snapshot(), before)

        class ReceiveFailure(PeerCommunicator):
            def iprobe(self, *, source, tag):
                return True

            def recv(self, *, source, tag):
                raise LookupError("injected receive adapter failure")

        generation, error, observed = (
            qualification.poll_cluster_lock_renewal(
                ReceiveFailure(), controller())
        )
        self.assertEqual((generation, observed), (0, False))
        self.assertIn("injected receive adapter failure", error)

        class ReceivedRequest(ReceiveFailure):
            def recv(self, *, source, tag):
                return {}

        malformed = controller()
        generation, error, observed = (
            qualification.poll_cluster_lock_renewal(
                ReceivedRequest(), malformed)
        )
        self.assertEqual((generation, observed), (0, True))
        self.assertIn("malformed", error)

        admission_failure = controller()
        with mock.patch.object(
            admission_failure,
            "accept_request",
            side_effect=LookupError("injected admission failure"),
        ):
            generation, error, observed = (
                qualification.poll_cluster_lock_renewal(
                    ReceivedRequest(), admission_failure)
            )
        self.assertEqual((generation, observed), (0, False))
        self.assertIn("injected admission failure", error)

        class InterruptedProbe(PeerCommunicator):
            def iprobe(self, *, source, tag):
                raise KeyboardInterrupt("renewal poll signal")

        with self.assertRaisesRegex(KeyboardInterrupt, "renewal poll signal"):
            qualification.poll_cluster_lock_renewal(
                InterruptedProbe(), controller())

    def test_runtime_renewal_delivery_observation_is_one_shot_and_total(self):
        class Request:
            def __init__(self, outcome):
                self.outcome = outcome
                self.calls = 0

            def Test(self):
                self.calls += 1
                if isinstance(self.outcome, BaseException):
                    raise self.outcome
                return self.outcome

        completed = Request(True)
        incomplete = Request(False)
        uncertain = Request(LookupError("injected completion lookup failure"))
        observation = qualification.observe_cluster_lock_renewal_delivery((
            completed,
            incomplete,
            uncertain,
        ))
        self.assertEqual(observation.total_count, 3)
        self.assertEqual(observation.completed_count, 1)
        self.assertEqual(observation.incomplete_count, 1)
        self.assertEqual(observation.uncertain_count, 1)
        self.assertIn("injected completion lookup failure", observation.error)
        self.assertEqual(
            (completed.calls, incomplete.calls, uncertain.calls),
            (1, 1, 1),
        )

        duplicate = Request(True)
        observation = qualification.observe_cluster_lock_renewal_delivery(
            (duplicate, duplicate))
        self.assertEqual(observation.total_count, 2)
        self.assertEqual(observation.uncertain_count, 2)
        self.assertIn("duplicate", observation.error)
        self.assertEqual(duplicate.calls, 0)

        invalid = qualification.observe_cluster_lock_renewal_delivery([])
        self.assertEqual(invalid.total_count, 0)
        self.assertIn("invalid", invalid.error)

        bounded = qualification.observe_cluster_lock_renewal_delivery((
            Request(LookupError("x\n\x00" * 300)),
        ))
        self.assertEqual(len(bounded.error), 512)
        self.assertNotIn("\n", bounded.error)
        self.assertNotIn("\x00", bounded.error)

        interrupted = Request(KeyboardInterrupt("delivery signal"))
        with self.assertRaisesRegex(KeyboardInterrupt, "delivery signal"):
            qualification.observe_cluster_lock_renewal_delivery((interrupted,))
        self.assertEqual(interrupted.calls, 1)


if __name__ == "__main__":
    unittest.main()
