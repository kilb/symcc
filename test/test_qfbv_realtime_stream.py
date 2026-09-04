#!/usr/bin/env python3
# RUN: python3 %s

import copy
import ctypes
import hashlib
import itertools
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

from cadical_qfbv_backend import PersistentCadicalQfbvSolver  # noqa: E402
from qfbv_incremental_proof import (  # noqa: E402
    IncrementalProofChecker,
    IncrementalProofStore,
    make_rup_clause_record,
)
from qfbv_incremental_sat import bitblast_qfbv_query  # noqa: E402
from qfbv_realtime_stream import (  # noqa: E402
    CLAUSE_ACTIVITY_PROTOCOL,
    NativeRealtimeCadical,
    RealtimeClauseExchangeSession,
    RealtimeStreamError,
    verify_clause_activity_receipt,
    verify_checked_import_ack,
)
from qfbv_adaptive_exchange import (  # noqa: E402
    AdaptiveProofController,
    AdaptiveProofPolicy,
    verify_adaptive_stream_result,
    verify_controller_snapshot,
)
from qfbv_utility_pairing import (  # noqa: E402
    UtilityPairingController,
    UtilityPairingPolicy,
    verify_pairing_stream_result,
)
from query_store import QueryStore  # noqa: E402
from symcc_query_service import _load_portfolio  # noqa: E402


_FAKE_C = r"""
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

typedef struct { int terminated; } Plain;
const char* ccadical_signature(void) { return "cadical-3.0.1-test"; }
void* ccadical_init(void) { return calloc(1, sizeof(Plain)); }
void ccadical_release(void* p) { free(p); }
void ccadical_add(void* p, int l) { (void)p; (void)l; }
void ccadical_assume(void* p, int l) { (void)p; (void)l; }
int ccadical_solve(void* p) { return ((Plain*)p)->terminated ? 0 : 10; }
int ccadical_val(void* p, int l) { (void)p; return l; }
int ccadical_failed(void* p, int l) { (void)p; (void)l; return 0; }
void ccadical_terminate(void* p) { ((Plain*)p)->terminated = 1; }
void ccadical_set_terminate(void* p, void* s, void* f) {
  (void)p; (void)s; (void)f;
}

typedef struct {
  pthread_mutex_t mutex;
  uint64_t token, generation, ordinal;
  uint64_t enqueued, delivered, rejected, learned_exported, activity_ordinal;
  uint64_t activated_unit, tracked;
  int queued, ack, terminated, learned, learned_enabled, solving;
  int activity, activity_enabled, clause[64], clause_size;
} Stream;

static _Atomic uint64_t solve_started;
static _Atomic int wait_for_import;

uint64_t symcc_test_realtime_solve_started(void) {
  return atomic_load(&solve_started);
}
void symcc_test_realtime_wait_for_import(int enabled) {
  atomic_store(&wait_for_import, enabled != 0);
}

const char* symcc_qfbv_realtime_protocol(void) {
  return "symcc-qfbv-realtime-lidrup-stream-v1";
}
const char* symcc_qfbv_realtime_signature(void) {
  return "symcc-qfbv-realtime-v1|cadical-3.0.1-test";
}
const char* symcc_qfbv_realtime_activity_protocol(void) {
  return "symcc-qfbv-native-clause-activity-v1";
}
void* symcc_qfbv_realtime_init(int length, uint64_t imports,
                               uint64_t literals, uint64_t learned) {
  (void)imports; (void)literals;
  Stream* s = calloc(1, sizeof(Stream));
  if (!s) return NULL;
  pthread_mutex_init(&s->mutex, NULL);
  s->learned_enabled = length > 0 && learned > 0;
  return s;
}
void symcc_qfbv_realtime_release(void* p) {
  Stream* s = p;
  pthread_mutex_destroy(&s->mutex);
  free(s);
}
int symcc_qfbv_realtime_add(void* p, int l) { (void)p; (void)l; return 0; }
int symcc_qfbv_realtime_assume(void* p, int l) { (void)p; (void)l; return 0; }
int symcc_qfbv_realtime_observe(void* p, int m) { (void)p; (void)m; return 0; }
int symcc_qfbv_realtime_solve(void* p) {
  Stream* s = p;
  atomic_fetch_add(&solve_started, 1);
  pthread_mutex_lock(&s->mutex);
  s->generation++;
  s->ordinal = 0;
  s->activity_ordinal = 0;
  s->solving = 1;
  if (s->learned_enabled && !s->learned) {
    s->learned = 1;
    s->learned_exported++;
  }
  pthread_mutex_unlock(&s->mutex);
  int require_import = atomic_load(&wait_for_import);
  int limit = require_import ? 1500 : 250;
  for (int i = 0; i < limit; ++i) {
    usleep(1000);
    pthread_mutex_lock(&s->mutex);
    int consumed = s->queued;
    if (consumed) {
      s->queued = 0;
      s->ack = 1;
      s->ordinal++;
      s->delivered++;
      if (s->activity_enabled) s->tracked++;
      if (s->activity_enabled && s->clause_size > 0) {
        s->activity = 1;
        s->activity_ordinal++;
        s->activated_unit++;
      }
    }
    int stopped = s->terminated;
    pthread_mutex_unlock(&s->mutex);
    if (stopped || (require_import && consumed)) {
      pthread_mutex_lock(&s->mutex);
      s->solving = 0;
      pthread_mutex_unlock(&s->mutex);
      return stopped ? 0 : 10;
    }
  }
  pthread_mutex_lock(&s->mutex);
  s->solving = 0;
  pthread_mutex_unlock(&s->mutex);
  return 10;
}
int symcc_qfbv_realtime_val(void* p, int l) { (void)p; return l; }
int symcc_qfbv_realtime_failed(void* p, int l) { (void)p; (void)l; return 0; }
int symcc_qfbv_realtime_terminate(void* p) {
  Stream* s = p;
  pthread_mutex_lock(&s->mutex);
  s->terminated = 1;
  pthread_mutex_unlock(&s->mutex);
  return 0;
}
int symcc_qfbv_realtime_clear_termination(void* p) {
  Stream* s = p;
  pthread_mutex_lock(&s->mutex);
  s->terminated = 0;
  pthread_mutex_unlock(&s->mutex);
  return 0;
}
int symcc_qfbv_realtime_enqueue(void* p, uint64_t token,
                                const int* literals, int size) {
  Stream* s = p;
  pthread_mutex_lock(&s->mutex);
  if (s->queued || size < 1 || size > 64) {
    s->rejected++;
    pthread_mutex_unlock(&s->mutex);
    return 0;
  }
  s->token = token;
  s->clause_size = size;
  for (int i = 0; i < size; ++i) s->clause[i] = literals[i];
  s->queued = 1;
  s->enqueued++;
  pthread_mutex_unlock(&s->mutex);
  return 1;
}
int symcc_qfbv_realtime_dequeue_ack(void* p, uint64_t* token,
                                    uint64_t* generation, uint64_t* ordinal) {
  Stream* s = p;
  pthread_mutex_lock(&s->mutex);
  if (!s->ack) { pthread_mutex_unlock(&s->mutex); return 0; }
  *token = s->token;
  *generation = s->generation;
  *ordinal = s->ordinal;
  s->ack = 0;
  pthread_mutex_unlock(&s->mutex);
  return 1;
}
int symcc_qfbv_realtime_enable_activity(void* p, int enabled) {
  Stream* s = p;
  pthread_mutex_lock(&s->mutex);
  s->activity_enabled = enabled;
  s->activity = 0;
  s->tracked = 0;
  pthread_mutex_unlock(&s->mutex);
  return 0;
}
int symcc_qfbv_realtime_dequeue_activity(
    void* p, uint64_t* token, uint64_t* generation, uint64_t* ordinal,
    uint64_t* level, int* kind, int* unit_literal, int* witness,
    int capacity, int* size) {
  Stream* s = p;
  pthread_mutex_lock(&s->mutex);
  if (!s->activity) { pthread_mutex_unlock(&s->mutex); return 0; }
  *size = s->clause_size - 1;
  if (capacity < *size) { pthread_mutex_unlock(&s->mutex); return -2; }
  *token = s->token;
  *generation = s->generation;
  *ordinal = s->activity_ordinal;
  *level = 1;
  *kind = 1;
  *unit_literal = s->clause[0];
  for (int i = 1; i < s->clause_size; ++i) witness[i - 1] = -s->clause[i];
  s->activity = 0;
  pthread_mutex_unlock(&s->mutex);
  return 1;
}
int symcc_qfbv_realtime_dequeue_learned(void* p, int* literals,
                                        int capacity, int* size) {
  Stream* s = p;
  pthread_mutex_lock(&s->mutex);
  if (!s->learned) { pthread_mutex_unlock(&s->mutex); return 0; }
  if (capacity < 2) { *size = 2; pthread_mutex_unlock(&s->mutex); return -2; }
  literals[0] = 1;
  literals[1] = -2;
  *size = 2;
  s->learned = 0;
  pthread_mutex_unlock(&s->mutex);
  return 1;
}
uint64_t symcc_qfbv_realtime_stat(void* p, int key) {
  Stream* s = p;
  pthread_mutex_lock(&s->mutex);
  uint64_t value = 0;
  if (key == 0) value = s->enqueued;
  else if (key == 1) value = s->delivered;
  else if (key == 2) value = s->rejected;
  else if (key == 3) value = s->learned_exported;
  else if (key == 5) value = s->queued;
  else if (key == 6) value = s->learned;
  else if (key == 7) value = s->ack;
  else if (key == 8) value = s->generation;
  else if (key == 9) value = s->solving;
  else if (key == 10) value = s->activated_unit;
  else if (key == 12) value = s->activity;
  else if (key == 13) value = s->tracked;
  pthread_mutex_unlock(&s->mutex);
  return value;
}
int symcc_qfbv_realtime_reset_queues(void* p) {
  Stream* s = p;
  pthread_mutex_lock(&s->mutex);
  s->queued = s->ack = s->learned = s->activity = 0;
  s->activity_enabled = 0;
  pthread_mutex_unlock(&s->mutex);
  return 0;
}
"""


def _plan(query_id="realtime-test"):
    return bitblast_qfbv_query(query_id, ["true"], {
        "true": {
            "op": "bool", "bits": 1, "children": [],
            "attrs": {"value": True},
        }
    }, {"incremental": True})


def _envelope():
    return {
        "schema": "symcc-query-ir-v1",
        "producer": "realtime-stream-test",
        "nodes": [{
            "id": 0, "op": "bool", "bits": 1, "children": [],
            "attrs": {"value": True},
        }],
        "prefix_roots": [],
        "target_root": 0,
        "input_hex": "",
        "timeout_ms": 2000,
        "metadata": {"source": "realtime-stream-test"},
        "smt2": "(assert true)\n",
        "prefix_smt2": "(assert true)\n",
        "target_smt2": "(assert true)\n",
    }


class RealtimeStreamTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        source = self.root / "fake.c"
        source.write_text(_FAKE_C, encoding="ascii")
        self.library = self.root / "libfake.so"
        subprocess.run(
            ["cc", "-shared", "-fPIC", "-pthread", str(source),
             "-o", str(self.library)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_event_stream_is_monotonic_and_concurrent_publish_is_idempotent(self):
        plan = _plan()
        store = IncrementalProofStore(self.root / "proofs")
        records = [make_rup_clause_record(
            plan, [1], source_worker=f"worker-{index}", worker_epoch=0,
            sequence=index,
        ) for index in range(8)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            published = list(pool.map(store.publish, records + records))
        self.assertEqual(sum(int(created) for _, created in published), 8)
        events = store.events_after(plan.formula_sha256, limit=64)
        self.assertEqual([sequence for sequence, _ in events], list(range(1, 9)))
        self.assertEqual(store.stats()["events"], 8)
        self.assertEqual(store.latest_event_sequence(plan.formula_sha256), 8)
        for sequence, digest in events:
            self.assertEqual(
                store.event_at(sequence), (plan.formula_sha256, digest)
            )
        with store._connect() as database:
            database.execute("DROP TABLE proof_events")
            database.execute(
                "DELETE FROM metadata WHERE key='event_stream_protocol'"
            )
        migrated = IncrementalProofStore(self.root / "proofs")
        self.assertEqual(migrated.stats()["events"], 8)
        migrated_events = migrated.events_after(
            plan.formula_sha256, limit=64
        )
        self.assertEqual(
            [sequence for sequence, _ in migrated_events], list(range(1, 9))
        )
        self.assertEqual(
            {digest for _, digest in migrated_events},
            {digest for _, digest in events},
        )

    def test_native_queue_backpressure_has_conserved_accounting(self):
        plan = _plan()
        store = IncrementalProofStore(self.root / "proofs")
        checker = IncrementalProofChecker(store)
        for index, clause in enumerate(([1], [1, -2])):
            store.publish(make_rup_clause_record(
                plan, clause, source_worker=f"publisher-{index}",
                worker_epoch=0, sequence=index,
            ))
        owner = NativeRealtimeCadical(self.library)
        native = owner.new_context(
            max_learned_length=0,
            max_imports=1,
            max_import_literals=64,
            max_learned=0,
        )
        native.observe(plan.max_variable)
        session = RealtimeClauseExchangeSession(
            plan, native, store, checker,
            native_signature=owner.signature,
            source_worker="consumer", worker_epoch=0,
            next_sequence=lambda: 100, stream_ordinal=1,
            max_imports=2, max_learned=0, poll_interval_ms=1,
        )
        session.start()
        deadline = time.monotonic() + 1.0
        while (
            native.stats()["imports_rejected"] != 1
            and time.monotonic() < deadline
        ):
            time.sleep(0.001)
        self.assertEqual(native.stats()["imports_rejected"], 1)
        self.assertEqual(native.solve(), 10)
        evidence = session.finish(native.stats()["solve_generation"])
        self.assertEqual(evidence["backend_realtime_import_candidates"], 2)
        self.assertEqual(evidence["backend_realtime_import_authorized"], 1)
        self.assertEqual(evidence["backend_realtime_import_delivered"], 1)
        self.assertEqual(evidence["backend_realtime_import_backpressure"], 1)
        native.close()

    def test_native_stats_expose_an_exact_solve_lifetime(self):
        owner = NativeRealtimeCadical(self.library)
        native = owner.new_context(
            max_learned_length=0,
            max_imports=1,
            max_import_literals=64,
            max_learned=0,
        )
        control = ctypes.CDLL(str(self.library))
        control.symcc_test_realtime_wait_for_import.argtypes = [ctypes.c_int]
        control.symcc_test_realtime_wait_for_import(1)
        result = []
        thread = threading.Thread(target=lambda: result.append(native.solve()))
        thread.start()
        deadline = time.monotonic() + 1.0
        while native.stats()["solving"] != 1 and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(native.stats()["solve_generation"], 1)
        self.assertEqual(native.stats()["solving"], 1)
        self.assertTrue(native.enqueue(1, [1]))
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [10])
        self.assertEqual(native.stats()["solving"], 0)
        control.symcc_test_realtime_wait_for_import(0)
        native.close()

    def test_native_compression_requirement_fails_closed(self):
        with self.assertRaisesRegex(
            RealtimeStreamError, "clause compression is required"
        ):
            NativeRealtimeCadical(
                self.library, require_clause_compression=True
            )
        with self.assertRaisesRegex(
            RealtimeStreamError, "requirement must be boolean"
        ):
            NativeRealtimeCadical(
                self.library, require_clause_compression="yes"
            )

    def test_native_ack_is_bound_to_checked_record_and_learned_is_replayed(self):
        plan = _plan()
        store = IncrementalProofStore(self.root / "proofs")
        checker = IncrementalProofChecker(store)
        record = make_rup_clause_record(
            plan, [1], source_worker="publisher", worker_epoch=1, sequence=1
        )
        source_digest, _ = store.publish(record)
        owner = NativeRealtimeCadical(self.library)
        native = owner.new_context(
            max_learned_length=8,
            max_imports=8,
            max_import_literals=64,
            max_learned=8,
        )
        for clause in plan.clauses:
            for literal in clause:
                native.add(literal)
            native.add(0)
        native.observe(plan.max_variable)
        sequence = itertools.count(10)
        session = RealtimeClauseExchangeSession(
            plan, native, store, checker,
            native_signature=owner.signature,
            source_worker="consumer", worker_epoch=2,
            next_sequence=lambda: next(sequence), stream_ordinal=1,
            max_imports=8, max_learned=8, poll_interval_ms=1,
        )
        session.start()
        self.assertEqual(native.solve(), 10)
        evidence = session.finish(native.stats()["solve_generation"])
        self.assertEqual(
            evidence["backend_realtime_import_record_sha256"], [source_digest]
        )
        self.assertEqual(evidence["backend_realtime_import_delivered"], 1)
        self.assertEqual(evidence["backend_realtime_learned_published"], 1)
        ack = evidence["backend_realtime_import_acks"][0]
        authorization = verify_checked_import_ack(plan, ack, checker=checker)
        self.assertEqual(authorization.record_sha256, source_digest)
        tampered = copy.deepcopy(ack)
        tampered["delivery_ordinal"] = 2
        with self.assertRaisesRegex(RealtimeStreamError, "identity"):
            verify_checked_import_ack(plan, tampered, checker=checker)
        native.terminate()
        self.assertEqual(native.solve(), 0)
        native.clear_termination()
        self.assertEqual(native.solve(), 10)
        native.close()

    def test_native_activity_is_bound_to_delivery_and_checked_clause(self):
        plan = _plan("native-activity-test")
        store = IncrementalProofStore(self.root / "activity-proofs")
        checker = IncrementalProofChecker(store)
        record = make_rup_clause_record(
            plan, [1, -2], source_worker="activity-publisher",
            worker_epoch=1, sequence=1,
        )
        digest, _ = store.publish(record)
        owner = NativeRealtimeCadical(self.library)
        self.assertEqual(owner.activity_protocol, CLAUSE_ACTIVITY_PROTOCOL)
        native = owner.new_context(
            max_learned_length=0,
            max_imports=1,
            max_import_literals=64,
            max_learned=0,
        )
        native.observe(plan.max_variable)
        control = ctypes.CDLL(str(self.library))
        control.symcc_test_realtime_wait_for_import.argtypes = [ctypes.c_int]
        control.symcc_test_realtime_wait_for_import(1)
        try:
            session = RealtimeClauseExchangeSession(
                plan, native, store, checker,
                native_signature=owner.signature,
                source_worker="activity-consumer", worker_epoch=2,
                next_sequence=lambda: 100, stream_ordinal=1,
                max_imports=1, max_learned=0, poll_interval_ms=1,
                track_clause_activity=True,
            )
            session.start()
            self.assertEqual(native.solve(), 10)
            evidence = session.finish(native.stats()["solve_generation"])
        finally:
            control.symcc_test_realtime_wait_for_import(0)
            native.close()
        self.assertTrue(
            evidence["backend_realtime_clause_activity_enabled"]
        )
        self.assertEqual(evidence["backend_realtime_import_delivered"], 1)
        self.assertEqual(evidence["backend_realtime_clause_activity_unit"], 1)
        self.assertEqual(
            evidence["backend_realtime_clause_activity_conflict"], 0
        )
        self.assertEqual(
            evidence["backend_realtime_clause_activity_unactivated"], 0
        )
        receipt = evidence["backend_realtime_clause_activity_receipts"][0]
        self.assertEqual(receipt["record_sha256"], digest)
        self.assertEqual(receipt["kind"], "unit")
        self.assertEqual(receipt["unit_literal"], 1)
        self.assertEqual(receipt["falsifying_assignments"], [2])
        authorization = verify_clause_activity_receipt(
            plan,
            receipt,
            ack=evidence["backend_realtime_import_acks"][0],
            checker=checker,
        )
        self.assertEqual(authorization.record_sha256, digest)

        tampered = copy.deepcopy(receipt)
        tampered["falsifying_assignments"] = [-2]
        body = dict(tampered)
        body.pop("activity_sha256")
        tampered["activity_sha256"] = hashlib.sha256(json.dumps(
            body, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("ascii")).hexdigest()
        with self.assertRaisesRegex(RealtimeStreamError, "witness"):
            verify_clause_activity_receipt(
                plan,
                tampered,
                ack=evidence["backend_realtime_import_acks"][0],
                checker=checker,
            )

        wrong_type = copy.deepcopy(receipt)
        wrong_type["activity_ordinal"] = "1"
        body = dict(wrong_type)
        body.pop("activity_sha256")
        wrong_type["activity_sha256"] = hashlib.sha256(json.dumps(
            body, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("ascii")).hexdigest()
        with self.assertRaisesRegex(RealtimeStreamError, "integer"):
            verify_clause_activity_receipt(
                plan,
                wrong_type,
                ack=evidence["backend_realtime_import_acks"][0],
                checker=checker,
            )

    def test_activity_tracking_requires_a_complete_native_capability(self):
        class LegacyNative:
            pass

        plan = _plan("activity-capability-test")
        store = IncrementalProofStore(self.root / "activity-capability-proofs")
        checker = IncrementalProofChecker(store)
        with self.assertRaisesRegex(RealtimeStreamError, "capable native shim"):
            RealtimeClauseExchangeSession(
                plan, LegacyNative(), store, checker,
                native_signature="legacy", source_worker="consumer",
                worker_epoch=0, next_sequence=lambda: 1, stream_ordinal=1,
                track_clause_activity=True,
            )

    def test_utility_pairing_closes_native_activity_outcome(self):
        plan = _plan("utility-pairing-realtime-test")
        store = IncrementalProofStore(self.root / "pairing-proofs")
        checker = IncrementalProofChecker(store)
        record = make_rup_clause_record(
            plan,
            [1, -2],
            source_worker="pairing-publisher",
            worker_epoch=1,
            sequence=1,
        )
        digest, _ = store.publish(record)
        owner = NativeRealtimeCadical(self.library)
        native = owner.new_context(
            max_learned_length=0,
            max_imports=1,
            max_import_literals=64,
            max_learned=0,
        )
        native.observe(plan.max_variable)
        controller = UtilityPairingController(UtilityPairingPolicy())
        control = ctypes.CDLL(str(self.library))
        control.symcc_test_realtime_wait_for_import.argtypes = [ctypes.c_int]
        control.symcc_test_realtime_wait_for_import(1)
        try:
            session = RealtimeClauseExchangeSession(
                plan,
                native,
                store,
                checker,
                native_signature=owner.signature,
                source_worker="pairing-consumer",
                worker_epoch=2,
                next_sequence=lambda: 100,
                stream_ordinal=1,
                max_imports=1,
                max_learned=0,
                poll_interval_ms=1,
                pairing_controller=controller,
                track_clause_activity=True,
            )
            session.start()
            self.assertEqual(native.solve(), 10)
            evidence = session.finish(native.stats()["solve_generation"])
        finally:
            control.symcc_test_realtime_wait_for_import(0)
            native.close()
        self.assertEqual(
            evidence["backend_realtime_pairing_action_counts"],
            {"admit": 1, "suppress": 0},
        )
        self.assertEqual(
            evidence["backend_realtime_pairing_outcome_counts"],
            {
                "unit": 1,
                "conflict": 0,
                "unactivated": 0,
                "backpressure": 0,
                "expired": 0,
            },
        )
        verified = verify_pairing_stream_result(
            evidence,
            stream_id=evidence["backend_realtime_stream_id"],
            consumer_worker="pairing-consumer",
            formula_family=(
                evidence["backend_realtime_pairing_formula_family_sha256"]
            ),
            delivered_records=[digest],
            activity_kinds={digest: "unit"},
        )
        self.assertEqual(
            verified["backend_realtime_pairing_controller_snapshot"][
                "pending_outcomes"
            ],
            0,
        )

    def test_utility_pairing_requires_activity_tracking(self):
        plan = _plan("utility-pairing-capability-test")
        store = IncrementalProofStore(self.root / "pairing-capability-proofs")
        checker = IncrementalProofChecker(store)
        with self.assertRaisesRegex(RealtimeStreamError, "requires clause-activity"):
            RealtimeClauseExchangeSession(
                plan,
                object(),
                store,
                checker,
                native_signature="test",
                source_worker="pairing-consumer",
                worker_epoch=0,
                next_sequence=lambda: 1,
                stream_ordinal=1,
                pairing_controller=UtilityPairingController(
                    UtilityPairingPolicy()
                ),
                track_clause_activity=False,
            )

    def test_adaptive_session_defers_until_solve_then_delivers(self):
        plan = _plan("adaptive-realtime-test")
        store = IncrementalProofStore(self.root / "adaptive-proofs")
        checker = IncrementalProofChecker(store)
        record = make_rup_clause_record(
            plan, [1], source_worker="adaptive-publisher",
            worker_epoch=1, sequence=1,
        )
        digest, _ = store.publish(record)
        owner = NativeRealtimeCadical(self.library)
        native = owner.new_context(
            max_learned_length=0,
            max_imports=1,
            max_import_literals=64,
            max_learned=0,
        )
        native.observe(plan.max_variable)
        control = ctypes.CDLL(str(self.library))
        control.symcc_test_realtime_wait_for_import.argtypes = [ctypes.c_int]
        control.symcc_test_realtime_wait_for_import(1)
        controller = AdaptiveProofController(AdaptiveProofPolicy(
            queue_capacity=1,
            high_watermark_permille=1000,
            max_retries=8,
            minimum_remaining_ms=0,
        ))
        session = RealtimeClauseExchangeSession(
            plan, native, store, checker,
            native_signature=owner.signature,
            source_worker="adaptive-consumer", worker_epoch=2,
            next_sequence=lambda: 100, stream_ordinal=1,
            max_imports=1, max_learned=0, poll_interval_ms=1,
            adaptive_controller=controller, solve_budget_ms=1000,
        )
        session.start()
        defer_deadline = time.monotonic() + 1.0
        while (
            not session._adaptive_decisions
            and time.monotonic() < defer_deadline
        ):
            time.sleep(0.001)
        self.assertEqual(session._adaptive_decisions[0]["action"], "defer")
        result = []
        thread = threading.Thread(target=lambda: result.append(native.solve()))
        thread.start()
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())
        evidence = session.finish(native.stats()["solve_generation"])
        self.assertEqual(result, [10])
        self.assertEqual(
            evidence["backend_realtime_import_record_sha256"], [digest]
        )
        self.assertGreaterEqual(
            evidence["backend_realtime_adaptive_action_counts"]["defer"], 1
        )
        self.assertEqual(
            evidence["backend_realtime_adaptive_action_counts"]["admit"], 1
        )
        self.assertGreaterEqual(
            evidence["backend_realtime_adaptive_retried"], 1
        )
        verified = verify_adaptive_stream_result(
            evidence,
            stream_id=evidence["backend_realtime_stream_id"],
            delivered_records=(digest,),
            authorized=1,
            backpressure=0,
        )
        self.assertEqual(
            verified["backend_realtime_adaptive_deferred_final"], 0
        )
        control.symcc_test_realtime_wait_for_import(0)
        native.close()

    def test_adaptive_abort_quiesces_feedback_before_next_stream(self):
        class NoAckNative:
            def __init__(self):
                self.enqueued = 0

            def reset_queues(self):
                return None

            def stats(self):
                return {
                    "solve_generation": 1,
                    "solving": True,
                    "acks_queued": 0,
                    "imports_enqueued": self.enqueued,
                    "imports_delivered": 0,
                    "imports_rejected": 0,
                    "learned_exported": 0,
                    "learned_dropped": 0,
                }

            def enqueue(self, token, clause):
                del token, clause
                self.enqueued += 1
                return True

            def dequeue_ack(self):
                return None

            def dequeue_learned(self, max_length):
                del max_length
                return None

            def terminate(self):
                return None

        plan = _plan("adaptive-abort-test")
        store = IncrementalProofStore(self.root / "adaptive-abort-proofs")
        checker = IncrementalProofChecker(store)
        record = make_rup_clause_record(
            plan, [1], source_worker="abort-publisher",
            worker_epoch=1, sequence=1,
        )
        store.publish(record)
        native = NoAckNative()
        controller = AdaptiveProofController(AdaptiveProofPolicy(
            queue_capacity=1, high_watermark_permille=1000,
            minimum_remaining_ms=0,
        ))
        first = RealtimeClauseExchangeSession(
            plan, native, store, checker,
            native_signature="no-ack-native",
            source_worker="abort-consumer", worker_epoch=2,
            next_sequence=lambda: 100, stream_ordinal=1,
            max_imports=1, max_learned=0, poll_interval_ms=1,
            adaptive_controller=controller, solve_budget_ms=1000,
        )
        first.start()
        deadline = time.monotonic() + 1.0
        while (
            first.progress()["authorized"] != 1
            and time.monotonic() < deadline
        ):
            time.sleep(0.001)
        self.assertEqual(first.progress()["authorized"], 1)
        first.abort()
        self.assertEqual(controller.snapshot()["pending_feedback"], 0)
        first.abort()

        second = RealtimeClauseExchangeSession(
            plan, native, store, checker,
            native_signature="no-ack-native",
            source_worker="abort-consumer", worker_epoch=2,
            next_sequence=lambda: 101, stream_ordinal=2,
            max_imports=1, max_learned=0, poll_interval_ms=1,
            adaptive_controller=controller, solve_budget_ms=1000,
        )
        second.start()
        deadline = time.monotonic() + 1.0
        while (
            second.progress()["authorized"] != 1
            and time.monotonic() < deadline
        ):
            time.sleep(0.001)
        self.assertEqual(second.progress()["authorized"], 1)
        second.abort()
        snapshot = verify_controller_snapshot(
            controller.snapshot(), policy=controller.policy
        )
        self.assertEqual(snapshot["pending_feedback"], 0)
        self.assertEqual(snapshot["totals"]["expired"], 2)

    def test_persistent_backend_streams_during_solve_and_store_rechecks(self):
        query_store = QueryStore(self.root / "queries")
        query_store.ingest(_envelope())
        lease = query_store.claim("worker")
        self.assertIsNotNone(lease)
        loaded = query_store.load_query_ir(lease.query_id)
        plan = bitblast_qfbv_query(
            lease.query_id, loaded[0], loaded[1], {"incremental": True}
        )
        proof_store = IncrementalProofStore(self.root / "proofs")
        checker = IncrementalProofChecker(proof_store)
        query_store.register_qfbv_incremental_proof_checker(checker)
        record = make_rup_clause_record(
            plan, [1], source_worker="late-publisher", worker_epoch=3,
            sequence=1,
        )

        probe = ctypes.CDLL(str(self.library))
        probe.symcc_test_realtime_solve_started.argtypes = []
        probe.symcc_test_realtime_solve_started.restype = ctypes.c_uint64
        probe.symcc_test_realtime_wait_for_import.argtypes = [ctypes.c_int]
        probe.symcc_test_realtime_wait_for_import.restype = None
        probe.symcc_test_realtime_wait_for_import(1)
        publication_errors = []

        def publish_late():
            deadline = time.monotonic() + 2.0
            while (
                probe.symcc_test_realtime_solve_started() == 0
                and time.monotonic() < deadline
            ):
                time.sleep(0.001)
            if probe.symcc_test_realtime_solve_started() == 0:
                publication_errors.append("native solve did not start")
                return
            proof_store.publish(record)

        publisher = threading.Thread(target=publish_late)
        publisher.start()
        command = [
            sys.executable, "-c", "raise SystemExit(1)", "--plain",
            "--lrat", "--no-binary", "{cnf}", "{proof}",
        ]
        with PersistentCadicalQfbvSolver(
            query_store, self.library, command,
            name="realtime-test", proof_store=proof_store,
            proof_checker=checker, capabilities={"incremental": True},
            realtime_library_path=self.library,
            realtime_max_learned=0,
            realtime_poll_interval_ms=1,
            realtime_adaptive_policy={
                "high_watermark_permille": 1000,
                "minimum_remaining_ms": 0,
            },
            realtime_track_clause_activity=True,
            realtime_pairing_policy={
                "min_exploration_samples": 2,
                "refresh_after_events": 64,
            },
        ) as backend:
            result = dict(backend(lease))
        publisher.join()
        self.assertEqual(publication_errors, [])
        self.assertEqual(result["status"], "sat")
        self.assertEqual(
            result["backend_native_context_protocol"],
            "cadical-ipasir-up-realtime-v1",
        )
        self.assertEqual(result["backend_realtime_import_delivered"], 1)
        self.assertTrue(query_store.complete(lease, "worker", result))
        with query_store._connect() as database:
            row = database.execute(
                "SELECT result_json FROM results WHERE query_id=?",
                (lease.query_id,),
            ).fetchone()
        saved = json.loads(str(row[0]))
        self.assertTrue(saved["store_realtime_stream_verified"])
        self.assertEqual(saved["store_realtime_stream_verified_imports"], 1)
        self.assertEqual(saved["backend_realtime_import_settled_candidates"], 1)
        self.assertEqual(saved["backend_realtime_import_duplicate_clauses"], 0)
        self.assertTrue(saved["store_realtime_clause_activity_verified"])
        self.assertEqual(
            saved["store_realtime_clause_activity_verified_receipts"], 1
        )
        self.assertEqual(saved["backend_realtime_clause_activity_unit"], 1)
        self.assertEqual(
            saved["backend_realtime_clause_activity_unactivated"], 0
        )
        self.assertTrue(saved["store_realtime_adaptive_verified"])
        self.assertGreaterEqual(
            saved["store_realtime_adaptive_verified_decisions"], 1
        )
        self.assertEqual(
            saved["store_realtime_adaptive_verified_enqueues"], 1
        )
        self.assertTrue(saved["store_realtime_pairing_verified"])
        self.assertEqual(
            saved["store_realtime_pairing_verified_decisions"], 1
        )
        self.assertEqual(
            saved["store_realtime_pairing_verified_outcomes"], 1
        )
        self.assertEqual(
            saved["store_realtime_pairing_checkpoint_disposition"],
            "advanced",
        )
        pairing_policy = UtilityPairingPolicy.from_mapping({
            "min_exploration_samples": 2,
            "refresh_after_events": 64,
        })
        persisted_pairing = query_store.load_qfbv_utility_pairing_snapshot(
            "cadical-native-worker", pairing_policy
        )
        self.assertIsNotNone(persisted_pairing)
        self.assertEqual(persisted_pairing["totals"]["decisions"], 1)
        with PersistentCadicalQfbvSolver(
            query_store, self.library, command,
            name="realtime-test-restored", proof_store=proof_store,
            proof_checker=checker, capabilities={"incremental": True},
            realtime_library_path=self.library,
            realtime_max_learned=0,
            realtime_poll_interval_ms=1,
            realtime_track_clause_activity=True,
            realtime_pairing_policy={
                "min_exploration_samples": 2,
                "refresh_after_events": 64,
            },
        ) as restored_backend:
            restored_snapshot = (
                restored_backend.realtime_pairing_controller.snapshot()
            )
        self.assertEqual(
            restored_snapshot["snapshot_sha256"],
            persisted_pairing["snapshot_sha256"],
        )
        stats = query_store.stats()
        self.assertEqual(stats["realtime_proof_stream_results"], 1)
        self.assertEqual(stats["realtime_proof_stream_verified_results"], 1)
        self.assertEqual(stats["realtime_proof_stream_imports_delivered"], 1)
        self.assertEqual(stats["realtime_clause_activity_results"], 1)
        self.assertEqual(stats["realtime_clause_activity_verified_results"], 1)
        self.assertEqual(stats["realtime_clause_activity_receipts"], 1)
        self.assertEqual(stats["adaptive_proof_admission_results"], 1)
        self.assertEqual(
            stats["adaptive_proof_admission_verified_results"], 1
        )
        self.assertGreaterEqual(stats["adaptive_proof_admission_decisions"], 1)
        self.assertEqual(stats["adaptive_proof_admission_enqueues"], 1)
        self.assertEqual(stats["utility_pairing_results"], 1)
        self.assertEqual(stats["utility_pairing_verified_results"], 1)
        self.assertEqual(stats["utility_pairing_persisted_workers"], 1)
        self.assertEqual(stats["utility_pairing_decisions"], 1)
        self.assertEqual(stats["utility_pairing_outcomes"], 1)
        self.assertEqual(stats["utility_pairing_admitted"], 1)
        self.assertEqual(stats["utility_pairing_suppressed"], 0)
        self.assertEqual(stats["utility_pairing_activated"], 1)
        self.assertEqual(stats["utility_pairing_unactivated"], 0)

        compressed = copy.deepcopy(result)
        compressed.update({
            "backend_realtime_clause_compression_protocol": (
                "symcc-qfbv-native-clause-compression-v1"
            ),
            "backend_realtime_clause_uncompressed_bytes": 4,
            "backend_realtime_clause_compressed_bytes": 2,
            "backend_realtime_clause_inline_clauses": 1,
            "backend_realtime_clause_heap_clauses": 0,
            "backend_realtime_clause_queued_encoded_bytes": 0,
            "backend_realtime_clause_decoded_literals": 1,
            "backend_realtime_clause_compression_failures": 0,
        })
        normalized_compressed = query_store._validate_result(compressed)
        self.assertEqual(
            normalized_compressed["backend_realtime_clause_compressed_bytes"],
            2,
        )
        invalid_compressed = copy.deepcopy(compressed)
        invalid_compressed.pop("backend_realtime_clause_heap_clauses")
        with self.assertRaisesRegex(ValueError, "telemetry is incomplete"):
            query_store._validate_result(invalid_compressed)
        invalid_compressed = copy.deepcopy(compressed)
        invalid_compressed["backend_realtime_clause_compression_failures"] = 1
        with self.assertRaisesRegex(ValueError, "must be in"):
            query_store._validate_result(invalid_compressed)

        invalid = copy.deepcopy(result)
        invalid["backend_realtime_stream_id"] = ""
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            query_store._validate_result(invalid)
        invalid = copy.deepcopy(result)
        invalid["backend_realtime_import_pending"] = 1
        with self.assertRaisesRegex(ValueError, "accounting"):
            query_store._validate_result(invalid)
        invalid = copy.deepcopy(result)
        invalid["backend_realtime_import_settled_candidates"] = 2
        with self.assertRaisesRegex(ValueError, "settled import candidates"):
            query_store._validate_result(invalid)
        invalid = copy.deepcopy(result)
        invalid.pop("backend_realtime_import_duplicate_clauses")
        with self.assertRaisesRegex(ValueError, "telemetry is incomplete"):
            query_store._validate_result(invalid)
        invalid = copy.deepcopy(result)
        invalid["backend_realtime_native_signature"] = (
            "symcc-qfbv-realtime-v1|cadical-3.0.1-other"
        )
        with self.assertRaisesRegex(ValueError, "signatures disagree"):
            query_store._validate_result(invalid)
        invalid = copy.deepcopy(result)
        invalid["backend_realtime_native_learned_exported"] = 1
        with self.assertRaisesRegex(ValueError, "learned-clause accounting"):
            query_store._validate_result(invalid)
        invalid = copy.deepcopy(result)
        invalid.pop("backend_realtime_clause_activity_conflict")
        with self.assertRaisesRegex(ValueError, "activity telemetry is incomplete"):
            query_store._validate_result(invalid)
        invalid = copy.deepcopy(result)
        activity = invalid["backend_realtime_clause_activity_receipts"][0]
        activity["falsifying_assignments"] = [2]
        activity_body = dict(activity)
        activity_body.pop("activity_sha256")
        activity["activity_sha256"] = hashlib.sha256(json.dumps(
            activity_body,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")).hexdigest()
        activity_store = QueryStore(self.root / "activity-tamper-queries")
        activity_store.ingest(_envelope())
        activity_lease = activity_store.claim("activity-tamper-worker")
        self.assertIsNotNone(activity_lease)
        activity_store.register_qfbv_incremental_proof_checker(checker)
        with self.assertRaisesRegex(ValueError, "independent store verification"):
            activity_store.complete(
                activity_lease, "activity-tamper-worker", invalid
            )
        invalid = copy.deepcopy(result)
        invalid["backend_native_signature"] += "x" * 128
        invalid["backend_realtime_native_signature"] = (
            "symcc-qfbv-realtime-v1|" + invalid["backend_native_signature"]
        )
        with self.assertRaisesRegex(ValueError, "must not be truncated"):
            query_store._validate_result(invalid)
        invalid = copy.deepcopy(result)
        invalid["backend_realtime_adaptive_decisions"][0]["score"] += 1
        with self.assertRaisesRegex(ValueError, "identity"):
            query_store._validate_result(invalid)
        invalid = copy.deepcopy(result)
        enqueued_record, old_decision_id = next(iter(
            invalid["backend_realtime_adaptive_enqueued_decisions"].items()
        ))
        enqueued_decision = next(
            decision
            for decision in invalid["backend_realtime_adaptive_decisions"]
            if decision["decision_sha256"] == old_decision_id
        )
        enqueued_decision["candidate"]["event_sequence"] += 10_000
        decision_body = dict(enqueued_decision)
        decision_body.pop("decision_sha256")
        new_decision_id = hashlib.sha256(
            json.dumps(
                decision_body,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
        ).hexdigest()
        enqueued_decision["decision_sha256"] = new_decision_id
        invalid["backend_realtime_adaptive_enqueued_decisions"][
            enqueued_record
        ] = new_decision_id
        tamper_store = QueryStore(self.root / "adaptive-tamper-queries")
        tamper_store.ingest(_envelope())
        tamper_lease = tamper_store.claim("tamper-worker")
        self.assertIsNotNone(tamper_lease)
        tamper_store.register_qfbv_incremental_proof_checker(checker)
        with self.assertRaisesRegex(ValueError, "independent store verification"):
            tamper_store.complete(tamper_lease, "tamper-worker", invalid)
        with query_store._connect() as database:
            database.execute(
                "UPDATE utility_pairing_snapshots SET snapshot_json='{}'"
            )
        with self.assertRaisesRegex(ValueError, "stored utility-pairing"):
            query_store.load_qfbv_utility_pairing_snapshot(
                "cadical-native-worker", pairing_policy
            )

    def test_realtime_deadline_terminates_and_closes_cleanly(self):
        envelope = _envelope()
        envelope["timeout_ms"] = 20
        query_store = QueryStore(self.root / "deadline-queries")
        query_store.ingest(envelope)
        lease = query_store.claim("worker")
        proof_store = IncrementalProofStore(self.root / "deadline-proofs")
        checker = IncrementalProofChecker(proof_store)
        command = [
            sys.executable, "-c", "raise SystemExit(1)", "--plain",
            "--lrat", "--no-binary", "{cnf}", "{proof}",
        ]
        with PersistentCadicalQfbvSolver(
            query_store, self.library, command,
            name="deadline-test", proof_store=proof_store,
            proof_checker=checker, capabilities={"incremental": True},
            realtime_library_path=self.library,
            realtime_max_imports=0,
            realtime_max_learned=0,
            realtime_poll_interval_ms=1,
        ) as backend:
            result = dict(backend(lease))
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["backend_native_result"], 0)
        self.assertIn("deadline", result["reason"])

    def test_realtime_portfolio_configuration_is_explicit_and_bounded(self):
        base = {
            "name": "cadical-realtime",
            "kind": "bitblast-cadical-qfbv",
            "persistent": True,
            "native_library": "/opt/cadical/lib/libcadical.so",
            "command": [
                "cadical", "--plain", "--lrat", "--no-binary",
                "{cnf}", "{proof}",
            ],
            "capabilities": {"incremental": True},
            "realtime_stream": {
                "library": "/opt/cadical/lib/libsymcc.so",
                "max_imports": 32,
                "max_events": 2048,
                "max_learned": 16,
                "track_clause_activity": True,
                "adaptive": {
                    "high_watermark_permille": 600,
                    "max_deferred": 8,
                },
                "pairing": {
                    "min_exploration_samples": 3,
                    "refresh_after_events": 17,
                },
            },
        }
        parsed = _load_portfolio(json.dumps([base]))[0]
        self.assertEqual(parsed["realtime_stream"]["max_imports"], 32)
        self.assertTrue(
            parsed["realtime_stream"]["track_clause_activity"]
        )
        self.assertEqual(
            parsed["realtime_stream"]["adaptive"][
                "high_watermark_permille"
            ],
            600,
        )
        self.assertEqual(
            parsed["realtime_stream"]["adaptive"]["max_deferred"], 8
        )
        self.assertEqual(
            parsed["realtime_stream"]["pairing"][
                "min_exploration_samples"
            ],
            3,
        )
        self.assertEqual(
            parsed["realtime_stream"]["pairing"]["refresh_after_events"],
            17,
        )
        invalid = copy.deepcopy(base)
        invalid["realtime_stream"]["poll_interval_ms"] = 0
        with self.assertRaisesRegex(RuntimeError, "bounds"):
            _load_portfolio(json.dumps([invalid]))
        unknown = copy.deepcopy(base)
        unknown["realtime_stream"]["typo"] = True
        with self.assertRaisesRegex(RuntimeError, "unknown"):
            _load_portfolio(json.dumps([unknown]))
        invalid_adaptive = copy.deepcopy(base)
        invalid_adaptive["realtime_stream"]["adaptive"]["max_retries"] = 0
        with self.assertRaisesRegex(RuntimeError, "adaptive proof policy"):
            _load_portfolio(json.dumps([invalid_adaptive]))
        invalid_activity = copy.deepcopy(base)
        invalid_activity["realtime_stream"]["track_clause_activity"] = "yes"
        with self.assertRaisesRegex(RuntimeError, "clause-activity"):
            _load_portfolio(json.dumps([invalid_activity]))
        missing_activity = copy.deepcopy(base)
        missing_activity["realtime_stream"]["track_clause_activity"] = False
        with self.assertRaisesRegex(RuntimeError, "requires clause activity"):
            _load_portfolio(json.dumps([missing_activity]))
        invalid_pairing = copy.deepcopy(base)
        invalid_pairing["realtime_stream"]["pairing"]["max_pairs"] = 0
        with self.assertRaisesRegex(RuntimeError, "utility pairing policy"):
            _load_portfolio(json.dumps([invalid_pairing]))


if __name__ == "__main__":
    unittest.main()
