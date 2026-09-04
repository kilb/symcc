# F405 Certified Finite Heap-Lifetime Union Evidence

This directory seals the 2026-08-15 mechanism evidence for compiler-certified
finite ordinary-heap points-to domains and single-state conditional lifetime
updates.

| Artifact | Meaning |
| --- | --- |
| `targeted-python.txt` | Core conditional lifetime, restore, and tamper test |
| `related-python.txt` | Heap/pointer-related distributed-state regressions |
| `targeted-lit-llvm18.txt` | Complete LLVM 18 continuation-lowering fixture |
| `full-lit-llvm17.txt` | Complete LLVM 17 lit gate |
| `full-python-gate.json` | Capability-closed exact-identity Python gate |
| `full-python-gate.log` | Human-readable full Python transcript |
| `pytest-inventory.json` | Canonical 1,041-node identity inventory |
| `heap-lifetime-union-oracles.json` | Independent finite-domain DAG oracle |
| `heap-lifetime-union-benchmark.json` | 16-object, 11-repeat mechanism cost |
| `source-research.txt` | Primary-source identities and adaptation boundary |
| `source-contract.txt` | SHA-256 identities for F405 code and contracts |
| `static-checks.txt` | Lint, compile, diagram, index, and claim gates |
| `environment.txt` | Host and toolchain context |
| `SHA256SUMS.txt` | Complete evidence-directory manifest |

The independent oracle covers domains 1 through 16 and all 256 byte values in
each domain. It independently interprets the persisted expression DAG and
performs 34,816 live-marker evaluations. It also proves direct versus paused
and fresh-executor resume equality for domain 8, and rejects foreign-base,
unsorted-domain, missing-capability, and pointer-width mutations. All checks
passed.

The final Python gate is 1,041 passed plus 250 passed subtests with no skip,
xfail, xpass, deselection, collection error, missing identity, or unexpected
identity. LLVM 17 discovered 264 tests: 262 passed and two pre-existing tests
were unsupported. The LLVM 18 continuation-lowering fixture passed 1/1.

The mechanism benchmark uses 16 allocated objects and 11 repetitions. It
executes 49 interpreted instructions with zero continuation forks. Resume cost
was 1,166,254,803 ns minimum, 1,203,925,470 ns median, and 1,459,818,673 ns
maximum on the recorded host. The `16 -> 1` comparison is analytic state
cardinality, not a measured wall-time speedup.

This is not a reproduction of POSE initial symbolic-heap materialization. It
does not establish public-target coverage, solver throughput, bug yield, or
end-to-end speedup. Alias-aware survivor MemorySSA and the native continuation
adapter remain explicit follow-up work.
