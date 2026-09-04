# F367 Sealed Query Artifact FD Handoff Evidence

This directory records the deterministic path-race counterexample and the
production evidence for immutable lease snapshots, one-shot fd inheritance,
and persistent `SCM_RIGHTS` descriptor handoff.

## Recorded Evidence

- `run_sealed_query_fd_checks.py`: executable old path-only counterfactual,
  sealed snapshot, one-shot helper, persistent receiver, and allocation-failure
  driver;
- `adversarial-cases.json` and `.log`: exact digests, seal masks, request-id
  binding, descriptor counts, and rollback state;
- `native-helper.log`: C++17 syntax gate, real `symcc-query-solver` build, and
  real one-shot/persistent SAT results after replacing all CAS pathnames;
- `lit-tests.log`: ten query/string lit tests, including two path replacements
  around a real persistent prefix-cache miss and hit;
- `directed-tests.log` and `affected-tests.log`: QueryStore and affected Python
  regression layers;
- `inventory-rebuild.log`: byte-identical canonical 787-node inventory;
- `full-gate.json` and `.log`: exact-identity, 16-capability full Python gate;
- `static-checks.log`: Python/C++ static gates, diagram, hashes, historical
  replay, and delivery verification.

## Interpretation

The old path-only consumer reads digest `69b0c4...` after the verified path is
replaced, although the lease identifies `614fe2...`. Production instead copies
all three verified artifacts into memfds with seal mask `15`. Replacing full,
prefix, and target CAS paths does not change any snapshot digest. The one-shot
helper consumes the inherited full fd; the persistent helper receives exactly
two sealed fds bound to the textual request id. If the second memfd allocation
fails, the first fd is closed and the query remains `pending` with zero
attempts.

`SHA256SUMS.txt` covers every regular file in this directory except itself.
This is local Linux correctness evidence. It includes a real local query-solver
build and filtered lit execution, but it is not hosted CI, cross-host/shared-
filesystem qualification, a vendored QSYM/PIN tracer run, a real MPI campaign,
CAS garbage collection, a public benchmark, LAVA-M, or a performance/coverage
uplift measurement.
