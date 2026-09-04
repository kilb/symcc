# F381 pre-I/O persistent portfolio cancellation evidence

This directory records the reproducible evidence for cancellation registration
before persistent-solver I/O admission.

## Scope

- per-invocation cancellation registration before `_io_lock` acquisition;
- query-scoped cancellation broadcast with exact active-process matching;
- total deregistration and close coverage for queued and active requests;
- deterministic pre-I/O race reproduction, cold recovery, and repeated checks.

The evidence establishes local concurrency and resource-lifecycle behavior. It
does not establish a real-time cancellation bound, distributed cancellation,
solver throughput, coverage, or campaign improvement.

## Reproduction

```bash
python3 -m pytest -q \
  test/test_query_store.py::QueryStoreTest::test_portfolio_cancels_persistent_helper_before_io_admission \
  test/test_query_store.py::QueryStoreTest::test_cancelled_persistent_helper_restarts_cold \
  test/test_qf_bv_backend.py::QfBvBackendTest::test_cancelled_incremental_qfbv_context_recovers_cold \
  -W error -p no:cacheprovider
python3 -m pytest -q test/test_qf_bv_backend.py test/test_query_store.py \
  -W error -p no:cacheprovider
cmake --build build --target check -j2
```

The canonical complete Python gate uses `test/pytest-nodeids.json` and requires
all configured native commands, Python modules, and the Z3 shared library. Its
machine-readable result is `full-python-gate.json`.

