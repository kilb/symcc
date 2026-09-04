# F404 TopSeed Persistent Campaign Selection Evidence

This directory seals the 2026-08-15 mechanism evidence for the bounded,
persistent TopSeed-style cross-run seed selector and its production MPI hybrid
wiring.

| Artifact | Meaning |
| --- | --- |
| `targeted-python.txt` | TopSeed algorithm, persistence, corruption, bounds, and triage tests |
| `related-python.txt` | TopSeed plus hybrid feedback, AFL orchestration, and MPI lifecycle regressions |
| `full-python-gate.json` | Capability-closed 1041-node exact-identity Python gate |
| `full-python-gate.log` | Human-readable full Python gate transcript |
| `full-lit-llvm17.txt` | Complete LLVM 17 lit gate including the TopSeed lit entry |
| `topseed-oracles.json` | Independent group/policy/rarity/cluster/bitmap/restart oracle |
| `topseed-mechanism-benchmark.json` | 11-sample 10,000-candidate mechanism cost and fixed-target ordering |
| `pytest-inventory.json` | Canonical 1041-node identity inventory |
| `source-contract.txt` | SHA-256 identities for current F404 source and contract files |
| `source-research.txt` | Primary paper/artifact identity, defaults, divergences, and adaptation map |
| `static-checks.txt` | Lint, compile, index, diagram, whitespace, and claim checks |
| `environment.txt` | Host and toolchain context |
| `SHA256SUMS.txt` | Complete evidence-directory manifest |

The independent oracle performs 11,417 checks without calling production
scoring, rarity, clustering, or snapshot-transition helpers: 3,840 exact-group
policy evaluations, 4,344 rarity evaluations, 1,121 optimal one-dimensional
two-cluster evaluations, 2,048 bitmap-bit evaluations, and 64 exact-next-
proposal restart evaluations. All passed.

The final capability-closed gate is 1041 passed plus 250 passed subtests with
zero skip, xfail, deselection, collection error, missing identity, or unexpected
identity. LLVM 17 discovered 264 tests: 262 passed and two pre-existing tests
were unsupported.

The mechanism benchmark uses 10,000 candidates, 2,500 exact groups, 512
transactionally committed histories, and 11 repetitions. Median Explore,
Exploit, snapshot, and strict-restore costs are 30.480083 ms, 1.449271 ms,
5.312926 ms, and 54.078208 ms. The serialized state is 2,865,767 bytes. In a
deliberately favorable fixed-weight ordering model, the high-value input moves
from FIFO dispatch 501 to TopSeed dispatch 1.

This is not a reproduction of the paper's public 17-program campaign. It does
not establish coverage gain, solver throughput, bug yield, or end-to-end
speedup. AFL bucket-bit grouping, branch-trace path-condition proxies, and
worker-retained output feedback are documented framework adaptations rather
than artifact-equivalent KLEE/gcov data.
