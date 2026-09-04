# F413 Evidence: Conditional Loop MemoryPhi Guard Carry

This directory seals mechanism-level evidence for F413. The feature extends
F411/F412 with a bounded single-latch conditional writer, an exact nested
MemoryPhi transcript, writer-edge polarity, and guard-carry byte-lane witnesses.
Static potential cover remains separate from path-local runtime initializedness.

## Evidence inventory

| File | Meaning |
|---|---|
| `conditional-loop-memoryphi-oracles.json` | Independent guard/recurrence/lane/bitmap semantics and mutations |
| `conditional-loop-memoryphi-benchmark.json` | Python reference-validator cost and guarded certificate cardinality |
| `targeted-lit-llvm18.txt` | LLVM 18 focused F413 result, 15 RUN commands |
| `targeted-lit-llvm17.txt` | LLVM 17 focused F413 result, 15 RUN commands |
| `related-lit-llvm18.txt` | LLVM 18 F411--F413 regression |
| `related-lit-llvm17.txt` | LLVM 17 F411--F413 regression |
| `targeted-python.txt` | F413 oracle/generator/benchmark tests |
| `related-python.txt` | F410--F413 Python regression |
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

- LLVM 17 complete suite: 275 passed, 2 existing unsupported, 0 failed.
- LLVM 17/18 focused F413: one lit file containing 15 `RUN` commands passed on
  each toolchain. LLVM 17/18 related F411--F413 groups passed 3/3 each.
- Python exact-identity gate: 1,066 passed plus 250 subtests, with no skip,
  deselection, xfail, failure, or node-ID drift. The manifest digest is
  `5b51e98e49435739615fb1e3ffc8985e1b8834cd380e76c48331a3ff94b0e06a`.
- Independent oracle: 126,624 cases, 768,672 lane checks, 551,712 runtime
  bitmap equivalences, all 20,640 supported covers accepted, all 105,984
  unsupported/partial cases rejected, and 18/18 mutations rejected.
- Runtime split inside those equivalences: 183,532 guard-true/prefix-complete
  accepts and 368,180 guard-false or prefix-incomplete rejects.
- Generated 64-byte boundary: 57 complete writer aliases, 8 reachable writers,
  64 potential writer bytes, 57 load aliases, 456 guarded lane witnesses, two
  MemoryPhi nodes, and four incoming edges.
- 64-byte Python reference validator: 149,259 ns/certificate median; batch
  minimum/median/maximum 147,800,439 / 149,259,299 / 160,534,540 ns.

## Claim boundary

The timing is a Python reference validator, not LLVM construction, continuation
execution, SMT solving, or fuzzing throughput. Cardinalities are proof sizes,
not state counts or speedups. This evidence does not establish public-target
coverage, solver throughput, bug yield, or end-to-end performance. F413 is
limited to an exact five-block, single-latch, single-layer integer-`icmp`
conditional writer with one non-overlapping scalar store and finite aliases.
Multi-latch, nested-predicate, two-writer, unknown-memory-effect, pointer-union,
dynamic-extent, and overlapping-writer shapes fail closed.
