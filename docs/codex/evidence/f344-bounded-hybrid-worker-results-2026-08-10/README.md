# F344 Bounded Hybrid Worker Result Admission Evidence

This directory records reproducible tests, a fresh-process mechanism benchmark,
and a local production-wrapper integration for F344. The feature replaces the
hybrid worker's unbounded `list(scandir)` plus retained read-all result set with
budget-first namespace admission, stable no-follow reads, digest-only pass one,
and whole-parent rejection.

## Files

- `benchmark_worker_result_admission.py`: fresh-process legacy-versus-F344
  collector benchmark at 32 and 128 one-MiB objects.
- `worker-result-admission-cost.json`: all 40 retained timing, traced-heap, RSS,
  cardinality, byte-count, and output-vector observations.
- `worker-result-admission-cost.log`: complete benchmark output.
- `run_worker_result_integration.py`: real child-process driver through the
  production `SymCCEngine` and `run_symcc_worker` wrapper.
- `worker-result-integration.json`: exact success/object/byte/hint cases and six
  recomputable checks.
- `worker-result-integration.log`: complete structured integration output.
- `directed-tests.log`: warnings-as-errors MPI lifecycle regression.
- `integration-tests.log`: six related distributed/MPI/hybrid modules.
- `full-tests.log`: every `test/test_*.py` module with warnings as errors.
- `checks.txt`: concise implementation, results, and proof boundaries.
- `SHA256SUMS.txt`: exact content manifest for this evidence directory.

## Reproduce

From this directory:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 benchmark_worker_result_admission.py \
  --warmups 2 --repetitions 10 \
  --output worker-result-admission-cost.json \
  > worker-result-admission-cost.log 2>&1

PYTHONDONTWRITEBYTECODE=1 python3 run_worker_result_integration.py \
  > worker-result-integration.json
cp worker-result-integration.json worker-result-integration.log
```

From the repository root:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -W error \
  test/test_mpi_lifecycle.py

PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -W error \
  test/test_distributed_state.py test/test_mpi_filesystem_qualification.py \
  test/test_mpi_lifecycle.py test/test_hybrid_feedback.py \
  test/test_afl_profile_orchestration.py test/test_adaptive_components.py

PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -W error test/test_*.py
```

## Interpretation

The benchmark fixture contains unique sparse regular result files. The legacy
mechanism models the former complete directory materialization and retained
whole-file `bytes` list. F344 calls the production preflight and stable-read
primitives twice, modeling coverage-redundant outputs whose bytes need not enter
the final MPI result. Each sample runs in a fresh Python process, and both paths
must reproduce the same object count, total bytes, and ordered digest vector.

The integration driver launches an actual executable Python producer through
the production SymCC engine wrapper. It establishes normal compatibility and
exact fail-closed object, aggregate-byte, and hint rejection. It is not an MPI
transport, afl-showmap, symbolic-solver, or fuzzing-campaign experiment.

These artifacts establish bounded worker-side collection, byte-equivalent
mechanism behavior, local resource cost, and production-wrapper integration.
They do not establish distributed scaling, solver speed, campaign throughput,
coverage improvement, bug discovery, or LAVA-M uplift.
