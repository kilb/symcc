# F401 Persistent Empc-Style Live-State Path-Cover Evidence

This directory seals the 2026-08-14 mechanism evidence for F401. It proves
function-local SCC condensation, bounded multiple minimum path covers,
checkpoint-token compatibility, frontier-generation coverage projection,
snapshot-v3 restart equivalence, deterministic production selection, and
regression status.

| Artifact | Meaning |
| --- | --- |
| `targeted-python.txt` | F401 planner, policy, frontier, and executor tests |
| `related-python.txt` | Live-state, persistent, path-cover, and ConDPOR regressions |
| `full-python-gate.json` | Capability-closed 1003-node exact-identity Python gate |
| `full-lit-llvm17.txt` | Complete LLVM 17 lit gate |
| `live-path-cover-microbenchmark.json` | 31-sample construction/scoring overhead |
| `matching-scc-oracles.json` | Independent small-graph matching and SCC checks |
| `pytest-inventory.json` | Canonical 1003-node identity inventory |
| `source-contract.txt` | SHA-256 identities for changed production and test files |
| `static-checks.txt` | Lint, compilation, whitespace, diagram, and claim checks |
| `environment.txt` | Host and toolchain context |
| `SHA256SUMS.txt` | Exact evidence-directory manifest |

The largest microbenchmark row has 769 CFG nodes and 512 candidates. Median
times are 14.992490 ms for one MPC-enabled graph build, 0.454554 ms for one
frontier-generation coverage context, 1.221638 ms for the full guidance batch,
and 0.075273 ms for final policy selection.

The benchmark is in-process and executes no target, solver, MPI campaign, AFL
map, or concrete replay. It makes no coverage, solver-throughput, bug-yield, or
end-to-end speedup claim.
