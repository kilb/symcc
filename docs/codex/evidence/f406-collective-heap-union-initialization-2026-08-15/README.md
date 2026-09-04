# F406 Collective Heap-Union Initialization Evidence

This directory seals the 2026-08-15 mechanism evidence for collective
dominating initialization of finite ordinary-heap pointer unions and the
post-symbolic-free survivor load.

| Artifact | Meaning |
| --- | --- |
| `targeted-lit-llvm18.txt` | LLVM 18 successful/partial/non-dominating fixture and four tamper checks |
| `targeted-lit-llvm17.txt` | The same focused fixture under LLVM 17 |
| `related-python.txt` | Existing heap/pointer consumer regressions |
| `test-identity-python.txt` | Python gate and discovery-contract regressions |
| `collective-init-oracles.json` | Independent finite-domain cover/lifetime/tamper oracle |
| `collective-init-benchmark.json` | 32-object, 11-repeat reference-proof cost |
| `full-python-gate.json` | Capability-closed exact-identity Python gate |
| `full-python-gate.log` | Human-readable full Python transcript |
| `full-lit-llvm17.txt` | Complete LLVM 17 lit gate |
| `pytest-inventory.json` | Canonical 1,041-node identity inventory |
| `source-research.txt` | Primary-source identities and adaptation boundary |
| `source-contract.txt` | SHA-256 identities for F406 code and contracts |
| `static-checks.txt` | Build, lint, document, diagram, oracle, and claim gates |
| `environment.txt` | Host and toolchain context |
| `SHA256SUMS.txt` | Complete evidence-directory manifest |

The independent oracle covers domains 2 through 32 and load widths 1 through
8. It executes 4,216 object-interval checks and 10,912 distinct victim/survivor
assignments. It also checks 248 cases rejected by the historical single-store
rule, 248 incomplete covers, 248 non-dominating covers, and four production
artifact-validator mutations. All expected outcomes passed.

The final Python gate is 1,041 passed plus 250 passed subtests with no skip,
xfail, xpass, deselection, collection error, missing identity, or unexpected
identity. LLVM 17 discovered 265 tests: 263 passed and two pre-existing tests
were unsupported. The focused F406 fixture passed 1/1 under both LLVM 17 and
LLVM 18.

The reference benchmark uses 32 object bases, 32 dominating store witnesses,
11 repetitions, and 10,000 proof evaluations per repetition. Batch cost was
39,691,970 ns minimum, 39,866,532 ns median, and 44,144,475 ns maximum; the
reported median was 3,986 ns per reference proof. This measures the independent
Python formula, not production C++ lowering. The `992 -> 1` value is analytic
state cardinality, not measured wall-time speedup.

This is not a complete POSE or MemorySSA implementation. It does not support
path-correlated branch-local stores, arbitrary symbolic heaps, or establish
public-target coverage, solver throughput, bug yield, or end-to-end speedup.
