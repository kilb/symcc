# F450 evidence bundle

This directory is the immutable evidence boundary for F450 closure-bound QF_BV
proof replay authorization caching. All commands were run from the repository
root on 2026-08-25 UTC.

## Contents

- `environment.txt`: host, tool, repository, filesystem, and canonical test
  inventory identity;
- `focused-tests.log`: the complete nine-test F450 module;
- `broad-tests.log`: F450 plus incremental SAT, F449 execution, proof wire and
  QueryStore regressions;
- `full-python-gate.json`: capability-closed, exact-nodeid full Python gate;
- `oracle.json`: machine-readable ten-round 8/32/64-level replay comparison;
- `oracle.stdout.json`: stdout copy of the oracle payload;
- `oracle.time.txt`: whole oracle process timing;
- `static-checks.txt`: bytecode compilation, Ruff, XML/SVG and patch whitespace
  gates;
- `review.txt`: five review perspectives, rejected prototypes and defects
  closed;
- `SHA256SUMS.txt`: SHA-256 seal for every file above except itself.

## Supported conclusion

The bundle proves that repeated authorization of an imported project-LRUP DAG
can skip recursive JSON/LRUP replay within the same immutable bit-blast plan
while still stable-reading and hashing the entire transitive CAS dependency
closure before returning a cache hit. It does not measure SAT search, fuzzing
coverage, target throughput or defect yield.
