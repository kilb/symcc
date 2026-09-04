# F408 Evidence: LLVM MemorySSA/AA Heap Initialization

This directory seals the mechanism-level evidence for F408. The feature walks
real LLVM MemoryUse/MemoryPhi/MemoryDef graphs and admits a bounded heap load
only when every reaching path has a full-width store after all intervening
definitions are proven NoAlias or NoModRef.

## Evidence inventory

| File | Meaning |
|---|---|
| `memoryssa-init-oracles.json` | Independent finite-domain graph semantics and mutations |
| `memoryssa-init-benchmark.json` | 64-way Python reference-proof cost and analytic state count |
| `targeted-lit-llvm18.txt` | LLVM 18 focused F408 result |
| `targeted-lit-llvm17.txt` | LLVM 17 focused F408 result |
| `full-python-gate.json` | Capability-closed exact node-ID Python gate |
| `full-python-gate.log` | Human-readable Python gate result |
| `full-lit-llvm17.txt` | Complete LLVM 17 lit result |
| `related-python.txt` | Heap/pointer-related Python regression |
| `test-identity-python.txt` | Python gate identity regression |
| `source-contract.txt` | Producer, consumer, fixture, oracle, and benchmark inventory |
| `source-research.txt` | Official LLVM and primary research sources |
| `environment.txt` | Execution environment and claim boundary |
| `static-checks.txt` | Build, syntax, diagram, docs, and evidence checks |
| `SHA256SUMS.txt` | Complete digest manifest except itself |

## Exact measured results

- LLVM 17 complete suite: 265 passed, 2 existing unsupported, 0 failed.
- LLVM 17/18 focused F408: one lit file containing 17 `RUN` commands passed on
  each toolchain (`1 passed` is lit's file-level count).
- Python exact-identity gate: 1,041 passed plus 250 subtests, with no skip,
  deselection, xfail, failure, or node-ID drift.
- Independent oracle: 504 valid graphs and 16,632 incoming/interval/NoAlias
  checks; missing store, partial width, and alias mismatch each rejected in
  16,632 cases; 8/8 executable artifact mutations rejected.
- 64-way reference formula: 65 nodes, 64 edges, median 19,661 ns/proof over
  11 batches of 10,000 iterations.

## Claim boundary

The timing is a Python reference validator, not production LLVM analysis or C++
pass latency. The 64-to-1 state count is analytic and is not a wall-time speedup.
The evidence does not establish public-target coverage, solver throughput, bug
yield, or end-to-end performance. F408 is bounded and intraprocedural; it does
not implement unbounded loop MemoryPhi reasoning, arbitrary dynamic intervals,
interprocedural memory summaries, or a complete POSE heap.
