# F385 executable evidence

This directory binds the implementation and verification evidence for the
bounded byte-lane writer graph. It is mechanism evidence, not a coverage,
throughput, bug-finding, or public-benchmark result.

- `overlap-llvm18.json` and `overlap-llvm17.json`: a two-byte write followed
  by a one-byte overwrite. The final graph maps lane 0 to the wide store and
  lane 1 to the later narrow store.
- `initial-llvm18.json`: one stored lane plus one initial-memory lane.
- `cross-function-llvm18.json`: one reachable function with an address-closed
  writer graph and another with a byte-lane PHI contract, proving that the
  program-level capability does not impose graph-local poison fields on the
  PHI-only function.
- `*-lowering.txt`: producer commands and lowering outcomes.
- `artifact-checks.txt`: independent executor/checker admission and execution.
- `targeted-python.txt`: five focused positive and tamper regressions.
- `live-lowering-lit.txt`: the large LLVM lowering file, including the mixed
  cross-function pipeline, passes as one test with more than 470 RUN commands.
- `full-python-gate.json`: exact 893-node capability gate, plus 229 subtests,
  with no skip, xfail, xpass, deselection, missing ID, or unexpected ID.
- `full-lit.txt`: controlled 32-worker result, 243 discovered, 242 passed, and
  one platform-declared unsupported test.
- `high-concurrency-lit-observation.txt`: the retained first 192-worker run.
  One pre-existing polyhedral exact-projection probe returned solver `unknown`
  under oversubscription while 242 other tests, including F385, ran; the test
  then passed 10/10 standalone and in the complete 32-worker run. The assertion
  was not weakened.
- `static-checks.txt`: LLVM 17/18 builds, Ruff, py_compile, whitespace, and
  delivery checks.
- `checks.txt`: compact machine-readable semantic and boundary summary.
- `SHA256SUMS.txt`: digest of every regular evidence file except itself.

The admitted graph is intentionally bounded to 2--8 byte non-atomic,
non-volatile loads and a reverse path of at most 64 blocks that remains
single-predecessor until all lanes are resolved. It does not claim general
MemorySSA, arbitrary branch/loop writer graphs, or independent reconstruction
of every LLVM poison predicate from source IR.
