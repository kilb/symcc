# F345 Stable Hybrid Input Admission Evidence

This directory records reproducible correctness tests, a local production-
primitive integration, and a fresh-process mechanism benchmark for F345. The
feature binds AFL scoring, master admission, worker materialization, and remote
residency knowledge to exact content identities while keeping queue selection
and verification metadata bounded.

## Files

- `benchmark_hybrid_input_snapshot.py`: fresh-process read-all versus production
  stable-snapshot benchmark at 32 and 128 MiB.
- `hybrid-input-snapshot-cost.json`: all 40 retained timing, traced-heap, RSS,
  size, and digest observations.
- `hybrid-input-snapshot-cost.log`: exact benchmark output.
- `run_hybrid_input_integration.py`: real-file driver through production
  queue, CAS, master admission, worker materialization, ACK, Top-K, and
  continuation primitives.
- `hybrid-input-integration.json`: 11 recomputable integration checks.
- `hybrid-input-integration.log`: exact structured integration output.
- `directed-tests.log`: distributed-state and MPI-lifecycle test summary.
- `integration-tests.log`: six related modules with warnings as errors.
- `full-tests.log`: every `test/test_*.py` module with warnings as errors.
- `checks.txt`: concise implementation, result, and claim boundaries.
- `SHA256SUMS.txt`: exact manifest for all other files in this directory.

## Reproduce

From this directory:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 benchmark_hybrid_input_snapshot.py \
  --warmups 2 --repetitions 10 \
  --output hybrid-input-snapshot-cost.json \
  > hybrid-input-snapshot-cost.log 2>&1

PYTHONDONTWRITEBYTECODE=1 python3 run_hybrid_input_integration.py \
  > hybrid-input-integration.json
cp hybrid-input-integration.json hybrid-input-integration.log
```

From the repository root:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q \
  test/test_distributed_state.py test/test_mpi_lifecycle.py

PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -W error \
  test/test_distributed_state.py test/test_mpi_filesystem_qualification.py \
  test/test_mpi_lifecycle.py test/test_hybrid_feedback.py \
  test/test_afl_profile_orchestration.py test/test_adaptive_components.py

PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -W error test/test_*.py
```

## Interpretation

The benchmark compares the former `open().read()` SHA-256 pattern with the
production bounded, no-follow, identity-closing snapshot in fresh processes.
Each mechanism receives the same sparse regular input; every retained size and
digest must match. It isolates a local hashing/admission mechanism and is not a
hybrid campaign throughput benchmark.

The integration uses real temporary files and production Python primitives. It
covers exact first/second queue versions, score invalidation, selected-record
pinning after FIFO eviction, content and path transport modes, CAS corruption,
repair, result acknowledgement, resource/type rejection, and self-contained
continuation identity. It does not launch MPI, afl-showmap, a target, or a
symbolic solver.

These artifacts establish the implemented data-plane invariants, bounded local
resource shape, deterministic local observations, and regression compatibility.
They do not establish multi-host scaling, network-byte savings in a real
campaign, solver acceleration, coverage improvement, bug discovery, or LAVA-M
uplift.

