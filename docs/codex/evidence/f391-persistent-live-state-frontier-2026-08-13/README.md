# F391 executable evidence

This directory binds mechanism evidence for the lease-fenced persistent
live-state frontier. It is not an edge-coverage, solver-speed, throughput,
bug-finding, LAVA-M, or public-benchmark result.

- `targeted-python.txt` covers 19 protocol/executor tests plus 6 subtests.
- `related-live-python.txt` covers 109 live-state tests plus 57 subtests.
- `concurrency-repeat.txt` repeats the two-executor ownership test ten times.
- `full-python-gate.json` proves 938/938 canonical pytest identities passed with
  zero skip, xfail, xpass, deselection, missing identity, or unexpected identity.
- `full-lit.txt` records 250 discovered tests: 249 passed and 1 unsupported.
- `frontier-microbenchmark.json` is a 31-sample local mechanism-cost run with
  file and directory fsync on the current overlay filesystem.
- `static-checks.txt` records syntax, Ruff, whitespace, and generated-index
  gates. `environment.txt` binds the measurement environment.

The first warnings-as-errors full run exposed delayed cleanup of incremental
solver artifacts. F391 closes that review finding with an idempotent explicit
cleanup/finalizer path and a forced-GC regression. No threshold was relaxed.

The frontier is one canonical JSON file and intentionally bounded. It is suited
to local or small shared deployments; it is not a sharded cluster database and
does not replace the MPI helper's separate distributed work protocol.
