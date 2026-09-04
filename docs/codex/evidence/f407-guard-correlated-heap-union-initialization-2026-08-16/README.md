# F407 Evidence: Guard-Correlated Heap-Union Initialization

This directory seals the mechanism-level evidence for F407. The feature admits
bounded acyclic control-flow regions in which each path initializes the heap
object selected by that same path at a merge load.

## Evidence inventory

| File | Meaning |
|---|---|
| `guarded-init-oracles.json` | Independent finite-domain semantics and mutation results |
| `guarded-init-benchmark.json` | 64-path Python reference-proof cost and analytic state count |
| `targeted-lit-llvm18.txt` | LLVM 18 focused F406/F407 result |
| `targeted-lit-llvm17.txt` | LLVM 17 focused F406/F407 result |
| `full-python-gate.json` | Capability-closed exact node-ID Python gate |
| `full-python-gate.log` | Human-readable Python gate result |
| `full-lit-llvm17.txt` | Complete LLVM 17 lit result |
| `related-python.txt` | Heap/pointer-related Python regression |
| `test-identity-python.txt` | Focused checker identity regression |
| `source-contract.txt` | Producer, consumer, test, oracle, and benchmark contract inventory |
| `source-research.txt` | Primary research and official engineering sources |
| `environment.txt` | Execution environment and explicit claim boundary |
| `static-checks.txt` | Build, lint, syntax, diagram, docs, and evidence checks |
| `SHA256SUMS.txt` | Complete digest manifest for this directory except itself |

## Exact measured results

- LLVM 17 complete suite: 264 passed, 2 existing unsupported, 0 failed.
- LLVM 17/18 focused F406+F407: 2 passed on each toolchain.
- Python exact-identity gate: 1,041 passed plus 250 subtests, no skip,
  deselection, xfail, or identity drift.
- Independent oracle: 48 valid trees, 1,008 path assignments and interval
  checks; missing store, guard mismatch, and partial width each rejected in
  1,008 cases; 10/10 artifact mutations rejected.
- Depth-6 reference formula: 64 paths, 384 decisions, median 15,214 ns/proof
  over 11 batches of 10,000 iterations.

## Claim boundary

The timing is a Python reference formula, not production C++ pass latency. The
64-to-1 state count is analytic and is not a wall-time speedup. These artifacts
do not establish public-target coverage, solver throughput, bug yield, or
end-to-end performance. F407 is MemoryPhi-inspired but does not serialize a
general LLVM MemorySSA/AA clobber chain and is not a complete POSE
implementation.
