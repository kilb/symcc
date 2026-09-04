# F342 Evidence: Public Provenance Intersection

This directory records implementation-level evidence for final corpus
accounting over one explicit set domain. F342 intersects observed external
content with canonical public objects before subtracting external provenance.

## Files

- `run_provenance_intersection_mpi.py`: actual Open MPI driver with an explicit
  rank-1 delay that makes one external observation disappear before its owner
  can publish it.
- `provenance-intersection-mpi.json`: structured topology, identities, exact
  namespace, output, epoch state, checks, and proof boundary.
- `provenance-intersection-mpi.log`: complete structured MPI artifact.
- `benchmark_provenance_intersection.py`: interleaved F341 counterfactual versus
  the production F342 streamed-intersection path.
- `provenance-intersection-cost.json`: every retained timing sample,
  cardinality result, summary, and proof boundary.
- `provenance-intersection-cost.log`: complete benchmark artifact.
- `directed-tests.log`: lifecycle/unit regression.
- `integration-tests.log`: six related MPI/distributed/hybrid modules.
- `full-tests.log`: every `test/test_*.py` module with warnings as errors.
- `checks.txt`: concise environment, mechanism, outcomes, and claim boundary.
- `SHA256SUMS.txt`: complete content manifest for this directory.

## Reproduce

From this directory:

```bash
python3 run_provenance_intersection_mpi.py \
  --output provenance-intersection-mpi.json \
  > provenance-intersection-mpi.log 2>&1
python3 benchmark_provenance_intersection.py \
  --objects 4096 --warmups 5 --repetitions 30 \
  --output provenance-intersection-cost.json \
  > provenance-intersection-cost.log 2>&1
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

The rank delay and observer target are deliberate evidence instrumentation.
Absolute timings depend on directory cache, host load, and storage. This is a
correctness and mechanism-cost result, not a solver, coverage, campaign,
multi-host, storage-failover, bug-discovery, or LAVA-M uplift result.
