# F411 Evidence: Loop-Carried MemoryPhi Byte-Lane Induction

This directory seals the mechanism-level evidence for F411. The feature
accepts a deliberately bounded canonical loop, proves a finite mapping from
every dynamic-load byte lane to a unique induction iteration, and leaves
actual path initializedness to the continuation runtime byte bitmap.

## Evidence inventory

| File | Meaning |
|---|---|
| `loop-memoryphi-byte-lane-oracles.json` | Independent recurrence/lane/bitmap semantics and mutations |
| `loop-memoryphi-byte-lane-benchmark.json` | Python reference-validator cost and certificate cardinality |
| `targeted-lit-llvm18.txt` | LLVM 18 focused F411 result, 15 RUN commands |
| `targeted-lit-llvm17.txt` | LLVM 17 focused F411 result, 15 RUN commands |
| `related-lit-llvm18.txt` | LLVM 18 F407--F411 regression |
| `related-lit-llvm17.txt` | LLVM 17 F407--F411 regression |
| `targeted-python.txt` | F411 oracle/generator/benchmark tests |
| `related-python.txt` | F409--F411 Python regression |
| `full-python-gate.json` | Capability-closed exact node-ID Python gate |
| `full-python-gate.log` | Human-readable Python gate result |
| `full-lit-llvm17.txt` | Complete LLVM 17 lit result |
| `test-identity-python.txt` | Python discovery-identity regression |
| `source-contract.txt` | Producer, consumer, fixture, oracle, and benchmark inventory |
| `source-research.txt` | Primary research and official LLVM sources |
| `environment.txt` | Execution environment, implementation bounds, and claim boundary |
| `static-checks.txt` | Builds, Python static checks, docs, diagram, index, and evidence checks |
| `SHA256SUMS.txt` | Complete digest manifest except itself |

## Exact measured results

- LLVM 17 complete suite: 271 passed, 2 existing unsupported, 0 failed.
- LLVM 17/18 focused F411: one lit file containing 15 `RUN` commands passed on
  each toolchain (`1 passed` is lit's file-level count).
- LLVM 17/18 related F407--F411 group: five lit files passed on each toolchain.
- Python exact-identity gate: 1,056 passed plus 250 subtests, with no skip,
  deselection, xfail, failure, or node-ID drift. The manifest digest is
  `94db3d6385abeb73051b50430d29abb4e8f4877e0777e8adcddd2402a895142a`.
- Independent oracle: 8,019 cases, 91,773 lane checks, 22,154 runtime bitmap
  equivalences, all 585 canonical covers accepted, all 7,434 noncanonical or
  partial cases rejected, and 12/12 structured mutations rejected.
- 64-byte Python reference validator: 51,116 ns/certificate median; batch
  minimum/median/maximum 48,176,739 / 51,116,708 / 55,564,197 ns.
- Generated boundary: 64 writer aliases, 57 load aliases, 456 byte-lane
  witnesses, and two MemoryPhi incoming edges.

## Claim boundary

The timing is a Python reference validator, not LLVM construction, continuation
execution, SMT solving, or fuzzing throughput. The 64/57/456/2 values are
certificate cardinalities, not state counts or speedups. This evidence does not
establish public-target coverage, solver throughput, bug yield, or end-to-end
performance. F411 is limited to a three-block, single-latch, zero-seed,
unit-step, unsigned-bound loop with one byte writer, one exit load, finite alias
domains, and one stack or heap object. MemoryPhi is treated as a may-reach
relation; the runtime byte bitmap remains authoritative for zero-trip and early
exit executions.
