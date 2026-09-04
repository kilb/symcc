# F343 Evidence: Bounded Streaming Simulation Mutations

This directory records implementation, regression, mechanism-cost, and real
Open MPI evidence for F343. The feature replaces read-all synthetic mutation
generation with budget-first, stable-source, fixed-window output fan-out and
temporary-first no-clobber publication.

## Files

- `benchmark_simulation_mutations.py`: fresh-process legacy-versus-production
  benchmark with an identical injected RNG stream.
- `simulation-mutation-cost.json`: all 40 retained timing, Python allocator,
  RSS, byte-count, and ordered output-vector observations plus recomputed
  summaries.
- `simulation-mutation-cost.log`: complete benchmark output.
- `run_streaming_simulation_mpi.py`: actual two-rank Open MPI integration
  driver for the production `--simulate` path.
- `streaming-simulation-mpi.json`: exact MPI configuration, public corpus,
  target observations, lifecycle state, output, and 13 structural checks.
- `streaming-simulation-mpi.log`: complete MPI and structured result output.
- `directed-tests.log`: warnings-as-errors MPI lifecycle regression.
- `integration-tests.log`: six related distributed/MPI/hybrid modules.
- `full-tests.log`: every `test/test_*.py` module with warnings as errors.
- `checks.txt`: concise environment, implementation, outcomes, and claim
  boundary.
- `SHA256SUMS.txt`: exact content manifest for this evidence directory.

## Reproduce

From this directory:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 benchmark_simulation_mutations.py \
  --warmups 2 --repetitions 10 \
  --output simulation-mutation-cost.json \
  > simulation-mutation-cost.log 2>&1

PYTHONDONTWRITEBYTECODE=1 python3 run_streaming_simulation_mpi.py \
  --output streaming-simulation-mpi.json \
  > streaming-simulation-mpi.log 2>&1
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

The benchmark creates five outputs for each sparse zero-filled input. Legacy
is the former `read()` + whole `bytearray` + `bytes(mutated)` implementation;
streaming calls the production F343 helper. Each sample runs in a fresh Python
process, both mechanisms receive `random.Random(0xF343)`, and every retained
sample must produce the same ordered output hash vector.

The MPI driver uses one master, one worker, a one-byte seed, and a synthetic
observer target. The seed produces no target output, so the worker must invoke
F343; non-seed children are echoed to prevent recursive simulation. It
exercises real MPI transport and the production result discovery, staging,
fenced publication, deduplication, epoch retirement, and shutdown ACK path.

These artifacts establish byte equivalence, bounded Python heap scaling,
local mechanism cost, and same-host integration. They do not establish solver
speed, symbolic-execution coverage, campaign throughput, multi-host behavior,
bug discovery, or LAVA-M uplift.
