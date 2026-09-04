# F403 Concrete Constraint Guided Scheduling Evidence

This directory seals the 2026-08-15 mechanism evidence for the conservative
function-local CGS adaptation. It covers static dependency admission, exact
bit-vector predicate evaluation, concrete branch/store observations, snapshot
v5, lease-conflict replay, two-level FIFO scheduling, and fail-open behavior.

| Artifact | Meaning |
| --- | --- |
| `targeted-python.txt` | CGS, snapshot, persistent, and scheduler tests |
| `related-python.txt` | All live/persistent and related ConDPOR regressions |
| `full-python-gate.json` | Capability-closed 1025-node exact-identity Python gate |
| `full-lit-llvm17.txt` | Complete LLVM 17 lit gate |
| `live-cgs-oracles.json` | Independent exhaustive 1--8 bit predicate oracle |
| `live-cgs-mechanism-benchmark.json` | 11-sample BFS/CGS target-latency mechanism comparison |
| `pytest-inventory.json` | Canonical 1025-node identity inventory |
| `source-contract.txt` | SHA-256 identities for changed production/test/contract files |
| `source-research.txt` | Primary paper/artifact identity and inspected algorithm map |
| `static-checks.txt` | Lint, compilation, index, whitespace, diagram, and claim checks |
| `environment.txt` | Host and toolchain context |
| `SHA256SUMS.txt` | Exact evidence-directory manifest |

The independent oracle evaluates all ten integer comparison predicates for
every value at bit widths 1 through 8 and both desired outcomes: 80 cases and
10,200 evaluations passed.

With a 500-instruction ordinary distractor, the minimum instruction budget to
reach the target outcome is 519 for BFS and 18 for CGS, a 96.5318% reduction in
this deliberately favorable synthetic target-latency experiment. Both complete
runs execute 519 instructions, fork twice, and terminate with values
`{10,20,30}`. Median complete-run wall time over 11 samples is 3.430162651 s for
BFS and 3.441957820 s for CGS, so this evidence does not show overall speedup.

This is not a public-target campaign, completeness proof, coverage-gain result,
bug-yield result, or solver-throughput result. It implements a conservative
continuation-level subset of the ICSE 2024 mechanism rather than claiming
artifact-equivalent LLVM/IDA support.
