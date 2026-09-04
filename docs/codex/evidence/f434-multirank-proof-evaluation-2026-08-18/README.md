# F434 evidence: qualified multi-rank realtime proof evaluation

Date: 2026-08-18

This directory contains local-host MPI mechanism evidence. It does not contain
a multi-node scaling, fuzzing coverage, defect-yield, or public-benchmark claim.

## Artifacts

| File | Meaning |
| --- | --- |
| `oracle-summary.json` | one preloaded and two independent active real-CaDiCaL MPI trials |
| `trial-01-preloaded.*` | sealed result and captured process output for the protocol baseline |
| `trial-02-active.*`, `trial-03-active.*` | sealed results and output for two active-solve trials |
| `active-too-short.*` | fail-closed active-readiness negative experiment and exit status |
| `focused_tests.txt` | focused protocol/store/native test result |
| `full_python_gate.json`, `full_python_tests.txt` | capability and exact node-ID gate |
| `llvm17_lit.txt`, `llvm18_lit.txt` | complete dual-LLVM discovery/outcome summaries |
| `static_checks.txt` | Ruff, Python compile, C++ warnings and diff checks |
| `review_findings.txt` | review defects and their dispositions |
| `source_manifest.txt` | authoritative source/report/figure identities |
| `f434_multirank_proof_evaluation.{svg,png}` | role, trust and timing-boundary schematic |
| `SHA256SUMS.txt` | hashes for every evidence file except itself |

## Result summary

- Topology: 5 local MPI ranks = 1 coordinator + 2 publishers + 2 consumers.
- Workload: 2 unique rounds, 200 variables and 860 random 3-SAT clauses per round.
- Preloaded trial: 8 expected, 8 delivered, 8 independently replayed ACKs;
  publish/solve/epoch medians 27,095/699,198/774,580 us.
- Active trial 1: 8/8/8, active delivery rate 1.0;
  publish/solve/epoch medians 27,100/692,084/698,241 us.
- Active trial 2: 8/8/8, active delivery rate 1.0;
  publish/solve/epoch medians 27,319/694,693/698,626 us.
- Aggregate: 24 expected, 24 delivered, 24 root-replayed ACKs.
- The too-short active workload exits 1 because consumers cannot prove they are
  still solving at publication release.
- Regression: 33 focused tests plus 7 subtests; 1,255 complete Python tests
  plus 291 subtests; 324 lit tests discovered under each of LLVM 17 and LLVM 18,
  with no failures or unresolved tests.

The tested realtime shim SHA-256 is:

```text
fb1d08ed0c3039ddf4e5ed4ddb3d89c8ea09423b4fceb100c10fd5bc39325ba9
```

The timing values are mechanism observations from three jobs, not a statistical
speedup claim. Cross-host monotonic clocks are never subtracted.
