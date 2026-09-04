# F414 Evidence: Multi-Latch MemoryPhi Fixed Point

This directory seals mechanism-level evidence for F414. The feature extends
F411--F413 from one backedge to a bounded two-to-four-latch MemoryPhi SCC. It
models writer and carry backedges as mutually exclusive transfers, computes a
finite monotone potential-byte closure, and keeps actual-path initializedness
under the runtime byte bitmap.

## Evidence inventory

| File | Meaning |
|---|---|
| `multilatch-loop-memoryphi-oracles.json` | Independent transfer/fixed-point/lane/bitmap semantics and mutations |
| `multilatch-loop-memoryphi-benchmark.json` | Python reference-validator cost and certificate cardinality |
| `targeted-lit-llvm18.txt` | LLVM 18 focused F414 result, 17 RUN commands |
| `targeted-lit-llvm17.txt` | LLVM 17 focused F414 result, 17 RUN commands |
| `related-lit-llvm18.txt` | LLVM 18 F411--F414 regression |
| `related-lit-llvm17.txt` | LLVM 17 F411--F414 regression |
| `targeted-python.txt` | F414 oracle/generator/benchmark tests |
| `related-python.txt` | F411--F414 Python regression |
| `full-python-gate.json` | Capability-closed exact node-ID Python gate |
| `full-python-gate.log` | Human-readable Python gate result |
| `full-lit-llvm17.txt` | Complete LLVM 17 lit result |
| `test-identity-python.txt` | Python discovery-identity regression |
| `source-contract.txt` | Producer, consumer, fixture, oracle, and benchmark inventory |
| `source-research.txt` | Primary research and official LLVM sources |
| `environment.txt` | Execution environment, implementation bounds, and claim boundary |
| `static-checks.txt` | Builds, static checks, docs, diagram, index, and evidence checks |
| `SHA256SUMS.txt` | Complete digest manifest except itself |

## Exact semantic results

- LLVM 17/18 focused F414: one lit file containing 17 `RUN` commands passed on
  each toolchain. Related F411--F414 groups passed 4/4 on both toolchains.
- Complete Python exact-identity gate: 1,071 passed plus 250 subtests, with no
  skip, xfail, deselection, missing/unexpected node ID, or failure. Manifest
  digest: `ddabe1a48648891f1b23c12e69a77d908cc622f2e4ea83a0c5600e2f7f3496c9`.
- Complete LLVM 17 lit gate: 277 passed, 2 existing unsupported, 0 failed.
- Independent oracle: 42,208 cases, 256,224 byte-lane checks and 633,552
  runtime bitmap equivalences. All 6,880 supported fixed points were accepted,
  all 35,328 unsupported/partial cases rejected, and 18/18 mutations rejected.
- Runtime split: 345,284 writer-path/prefix-complete accepts and 288,268 carry
  or prefix-incomplete rejects.
- Generated 64-byte boundary: 57 aliases and 8 recurrence-reachable writers
  per writer transfer, 128 potential writer bytes, 57 load aliases, 456 lane
  witnesses, 912 alternatives, 3 decisions, 4 backedges, 1 MemoryPhi with 5
  incoming edges, and 8 closure rounds plus 1 stability round.
- 64-byte Python reference validator: 104,156 ns/certificate median; batch
  minimum/median/maximum 103,406,795 / 104,156,971 / 111,543,685 ns.

The machine-readable gate, oracle, benchmark, and SHA-256 files are
authoritative for these counts.

## Claim boundary

The benchmark is a Python reference-validator measurement, not LLVM analysis,
continuation execution, SMT solving, fuzzing throughput, coverage, defect
yield, or end-to-end speedup. Cardinalities are proof sizes, not state-count
reductions. F414 is limited to a single reducible loop with a join-free binary
decision tree, two through four direct latches, one direct simple store per
writer latch, common positive constant recurrence, finite aliases, and
initializedness-only effects. Nested loops, ordered multiwriter transfers,
value/last-write summaries, pointer unions, multiple objects, non-tree control
flow, dynamic extents, and interprocedural loop summaries fail closed.
