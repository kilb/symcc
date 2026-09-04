# F400 Persistent Multi-Objective Live-State Feedback Evidence

This directory seals the 2026-08-14 mechanism evidence for F400. It proves
production selection over persistent continuation states, versioned and bounded
outcome feedback, selection/observation transition separation, generation-CAS
rebasing, lease-fenced completion, atomic failed abandonment, restart behavior,
and local regression status. It contains no public-target campaign and makes no
coverage, bug-yield, solver-throughput, or end-to-end speedup claim.

| Artifact | Meaning |
| --- | --- |
| `targeted-python.txt` | F400 policy/frontier/executor tests |
| `related-python.txt` | All live-state, persistent, path-cover, and ConDPOR regressions |
| `full-python-gate.json` | Capability-closed 995-node exact-identity Python gate |
| `full-lit-llvm17.txt` | Complete LLVM 17 lit gate |
| `policy-microbenchmark.json` | 31-sample bounded selection-cost measurement |
| `pytest-inventory.json` | Canonical 995-node test identity inventory |
| `source-contract.txt` | SHA-256 identities for implementation and tests |
| `static-checks.txt` | Lint, compilation, whitespace, diagram, and claim checks |
| `environment.txt` | Host and toolchain context |
| `SHA256SUMS.txt` | Exact evidence-directory manifest |

The final isolated mechanism run reports a multi-objective selection median of
0.128764 ms for 64 candidates, 2.164946 ms for 1,024, and 8.027188 ms for
4,096. The benchmark excludes frontier I/O, solver execution, and target
execution. Its BFS rows establish the harness floor; they are not an
algorithmically equivalent speedup baseline.

`coverage_gain` in this evidence means newly observed live-interpreter CFG
locations after rebasing against the latest durable snapshot. It is not AFL
edge coverage and not native concrete-replay coverage.
