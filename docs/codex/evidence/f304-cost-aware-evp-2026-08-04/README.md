# F304 Cost-Aware Empirical-Domain Evidence

This directory records one bounded mechanism experiment on 2026-08-04. It is
not a coverage, throughput, or bug-finding benchmark.

## Configuration

- target: `test/empirical_value_profile_feedback.c`, compiled by the current
  LLVM 18 SymCC/QSYM build at `-O0`;
- concrete inputs: one byte `0x00` and `0x01`;
- rolling telemetry window: two records;
- query/ratio gate: at least two queries and validated/query below 125,000 ppm;
- primary cost floor: 0 us, used to exercise suppression;
- counterfactual cost floor: measured exact-domain cost plus 1 us;
- driver: `benchmark/run_evp_admission_smoke.py`.

## Result

The real QSYM runs emitted two 12-field feedback rows for the exact 32-bit
domain `[1, 2]`. They contain two solver queries, two solver-UNSAT outcomes,
zero validated models, zero prefilter rejects, and 142 us of cumulative solver
time. All count conservation identities hold, and the global empirical-domain
solver time equals the bounded exact-domain total in this fixture.

With a 0 us cost floor, the v3 admission artifact suppressed the domain and
retained one other runtime profile. Replaying the same two raw telemetry
documents through a 143 us floor retained both profiles and produced no
suppression proof. This isolates the cost predicate: query count, outcomes,
profile values, executable identity, and rolling window are identical.

The original suppression/re-exploration chain also remains valid: suppressed
executions emitted zero rows for the key, two fresh records re-admitted it, and
a final execution queried it again.

## Interpretation

The evidence proves real per-domain timing, v3 proof construction, cost-gated
admission, legacy state-machine behavior, and counterfactual policy replay. It
does not establish that the default 1,000 us threshold improves end-to-end
solver time or coverage on public targets; that requires multi-run ablation.
