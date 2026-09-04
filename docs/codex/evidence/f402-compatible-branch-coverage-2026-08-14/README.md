# F402 Compatible Branch Coverage Evidence

This directory seals the 2026-08-14 mechanism evidence for F402. It covers
function-local data/control-dependence closure, compatible-branch grouping,
pressure-gated fork-time pruning before CAS publication, snapshot-v4 restart,
and persistent-worker pressure propagation.

| Artifact | Meaning |
| --- | --- |
| `targeted-python.txt` | CBC, snapshot, and persistent-integration tests |
| `related-python.txt` | All live-state and persistent-live regressions |
| `full-python-gate.json` | Capability-closed 1013-node exact-identity Python gate |
| `full-lit-llvm17.txt` | Complete LLVM 17 lit gate |
| `live-cbc-oracles.json` | Exhaustive Boolean-pattern branch-outcome oracle |
| `live-cbc-mechanism-benchmark.json` | 11-sample BFS/CBC synthetic mechanism comparison |
| `pytest-inventory.json` | Canonical 1013-node identity inventory |
| `source-contract.txt` | SHA-256 identities for changed production, test, and contract files |
| `source-research.txt` | Primary paper/artifact identity and inspected algorithm map |
| `static-checks.txt` | Lint, compilation, index, whitespace, diagram, and claim checks |
| `environment.txt` | Host and toolchain context |
| `SHA256SUMS.txt` | Exact evidence-directory manifest |

The finite oracle enumerates 508 patterns across independent branch counts
2 through 8 and confirms that the accepted all-false/all-true representatives
preserve both outcomes of every branch.

For six independent branches, exhaustive BFS emits 64 terminal states and 126
checkpoints. CBC emits 2 terminal representatives and 12 checkpoints, while
feasibility checks fall from 126 to 22. Median wall time over 11 runs is
2.776778111 s for BFS and 0.487879370 s for CBC in this deliberately favorable
Python/interpreter model.

This evidence does not execute a public target campaign and makes no general
coverage, solver-throughput, bug-yield, or end-to-end speedup claim. The paper's
reported results remain external results and are not attributed to this build.
