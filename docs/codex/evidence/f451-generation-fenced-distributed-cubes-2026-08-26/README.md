# F451 evidence: generation-fenced distributed certified cubes

This directory seals the implementation, regression, mechanism, and physical
cross-host adapter evidence for F451.

## Claim supported

An F449 certified cube can be executed as an F447 generation-fenced work item.
Only a result whose inner cube token and outer generation/shard/work lease are
both current may change query state.  Recovery re-fences every ambiguous
in-flight cube, including survivor-owned work, without charging an additional
semantic solve attempt.  SAT cancels all current peers; complete UNSAT leaves
are still aggregated through the checked F448 split tree.

The physical cross-host oracle uses SSH only as an application-level transport.
It proves request/response binding and recovery across two physical hosts; it
does not claim new MPI/ULFM latency, solver speedup, fuzzing coverage, or defect
yield.  F447 separately supplies the physical ULFM communicator-repair evidence.

## Artifacts

- `focused-tests.log`: 15 F451 tests.
- `broad-tests.log`: 127 coupled tests and 38 subtests.
- `full-python-gate.json`: identity-exact 1443-test capability gate.
- `oracle.json`: five-round executable logical-topology oracle.
- `crosshost-oracle.json`: three-round two-physical-host adapter oracle.
- `oracle.time.txt`, `crosshost.time.txt`: wall-clock command measurements.
- `oracle.stdout.log`, `oracle.stderr.log`, `crosshost.stdout.log`,
  `crosshost.stderr.log`: empty by design because the drivers commit canonical
  JSON to their explicit output files and emitted no diagnostics.
- `environment.txt`: host, interpreter, tool, and nodeid identities.
- `review.txt`: six review rounds and the corresponding fixes.
- `static-checks.txt`: compile, lint, SVG, pixel, and whitespace gates.
- `SHA256SUMS.txt`: complete manifest over every artifact except itself.

## Reproduction

```bash
python3 -m pytest -q test/test_qfbv_distributed_partition_execution.py

python3 -m pytest -q \
  test/test_qfbv_distributed_partition_execution.py \
  test/test_qfbv_partition_execution.py \
  test/test_mpi_ulfm_recovery.py \
  test/test_qfbv_proof_prefix_partition.py \
  test/test_qfbv_incremental_sat.py \
  test/test_query_store.py

python3 benchmark/check_qfbv_distributed_partition_oracles.py \
  --rounds 5 --cube-count 8 --output /tmp/f451-oracle.json

python3 benchmark/check_qfbv_distributed_crosshost_oracles.py \
  --rounds 3 --remote root@down.kew.ac \
  --output /tmp/f451-crosshost.json
```

The cross-host command requires explicit access to the named research host.

