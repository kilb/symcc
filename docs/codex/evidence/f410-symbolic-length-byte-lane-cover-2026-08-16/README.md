# F410 Evidence: Symbolic-Length Dynamic Byte-Lane Cover

This directory seals the mechanism-level evidence for F410. The feature lowers
a bounded symbolic-length region effect into conditional byte operations and
proves an exact finite dynamic-address/lane cover before admitting the load.

## Evidence inventory

| File | Meaning |
|---|---|
| `symbolic-length-byte-lane-oracles.json` | Independent interval/lane/guard semantics and mutations |
| `symbolic-length-byte-lane-benchmark.json` | Python reference-validator cost and bounded-effect cardinality |
| `targeted-lit-llvm18.txt` | LLVM 18 focused F410 result, 22 RUN commands |
| `targeted-lit-llvm17.txt` | LLVM 17 focused F410 result, 22 RUN commands |
| `related-lit-llvm18.txt` | LLVM 18 F406--F410 regression |
| `related-lit-llvm17.txt` | LLVM 17 F406--F410 regression |
| `targeted-python.txt` | F410 oracle/generator/benchmark tests |
| `related-python.txt` | F406--F410 Python regression |
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

- LLVM 17 complete suite: 269 passed, 2 existing unsupported, 0 failed.
- LLVM 17/18 focused F410: one lit file containing 22 `RUN` commands passed on
  each toolchain (`1 passed` is lit's file-level count).
- LLVM 17/18 related F406--F410 group: five lit files passed on each toolchain.
- Python exact-identity gate: 1,051 passed plus 250 subtests, with no skip,
  deselection, xfail, failure, or node-ID drift. The manifest digest is
  `dfecf35d97965c0f3a9bc5271f8284a64df155485e2445ad8d5ae34611e2343b`.
- Independent oracle: 48,294 cases, 364,317 lane checks, 102,004 runtime guard
  equivalences, all 7,525 covers accepted, all 40,769 non-covers rejected, and
  10/10 structured certificate mutations rejected.
- 64-alias Python reference validator: 10,309 ns/certificate median; batch
  minimum/median/maximum 9,995,838 / 10,309,694 / 11,218,936 ns.
- Generated 64-byte bounded effect: 65 bounded length values, 64 conditional
  byte writes, and zero continuation state forks. The 8-byte load boundary has
  57 aliases and 456 lane witnesses.

## Claim boundary

The timing is a Python reference validator, not LLVM construction or executor
latency. The 65 length values, 64 conditional writes, and zero state forks are
analytic/runtime mechanism properties, not a 65x speedup. This evidence does
not establish public-target coverage, solver throughput, bug yield, or
end-to-end performance. F410 is limited to one bounded static-base region
writer, capacity at most 64 bytes, one stack object or one guarded heap
alternative, a linear MemoryDef chain, at most 256 aliases and 2,048 lanes.
It does not implement loop-carried MemoryPhi induction, multiple region-effect
composition, dynamic writer bases, a complete MInt lazy memory model, or
general symbolic memory.
