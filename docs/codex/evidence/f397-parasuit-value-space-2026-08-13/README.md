# F397 Program-Bound ParaSuit Value-Space Evidence

This directory seals the 2026-08-13 mechanism evidence for F397. It proves
the local implementation, boundedness, deterministic two-cluster oracle,
state-binding rejection paths, complete Python identity gate, and LLVM
regression status. It does not contain a target campaign and makes no coverage,
solver-throughput, bug-yield, or end-to-end speedup claim.

| Artifact | Meaning |
| --- | --- |
| `targeted-python.txt` | Nine F397 unit/contract tests |
| `related-python.txt` | F397 plus self-config, MPI lifecycle, feedback and orchestration regressions |
| `full-python-gate.json` | Capability-closed 969-node exact-identity Python gate |
| `targeted-lit-llvm17.txt`, `targeted-lit-llvm18.txt` | Native provider contract on both maintained LLVM builds |
| `full-lit.txt` | Complete LLVM 18 lit gate |
| `mechanism-benchmark.json` | 1,000-run deterministic cluster/value/state cost benchmark |
| `randomized-property.txt` | 1,000 deterministic randomized boundedness checks |
| `provider.json` | Five coordinator-campaign value-policy settings |
| `pytest-inventory.txt` | Canonical node-ID inventory result |
| `static-checks.txt` | Ruff, format, compilation and whitespace gates |
| `source-contract.txt` | SHA-256 identities for the implementation and tests |
| `upstream-revision.txt` | Exact ParaSuit research/artifact provenance and claim boundary |
| `environment.txt` | Host and toolchain context |
| `SHA256SUMS.txt` | Exact evidence-directory manifest |

The fixed eight-observation oracle has labels `00001111` and silhouette
`0.914156472`. The final isolated timing run reports medians of 75.033 us for
analysis, 85.779 us for adaptive value selection, 6.050 us for the F396
Thompson selector, and 668.729 us for bound state-pair reload. These values
measure small synthetic Python mechanisms only.

