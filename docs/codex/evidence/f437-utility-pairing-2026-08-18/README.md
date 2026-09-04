# F437 Utility-Aware Proof-Worker Pairing Evidence

This directory seals the implementation, regression gates, and local paired
mechanism evidence for F437. The controller may suppress only a clause that
has already passed the project proof/event checks; it cannot authorize an
unchecked clause or an UNSAT result.

## Evidence inventory

| Artifact | Purpose |
| --- | --- |
| `ablation-summary.json` | Sealed two-seed paired baseline/treatment result |
| `seed-*-baseline.json` | Full-delivery local five-rank trials |
| `seed-*-pairing.json` | Utility-pairing local five-rank trials |
| `full_python_gate.json` | Capability-closed Python and exact node-ID gate |
| `llvm{17,18}_lit.txt` | Complete cross-LLVM lit outputs |
| `focused_tests.txt` | Controller, stream, store and multi-rank regressions |
| `full_suite_summary.json` | Machine-readable result and claim boundary |
| `review_findings.txt` | Four review rounds and repaired findings |
| `research_sources.md` | Primary research sources and scope comparison |
| `source_manifest.txt` | SHA-256 contract for authoritative F437 paths |
| `static_checks.txt` | Syntax, lint, generated-index, figure and diff gates |
| `delivery_verifier.txt` | Complete Codex document/evidence integrity gate |
| `f437_utility_aware_pairing.{svg,png}` | Canonical mechanism diagram |

`SHA256SUMS.txt` covers every regular file in this directory except itself.

## Sealed local result

- Python: 1,290 passed plus 291 passed subtests; exact 1290/1290 node-ID
  equality and no failure, skip, xfail, xpass, deselection, or collection
  error.
- Focused F437 tests: 68 passed plus 25 passed subtests.
- LLVM 17: 327 discovered, 325 passed, two expected unsupported; LLVM 18: 327
  discovered, 326 passed, one expected unsupported. Both have zero failed or
  unresolved tests.
- Seeds 62519 and 128056 use identical formula, CNF, library, process count,
  publisher count, round count, and solve budget inside each pair.
- Across 24 opportunities, pairing admits/delivers 20 and suppresses four
  native imports (16.7%). Both groups observe 12 unit/conflict activations.
  Unactivated deliveries fall from 12 to eight and the fixture activation rate
  changes from 50% to 60%.
- Aggregate solve time changes from 15,884,765 us to 15,819,372 us, about
  -0.41%; one seed improves and one regresses. No speedup claim is made.

## Reproduction

Build the pinned CaDiCaL 3.0.1 realtime bridge, then run:

```bash
python3 benchmark/check_qfbv_utility_pairing_oracles.py \
  --library /path/to/libsymcc_qfbv_cadical_realtime.so \
  --output-dir /tmp/f437-pairing \
  --processes 5 --publishers 2 --rounds 3 --seed 62519 \
  --variables 200 --clauses 860 --repetitions 2 \
  --solve-timeout-ms 30000 --qualification-timeout 30
```

The MPI qualification in every trial is `same-host-subprocess-v1` with
`distributed_filesystem=false` and `cluster_lock_verified=false`. These
artifacts are local multi-process evidence, not cross-host scaling evidence.

## Claim boundary

The evidence proves deterministic decision/outcome/snapshot replay, proof-
first suppression, exactly-once settlement, durable restart recovery, and
opportunity/admit/delivery/activity accounting. Native activation is a
semantic opportunity, not proof that one clause uniquely caused progress.
Nothing here establishes solver speedup, fuzzing coverage, defect yield,
network efficiency, causal clause utility, or 8/32/128-worker scalability.
