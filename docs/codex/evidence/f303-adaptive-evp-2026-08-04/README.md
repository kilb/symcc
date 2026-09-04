# F303 Adaptive Empirical-Domain Evidence

This directory records one bounded mechanism experiment on 2026-08-04. It is
not a coverage, throughput, or bug-finding benchmark.

## Configuration

- target: `test/empirical_value_profile_feedback.c`, compiled by the current
  LLVM 18 SymCC/QSYM build at `-O0`;
- concrete inputs: one byte `0x00` and `0x01`;
- rolling telemetry window: two records;
- profile admission: two observations and at most two distinct values;
- feedback gate: at least two actual solver queries and a validated/query ratio
  below 125,000 ppm;
- driver: `benchmark/run_evp_admission_smoke.py`.

## Result

The first generation admitted two runtime domains. For the exact 32-bit domain
`[1, 2]`, two real QSYM executions produced two Z3 queries, two solver-UNSAT
outcomes, zero validated models, zero prefilter rejects, and counters satisfying
all three conservation identities. The next generation suppressed only that
exact domain and retained one other runtime profile. Two executions using the
suppressed sidecar produced zero feedback rows for the suppressed key.

After those two fresh records displaced the failed samples from the rolling
window, the original two-domain artifact was re-admitted. A final real execution
then produced one new query for `[1, 2]`, proving bounded re-exploration rather
than permanent exclusion. No parse, context, validation, checkpoint, or
publication failure was observed.

See `summary.json` for compact counters, the three `*.runtime` files for the
admitted/suppressed/re-admitted solver views, `coordinator/generations/` for the
verified v2 proof artifacts, and the per-execution JSON files for raw telemetry.

## Interpretation

This proves the mechanism chain: real value collection -> exact-domain query
feedback -> proof-carrying suppression -> no-query suppression interval ->
rolling-window expiry -> real re-query. It does not establish end-to-end
coverage gains, solver-time gains, statistical significance, or superiority
over another system.
