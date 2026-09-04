# F366 Verified Query Artifact CAS Evidence

This directory records the counterexample, production fault injection, and
regression evidence for QueryStore's descriptor-anchored SMT2 artifact CAS and
pre-lease admission boundary.

## Recorded Evidence

- `run_verified_query_artifact_checks.py`: executable legacy counterfactual,
  production corruption/path/type checks, and eight-writer convergence driver;
- `adversarial-cases.json` and `.log`: exact old/new states;
- `directed-tests.log`: QueryStore and distributed-state tests;
- `affected-tests.log`: directed scope plus MPI lifecycle and QF_BV backend;
- `inventory-rebuild.log`: byte-identical canonical 787-node inventory;
- `full-gate.json` and `.log`: exact-identity, 16-capability full gate;
- `static-checks.log`: static, historical replay, diagram, hash, and delivery
  checks.

## Interpretation

The legacy existence-only rule silently reuses bytes whose SHA-256 differs
from the digest encoded in their path. Production repairs pre-existing corrupt
regular files, symlinks, and FIFOs when expected content is available; it
rejects post-commit corruption and non-canonical database paths. A failed
artifact check occurs before lease mutation, preserving `pending` and zero
attempts. Eight concurrent writers converge on one digest and one database row
without residual temporary files.

`SHA256SUMS.txt` covers every regular file in this directory except itself.
This is local correctness and recovery evidence. It is not an fd handoff proof,
orphan-CAS garbage collection, cross-host filesystem qualification, hosted CI,
a real solver/coverage campaign, a public benchmark, LAVA-M, or a performance
uplift measurement.
