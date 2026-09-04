# F341 Evidence: Stable Streaming Input Admission

This directory records implementation-level evidence for stable external-input
snapshots, bounded owner publication, one-pass worker copies, and provenance-
correct public-corpus accounting.

## Files

- `run_stable_input_mpi.py`: actual Open MPI same-name replacement and input-
  overflow driver.
- `stable-input-mpi.json`: structured MPI states, checks, public identities,
  epoch outcomes, return codes, and proof boundary.
- `stable-input-mpi.log`: complete output from both MPI cases.
- `benchmark_input_admission.py`: fresh-process legacy read-all versus
  production F341 owner-admission comparison.
- `input-admission-cost.json`: every raw sample, median, P95, ratio, and
  benchmark boundary.
- `input-admission-cost.log`: complete benchmark JSON.
- `directed-tests.log`: lifecycle/unit regression.
- `integration-tests.log`: six related MPI/distributed/hybrid modules.
- `full-tests.log`: every `test/test_*.py` module with warnings as errors.
- `checks.txt`: concise environment, mechanism, outcomes, and claim boundary.
- `SHA256SUMS.txt`: complete content manifest for this directory.

## Reproduce

From this directory:

```bash
python3 run_stable_input_mpi.py --output stable-input-mpi.json \
  > stable-input-mpi.log 2>&1
python3 benchmark_input_admission.py --warmups 2 --repetitions 10 \
  --output input-admission-cost.json > input-admission-cost.log 2>&1
```

From the repository root:

```bash
python3 -m pytest -q -W error test/test_mpi_lifecycle.py
python3 -m pytest -q -W error \
  test/test_distributed_state.py test/test_mpi_filesystem_qualification.py \
  test/test_mpi_lifecycle.py test/test_hybrid_feedback.py \
  test/test_afl_profile_orchestration.py test/test_adaptive_components.py
python3 -m pytest -q -W error test/test_*.py
```

Absolute timings depend on host, page cache, and storage. The JSON raw samples
and explicit proof boundaries are authoritative; the report must not convert
mechanism memory evidence into campaign, solver, coverage, or bug-finding claims.
