# F430 QF_BV Native Solver-State Fork Evidence

This directory seals the implementation, correctness, failure-isolation, and
mechanism-cost evidence for F430. The authoritative report is
`../../research-progress/Native_QFBV_Solver_State_Fork_Reuse_F430_2026-08-17.md`.

## Result summary

- official Z3 5.0.0, LLVM 17.0.6 and LLVM 18.1.3;
- 8 focused tests plus 5 subtests, all passed;
- 43 related tests plus 25 subtests, all passed;
- 1,205 complete Python tests plus 278 subtests, all passed;
- LLVM 17: 312 PASS and 2 expected UNSUPPORTED out of 314;
- LLVM 18: 313 PASS and 1 expected UNSUPPORTED out of 314;
- two independent 128-target runs: zero status mismatch, zero invalid model,
  128 unique child PIDs, and timeout recovery in the same snapshot generation;
- latest-Z3 ASan+UBSan focused suite and a 32-target oracle passed.

The observed 2.252x and 2.249x cold/native ratios are limited to the recorded
same-host, one-byte-prefix mechanism experiment. They are not application-level
coverage, throughput, or bug-finding results.

## Inventory

- `oracle-run-{1,2}.json`: independent same-version cold/native runs;
- `targeted-python.xml`, `related-python.xml`: JUnit gates;
- `targeted-lit-*.json`, `llvm*-full.json`: compiler gates;
- `full-python-gate.json`, `full-suite-summary.json`: complete gate records;
- `environment.txt`, `source-contract.txt`: tool and source identities;
- `sanitizer.txt`, `static-checks.txt`, `review-findings.txt`: engineering review;
- `claim-boundary.txt`, `source-research.txt`: interpretation and prior work;
- `f430_qfbv_native_state_fork.{svg,png}`: reviewed mechanism figure;
- `SHA256SUMS.txt`: complete non-self manifest.

Run `python3 docs/codex/verify_delivery.py` from a repository checkout to verify
the manifest, source contract, oracle semantics, tests, documentation, and live
production wiring.
