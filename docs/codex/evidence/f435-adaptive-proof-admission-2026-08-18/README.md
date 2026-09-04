# F435 evidence: adaptive checked-proof admission

Date: 2026-08-18

This directory preserves I/T/E-local evidence for deterministic proof-first
admission and bounded backpressure recovery. It does not contain a solver
speedup, fuzzing coverage, defect-yield, network, or multi-node claim.

## Artifacts

| File | Meaning |
| --- | --- |
| `oracle-run-{1..6}.json/.stdout` | six independent real-CaDiCaL synchronized-burst trials |
| `focused_tests.txt` | adaptive controller, realtime and multi-rank regression result |
| `full_python_gate.json`, `full_python_tests.txt` | capability and exact node-ID gate |
| `llvm17_lit.txt`, `llvm18_lit.txt` | complete dual-LLVM discovery/outcome logs |
| `full_suite_summary.json` | compact agreement record for all gates |
| `static_checks.txt` | Python, Ruff, C++, figure and diff checks |
| `review_findings.txt` | three review rounds and dispositions |
| `research_sources.md` | primary research sources and implementation boundary |
| `source_manifest.txt` | current authoritative source/report/figure identities |
| `f435_adaptive_proof_admission.{svg,png}` | proof/admission/feedback schematic |
| `SHA256SUMS.txt` | every evidence file except the manifest itself |

## Result summary

- Six independent processes use the same 200-variable/860-clause plan shape,
  eight checked and canonically distinct records, and queue capacity two.
- Every static run settles 8/8 candidates, delivers 2/8, and records six
  backpressure drops.
- Every adaptive run settles 8/8, first defers 8, retries 8, delivers 8/8, and
  records zero native backpressure or final deferred records.
- This constructed pressure case changes delivery from 25% to 100%: +6 clauses,
  +75 percentage points, and 4.0x the static delivery count.
- Focused regression: 31 passed.
- Complete Python: 1,267 passed plus 291 passed subtests, exact node-ID match,
  no skipped/failed/deselected/xfail/xpass/collection-error outcome.
- LLVM 17: 325 discovered, 323 passed, 2 expected unsupported.
- LLVM 18: 325 discovered, 324 passed, 1 expected unsupported.

The tested realtime shim SHA-256 is:

```text
fb1d08ed0c3039ddf4e5ed4ddb3d89c8ea09423b4fceb100c10fd5bc39325ba9
```

Solve wall times are retained in raw JSON for audit but were not randomized and
are not used as an efficacy metric.
