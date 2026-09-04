# F388 executable evidence

This directory binds the implementation and verification evidence for the
bounded conditional cyclic byte-lane writer graph. It is mechanism evidence,
not a coverage, throughput, solver-speed, bug-finding, or public-benchmark
result.

- `conditional-llvm18.json` and `conditional-llvm17.json`: the same direct
  two-leaf conditional loop lowered by both LLVM versions. Each contains one
  graph, two cyclic endpoints, one transfer, one store lane, one carry lane,
  two function-local graph store IDs, and one poison-bound transfer store.
- `forwarded-llvm18.json`: the branch-to-arm paths contain forwarded corridor
  blocks, while preserving the same graph cardinalities and leaf semantics.
- `*-lowering.txt`: producer command and lowering outcome for each artifact.
- `artifact-checks.txt`: all three artifacts pass capability, marker, address,
  and poison-source active tampering in the production checker.
- `targeted-python.txt`: five positive/counterexample F388 model tests.
- `writer-graph-python.txt`: all twenty F385--F388 writer-graph tests.
- `related-live-python.txt`: all 79 live-state tests plus 51 subtests.
- `full-python-gate.json`: exact 908-node gate plus 229 subtests, with no skip,
  xfail, xpass, deselection, missing identity, or unexpected identity.
- `full-lit.txt`: controlled 32-worker result: 246 discovered, 245 passed, and
  one platform-declared unsupported test.
- `static-checks.txt`: LLVM 17/18, Ruff, py_compile, whitespace, and delivery
  status.
- `checks.txt`: compact machine-readable semantic and boundary summary.
- `SHA256SUMS.txt`: digest of every regular evidence file except itself.

Admission decomposes a conditional backedge into independent seed-to-root,
merge-edge-to-join, store-leaf-to-branch, and carry-leaf-to-branch proofs.
Each reverse path is bounded to 64 unique blocks and has one predecessor per
internal step; existing forwarded-corridor topology is additionally bounded
to 16 blocks. The first store covering each unresolved load byte must match
its function-local ID, address-derived byte offset, and width. Remaining lanes
are carry only at their explicit join or branch boundary.

This contract covers direct and forwarded two-leaf conditional transfers. It
does not cover multi-arm transfers, recursive condition trees, general
multi-latch MemorySSA, symbolic pointers, concurrent memory, or independent
derivation of LLVM poison predicates.
