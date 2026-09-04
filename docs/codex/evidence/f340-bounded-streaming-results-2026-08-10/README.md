# F340 Evidence: Bounded Streaming Result Admission

This directory records the implementation-level evidence for per-parent
standalone result object/byte admission and streaming worker staging.

## Files

- `run_result_budget_mpi.py`: deterministic success/object-overflow/byte-overflow
  driver using actual Open MPI transport.
- `result-budget-mpi.json`: structured MPI outcomes, exact public-object state,
  epoch state, residue checks, return codes, and proof boundary.
- `result-budget-mpi.log`: complete stdout/stderr for all three MPI cases.
- `benchmark_result_staging.py`: fresh-process comparison of the legacy
  whole-file staging algorithm and the production F340 streaming helper.
- `result-staging-cost.json`: all raw samples, medians, P95 values, ratios, and
  benchmark boundary.
- `result-staging-cost.log`: complete benchmark JSON emitted by the driver.
- `directed-tests.log`: lifecycle/unit regression.
- `integration-tests.log`: six related MPI/distributed/hybrid modules.
- `full-tests.log`: every `test/test_*.py` module with warnings as errors.
- `checks.txt`: concise environment, mechanism, results, and claim boundary.
- `SHA256SUMS.txt`: content manifest for this directory.

## Reproduce

```bash
python3 run_result_budget_mpi.py --output result-budget-mpi.json \
  > result-budget-mpi.log 2>&1

python3 benchmark_result_staging.py --warmups 2 --repetitions 10 \
  --output result-staging-cost.json > result-staging-cost.log 2>&1

python3 -m pytest -q -W error test/test_mpi_lifecycle.py
python3 -m pytest -q -W error \
  test/test_distributed_state.py test/test_mpi_filesystem_qualification.py \
  test/test_mpi_lifecycle.py test/test_hybrid_feedback.py \
  test/test_afl_profile_orchestration.py test/test_adaptive_components.py
python3 -m pytest -q -W error test/test_*.py
```

Run test commands from the repository root. Absolute timings depend on the
host and storage cache. JSON mechanism checks and raw sample counts are the
authoritative evidence; the report must retain the stored proof boundaries.
