# F383 evidence

This directory preserves executable evidence for the proof-carrying multi-cell alias graph.

- `fixed-heap.json`: disjoint fixed-allocation objects with one verified graph edge.
- `guard-correlated.json`: overlapping addresses made mutually exclusive by one canonical BV guard.
- `symbolic-index.json`, `symbolic-index-llvm17.json`: LLVM 18/17 artifacts with exact address-to-index witnesses.
- `phi-correlated.json`: explicit conservative boundary; it retains the older PHI correlation contract and does not claim the graph capability.
- `*-lowering.txt`: exact lowering command records produced by the source driver.
- `targeted-python.txt`: five direct admission and tamper regressions.
- `full-lit.txt`: complete LLVM lit suite.
- `full-python-gate.json`: capability-closed canonical Python gate.
- `static-checks.txt`: LLVM 17/18 builds, Ruff, py_compile and whitespace checks.
- `checks.txt`: mechanically checked semantic summary.
- `SHA256SUMS.txt`: digest manifest for every other file in this directory.

The recorded complete gates are 240 passed plus one unsupported lit test, and
883 passed plus 229 passed subtests in the capability-closed Python gate. The
canonical inventory has no missing or unexpected node IDs and no skipped,
xfailed, xpassed, or deselected outcomes.

The evidence proves admission-time consistency for declared edges over bounded
finite address domains. It does not prove a general heap graph, complete
points-to analysis, undeclared graph edges, benchmark coverage improvement, or
performance improvement.
