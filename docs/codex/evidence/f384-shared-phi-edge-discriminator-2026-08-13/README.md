# F384 evidence

This directory preserves executable evidence for the shared PHI edge discriminator.

- `phi-ordered.json`: two correlated pointer PHIs with matching incoming list order.
- `phi-reordered.json`, `phi-reordered-llvm17.json`: LLVM 18/17 artifacts whose second PHI intentionally reverses its incoming list.
- `function-pointer-phi.json`: indirect-call PHI compatibility artifact.
- `*-lowering.txt`: exact LLVM version and lowering command records.
- `artifact-checks.txt`: independent checker results for all four artifacts.
- `targeted-python.txt`: capability, edge-set, assignment, definition and load-guard tamper regressions.
- `full-lit.txt`: complete LLVM lit suite.
- `full-python-gate.json`: capability-closed canonical Python gate.
- `static-checks.txt`: LLVM 17/18 builds, Ruff, py_compile and whitespace checks.
- `checks.txt`: mechanically checked semantic summary.
- `SHA256SUMS.txt`: digest manifest for every other file in this directory.

The complete gates are 241 passed plus one unsupported lit test, and 888
passed plus 229 passed subtests in the Python gate. The canonical inventory has
no missing or unexpected node IDs and no skipped, xfailed, xpassed, or
deselected outcomes.

The evidence proves a bounded same-function, same-PHI-block predecessor
relation. It does not prove a general path relation, heap graph, points-to
analysis, benchmark coverage improvement, or performance improvement.
