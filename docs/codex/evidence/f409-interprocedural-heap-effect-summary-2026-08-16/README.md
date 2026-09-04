# F409 Evidence: Callsite-Instantiated Interprocedural Heap Effects

This directory seals the mechanism-level evidence for F409. The feature joins a
caller MemorySSA chain to one bounded direct-callee effect and instantiates that
effect at the exact callsite before admitting an ordinary-heap load.

## Evidence inventory

| File | Meaning |
|---|---|
| `interprocedural-effect-oracles.json` | Independent interval/callsite semantics and mutations |
| `interprocedural-effect-benchmark.json` | 64-callsite Python reference-validator cost and relation counts |
| `targeted-lit-llvm18.txt` | LLVM 18 focused F409 result |
| `targeted-lit-llvm17.txt` | LLVM 17 focused F409 result |
| `related-lit-llvm18.txt` | LLVM 18 F406--F409 regression |
| `related-lit-llvm17.txt` | LLVM 17 F406--F409 regression |
| `targeted-python.txt` | F409 oracle/generator/benchmark unit tests |
| `related-python.txt` | Heap/pointer/F409 Python regression |
| `full-python-gate.json` | Capability-closed exact node-ID Python gate |
| `full-python-gate.log` | Human-readable Python gate result |
| `full-lit-llvm17.txt` | Complete LLVM 17 lit result |
| `test-identity-python.txt` | Python test-identity regression |
| `source-contract.txt` | Producer, consumer, fixture, oracle, and benchmark inventory |
| `source-research.txt` | Official LLVM and primary research sources |
| `environment.txt` | Execution environment and claim boundary |
| `static-checks.txt` | Build, syntax, diagram, docs, and evidence checks |
| `SHA256SUMS.txt` | Complete digest manifest except itself |

## Exact measured results

- LLVM 17 complete suite: 267 passed, 2 existing unsupported, 0 failed.
- LLVM 17/18 focused F409: one lit file containing 21 `RUN` commands passed on
  each toolchain (`1 passed` is lit's file-level count).
- LLVM 17/18 related F406--F409 group: four lit files passed on each toolchain.
- Python exact-identity gate: 1,046 passed plus 250 subtests, with no skip,
  deselection, xfail, failure, or node-ID drift.
- Independent oracle: 182,196 interval instantiations; all 49,896 valid covers
  accepted; 132,300 non-covers, 49,896 wrong-callsite bindings, and 8/8
  executable artifact mutations rejected.
- 64-callsite reference validator: 816 ns/certificate median and 52,226,581 ns
  median per 64,000-certificate batch. Analytic relation checks are 4,096 for
  a context-insensitive cross product and 64 after callsite instantiation.

## Claim boundary

The timing is a Python reference validator, not production LLVM analysis or C++
pass latency. The 4,096-to-64 relation count is analytic and is not a wall-time
speedup. This evidence does not establish public-target coverage, solver
throughput, bug yield, or end-to-end performance. F409 is limited to finite
direct callsites, one straight-line callee effect, constant parameter-relative
offsets, at most 256 returned heap alternatives, and exact artifact identity.
It does not implement recursion, indirect
calls, conditional/multiple writers, dynamic intervals, exceptional
postconditions, cross-module summaries, or complete POSE.
