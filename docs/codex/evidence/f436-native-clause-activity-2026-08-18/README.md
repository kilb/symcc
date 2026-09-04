# F436 Native Clause Activity Evidence

This directory seals the implementation and local mechanism evidence for
F436. It records consumer-side unit/conflict activation of proof-checked
clauses after native delivery. An activity receipt is a bounded telemetry
artifact, not proof that one imported clause uniquely caused a propagation or
improved solving, coverage, or defect yield.

## Evidence inventory

| Artifact | Purpose |
| --- | --- |
| `oracle-run-{1,2}.{json,stdout}` | Independent 128-case real-CaDiCaL runs |
| `oracle-asan.{json,stdout}` | 32-case ASan/UBSan run |
| `mpi-active-run-{1,2}.{json,stdout}` | Independent local five-rank active-solve runs |
| `full_python_gate.json` | Capability-closed Python gate and node-ID identity |
| `llvm{17,18}_lit.txt` | Complete dual-LLVM lit output |
| `focused_tests.txt` | F436-focused Python result |
| `review_regression_tests.txt` | Portfolio cancellation regression result |
| `full_suite_summary.json` | Machine-readable result and claim summary |
| `review_findings.txt` | Four review rounds and repaired findings |
| `research_sources.md` | Primary research and upstream implementation sources |
| `source_manifest.txt` | SHA-256 contract for authoritative F436 paths |
| `static_checks.txt` | Syntax, lint, C++, shell, image and diff gates |
| `f436_native_clause_activity.{svg,png}` | Canonical execution/trust-boundary diagram |

`SHA256SUMS.txt` covers every regular file in this directory except itself.

## Sealed results

- Python: 1,274 passed plus 291 passed subtests; no failures, skips, xfails,
  xpasses, deselection, collection errors, missing node IDs, or unexpected
  node IDs.
- LLVM 17: 326 discovered, 324 passed, two expected unsupported, zero failed
  or unresolved.
- LLVM 18: 326 discovered, 325 passed, one expected unsupported, zero failed
  or unresolved.
- Focused F436 tests: 27 passed. Review regressions: 14 passed.
- Real CaDiCaL runs with seeds 62518 and 127645: each has 128/128 checked
  deliveries, unit receipts, independent replays, and tamper rejections.
- ASan/UBSan: 32/32 cases and no sanitizer diagnostic.
- Local MPI runs: each has 4/4 delivery, two unit activations, zero conflicts,
  and two unactivated imports. Both runs together separate 8/8 delivery from
  4/8 semantic activation.

## Reproduction

Build the pinned CaDiCaL 3.0.1 bridge, then run:

```bash
python3 benchmark/check_qfbv_clause_activity_oracles.py \
  --library /path/to/libsymcc_qfbv_cadical_realtime.so \
  --cases 128 --seed 62518 --output /tmp/f436-oracle.json

mpiexec -n 5 python3 benchmark/run_qfbv_realtime_multirank.py \
  --library /path/to/libsymcc_qfbv_cadical_realtime.so \
  --proof-root /tmp/f436-mpi \
  --publishers 2 --rounds 1 --variables 200 --clauses 860 \
  --mode active --track-clause-activity \
  --seed 62518 --output /tmp/f436-mpi.json
```

The preserved MPI filesystem qualification says
`same-host-subprocess-v1`, `distributed_filesystem=false`, and
`cluster_lock_verified=false`. The MPI artifacts therefore establish local
multi-process protocol behavior, not multi-node scaling.

## Claim boundary

The oracle verifies exact receipt identity, proof/event/ACK replay, activity
witness semantics, native state variants, and lifecycle fencing. The MPI runs
show that native delivery and semantic activation are distinct observables.
No artifact in this directory establishes a public-workload utilization rate,
unique clause causality, solver speedup, fuzzing coverage, vulnerability yield,
network efficiency, or cross-host scalability.
