# F449 evidence bundle

This directory is the immutable evidence boundary for F449 proof-aware certified
QF_BV partition execution. All commands were run from the repository root on
2026-08-25 UTC.

## Contents

- `environment.txt`: host, tool, repository, filesystem, and canonical test
  inventory identity;
- `focused-tests.log`: the complete F449 test module;
- `broad-tests.log`: F449 plus proof, wire, realtime, partition, lifecycle, and
  QueryStore regressions;
- `full-python-gate.json`: capability-closed, exact-nodeid full Python gate;
- `oracle.json`: machine-readable five-round mechanism oracle;
- `oracle.stdout.json`: stdout copy of the oracle payload;
- `oracle.time.txt`: whole oracle process timing;
- `static-checks.txt`: bytecode compilation, Ruff, and patch whitespace gates;
- `review.txt`: five independent review perspectives and the defects closed;
- `SHA256SUMS.txt`: SHA-256 seal for every file above except itself.

## Supported conclusion

The bundle proves that certified cubes can be leased, solved in independent
contexts, recovered after a stale lease, cancelled after a valid SAT winner,
and aggregated into a replayable base-query UNSAT receipt. It does not measure
application speedup, fuzzing coverage, or defect yield.
