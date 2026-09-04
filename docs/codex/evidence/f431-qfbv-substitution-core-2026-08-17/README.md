# F431 Variable-Substitution QF_BV UNSAT-Core Evidence

This directory seals the implementation, correctness, failure-closure, and
mechanism-cost evidence for F431. The authoritative report is
`../../research-progress/Variable_Substitution_UNSAT_Core_Reuse_F431_2026-08-17.md`.

## Result summary

- pinned cvc5 1.3.4, Ethos/CPC bundle, LLVM 17.0.6 and LLVM 18.1.3;
- 38 focused tests, all passed;
- 101 related tests plus 45 subtests, all passed;
- 1,221 complete Python tests plus 278 subtests, all passed with exact identity;
- LLVM 17: 315 PASS and 2 expected UNSUPPORTED out of 317;
- LLVM 18: 316 PASS and 1 expected UNSUPPORTED out of 317;
- two independent 512-case exhaustive-mapping oracles: 294 positive, 218
  negative, 97 non-injective-positive, and zero mismatch per run;
- 64-round warm mechanism ratios: 13.861x for 2-clause targets and 5.973x for
  130-clause targets; fresh-consumer cold replay was slower in both profiles;
- debug allocator, development mode, warnings-as-errors, ruff and py_compile
  gates passed.

These measurements describe proof/core matching and amortization only. They do
not establish fuzzing coverage, defect yield, multi-node throughput, arbitrary
SMT-sort support, or reproduction of the reuse rate in the source paper.

## Inventory

- `oracle-run-{1,2}.json`: independent exhaustive-mapping cross-oracles;
- `benchmark-padding{0,128}.json`: cold and warm proof-reuse profiles;
- `targeted-python.xml`, `related-python.xml`: JUnit gates;
- `full-python-gate.json`, `full-suite-summary.json`: complete gate records;
- `llvm{17,18}-full.json`: complete compiler gates;
- `debug-allocator.txt`, `static-checks.txt`: engineering gates;
- `environment.txt`, `source-contract.txt`: tool and source identities;
- `claim-boundary.txt`, `source-research.txt`, `review-findings.txt`: scope,
  prior work, and review record;
- `f431_qfbv_substitution_core_reuse.{svg,png}`: reviewed mechanism figure;
- `SHA256SUMS.txt`: complete non-self manifest.

Run `python3 docs/codex/verify_delivery.py` from a repository checkout to verify
the manifest, source contract, oracle semantics, tests, documentation, figures,
and live production wiring.
