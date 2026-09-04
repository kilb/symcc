# F386 executable evidence

This directory binds the implementation and verification evidence for the
bounded byte-lane PHI writer graph. It is mechanism evidence, not a coverage,
throughput, bug-finding, or public-benchmark result.

- `phi-llvm18.json` and `phi-llvm17.json`: one two-endpoint PHI contract. Each
  endpoint resolves two lanes to function-local last writers and binds four
  graph-referenced stores to poison-source sidecars.
- `initial-llvm18.json`: each endpoint combines one stored lane with one
  initial-memory lane, showing that graph replay closes at the function entry.
- `cross-function-llvm18.json`: three reachable functions preserve one F385
  non-PHI graph and one F386 PHI graph without confusing their local store IDs.
- `*-lowering.txt`: producer command and lowering outcome for each artifact.
- `artifact-checks.txt`: four independent checker/executor admissions, including
  the checker's capability, marker, writer substitution, and poison tampering.
- `targeted-python.txt`: five focused runtime positive/tamper regressions.
- `related-live-python.txt`: all 69 live-state modules pass together with 51
  subtests, checking compatibility with the surrounding admission pipeline.
- `live-lowering-lit.txt`: the large LLVM lowering file and its more than 470
  RUN pipelines pass as one test.
- `full-python-gate.json`: exact 898-node gate plus 229 subtests with no skip,
  xfail, xpass, deselection, missing ID, or unexpected ID.
- `full-lit.txt`: controlled 32-worker result, 244 discovered, 243 passed, and
  one platform-declared unsupported test.
- `static-checks.txt`: LLVM 17/18, Ruff, py_compile, whitespace, and delivery
  status.
- `checks.txt`: compact machine-readable semantic and boundary summary.
- `SHA256SUMS.txt`: digest of every regular evidence file except itself.

For each PHI endpoint, admission starts before its terminal jump, walks backward
through at most 64 blocks, and follows only a unique predecessor. The first
store covering an unresolved load byte must exactly match the declared store
ID, address-derived byte offset, and width. A path without a predecessor may
resolve only lanes declared as initial memory.

This contract does not prove general MemorySSA, arbitrary joins inside an
endpoint path, loop-carried writers, or LLVM poison predicates independently
of the producer. Those remain explicit follow-on boundaries.
