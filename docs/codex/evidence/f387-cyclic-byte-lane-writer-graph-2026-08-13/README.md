# F387 executable evidence

This directory binds the implementation and verification evidence for the
bounded cyclic byte-lane writer graph. It is mechanism evidence, not a
coverage, throughput, bug-finding, or public-benchmark result.

- `cycle-llvm18.json` and `cycle-llvm17.json`: the same base cyclic PHI is
  lowered by both LLVM versions. Each artifact contains one graph, two
  endpoints, one carry lane, and two function-local store IDs.
- `conditional-llvm18.json`: a conservative boundary artifact. Its
  conditional transfer keeps the existing conditional capability and has no
  F387 capability or `writer_graph` marker.
- `*-lowering.txt`: producer command and lowering outcome for each artifact.
- `artifact-checks.txt`: the two graph artifacts pass checker-generated
  capability, marker, address, and poison tampering; the conditional boundary
  passes its original checker without an F387 claim.
- `targeted-python.txt`: five positive/counterexample model tests.
- `writer-graph-python.txt`: all fifteen F385--F387 graph tests together.
- `related-live-python.txt`: all 74 live-state modules plus 51 subtests.
- `live-lowering-lit.txt`: the large LLVM lowering file and its more than 470
  RUN pipelines pass as one test.
- `full-python-gate.json`: exact 903-node gate plus 229 subtests with no skip,
  xfail, xpass, deselection, missing ID, or unexpected ID.
- `full-lit.txt`: controlled 32-worker result, 245 discovered, 244 passed, and
  one platform-declared unsupported test.
- `static-checks.txt`: LLVM 17/18, Ruff, py_compile, whitespace, and delivery
  status.
- `checks.txt`: compact machine-readable semantic and boundary summary.
- `SHA256SUMS.txt`: digest of every regular evidence file except itself.

For each base cyclic endpoint, admission starts before the terminal jump and
walks backward through at most 64 blocks. The first store covering an
unresolved load byte must match its declared ID, address-derived byte offset,
and width. Reaching the same merge load resolves remaining lanes only as
carry; reaching a predecessor-free root resolves them only as initial memory.

This contract does not prove conditional/multi-arm/recursive transfer leaves,
general multi-latch MemorySSA, symbolic pointers, concurrent memory, or LLVM
poison predicates independently of the producer. Those remain explicit
follow-on boundaries.
