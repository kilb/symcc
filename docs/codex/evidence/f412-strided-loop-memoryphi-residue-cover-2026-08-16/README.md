# F412 Evidence: Strided Loop MemoryPhi Residue-Class Cover

This directory seals mechanism-level evidence for F412. The feature extends
F411 with bounded positive constant induction steps, affine writer scales,
non-overlapping scalar writer intervals, and residue-class byte-lane witnesses.
Static potential cover remains separate from path-local runtime initializedness.

## Evidence inventory

| File | Meaning |
|---|---|
| `strided-loop-memoryphi-oracles.json` | Independent recurrence/residue/lane/bitmap semantics and mutations |
| `strided-loop-memoryphi-benchmark.json` | Python reference-validator cost and certificate cardinality |
| `targeted-lit-llvm18.txt` | LLVM 18 focused F412 result, 15 RUN commands |
| `targeted-lit-llvm17.txt` | LLVM 17 focused F412 result, 15 RUN commands |
| `related-lit-llvm18.txt` | LLVM 18 F411--F412 regression |
| `related-lit-llvm17.txt` | LLVM 17 F411--F412 regression |
| `targeted-python.txt` | F412 oracle/generator/benchmark tests |
| `related-python.txt` | F410--F412 Python regression |
| `full-python-gate.json` | Capability-closed exact node-ID Python gate |
| `full-python-gate.log` | Human-readable Python gate result |
| `full-lit-llvm17.txt` | Complete LLVM 17 lit result |
| `test-identity-python.txt` | Python discovery-identity regression |
| `source-contract.txt` | Producer, consumer, fixture, oracle, and benchmark inventory |
| `source-research.txt` | Primary research and official LLVM sources |
| `environment.txt` | Execution environment, implementation bounds, and claim boundary |
| `static-checks.txt` | Builds, static checks, docs, diagram, index, and evidence checks |
| `SHA256SUMS.txt` | Complete digest manifest except itself |

## Exact measured results

- LLVM 17 complete suite: 273 passed, 2 existing unsupported, 0 failed.
- LLVM 17/18 focused F412: one lit file containing 15 `RUN` commands passed on
  each toolchain (`1 passed` is lit's file-level count).
- LLVM 17/18 related F411--F412 group: two lit files passed on each toolchain.
- Python exact-identity gate: 1,061 passed plus 250 subtests, with no skip,
  deselection, xfail, failure, or node-ID drift. The manifest digest is
  `79e009242ed5cdea20e39e914abd7267e512cd073b095d65521573004bda1d31`.
- Independent oracle: 24,744 cases, 199,836 lane checks, 80,404 runtime bitmap
  equivalences, all 2,906 supported covers accepted, all 21,838 unsupported or
  partial cases rejected, and 15/15 structured mutations rejected.
- 64-byte Python reference validator: 95,151 ns/certificate median; batch
  minimum/median/maximum 94,483,740 / 95,151,368 / 105,103,552 ns.
- Generated boundary: 57 complete writer aliases, 8 recurrence-reachable
  writers, 64 covered writer bytes, 57 load aliases, 456 byte-lane witnesses,
  and two MemoryPhi incoming edges.

## Claim boundary

The timing is a Python reference validator, not LLVM construction, continuation
execution, SMT solving, or fuzzing throughput. Cardinalities are proof sizes,
not state counts or speedups. This evidence does not establish public-target
coverage, solver throughput, bug yield, or end-to-end performance. F412 remains
limited to a three-block, single-latch, zero-seed, positive constant-step loop,
one non-overlapping scalar writer, one exit load, finite aliases, one stack or
heap object, and an input-derived unsigned bound with a whole-domain no-wrap
proof. Conditional writers, descending/overlapping recurrence, multiple latches,
nested loops, pointer unions, and interprocedural loop effects fail closed.
