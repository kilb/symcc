# F364 Stable Query Spool Admission Evidence

This directory records the stable regular-file Query IR reader and
crash-released cooperative spool admission introduced by F364.

## Recorded Evidence

- `run_query_spool_adversarial_checks.py`: executable legacy counterfactual and
  production driver;
- `adversarial-cases.json` and `.log`: deterministic two-consumer legacy
  failure, live lock contention, real `SIGKILL` takeover, symlink/FIFO input
  rejection, path identity closure, and poisoned lock-object rejection;
- `directed-tests.log`: 20 QueryStore tests plus two lock-object subtests;
- `affected-tests.log`: 124 QueryStore, QF_BV, schedule, and semantic-proposal
  tests plus the same two subtests;
- `inventory-rebuild.log`: byte-identical 787-node canonical inventory;
- `full-gate.json` and `.log`: complete 16-capability and exact-identity gate;
- `static-checks.log`: Ruff, `py_compile`, whitespace, diagram, hash, and
  delivery verification.

## Interpretation

The embedded legacy algorithm deterministically calls ingestion twice, returns
normally once, then leaks one `FileNotFoundError` and a misleading error file.
The production path returns `(0,0)` with zero QueryStore calls while another
process holds the lock. After that holder is killed with `SIGKILL`, a new
service immediately admits the preserved input and stores exactly one query and
one witness.

Final symlinks and FIFOs are rejected without following or blocking; a changed
post-read path identity creates zero queries. Symlink/FIFO lock objects fail
before input scanning and leave the external target unchanged.

`SHA256SUMS.txt` covers every regular file in this directory except itself.
This is local crash-only evidence for cooperating consumers on a filesystem
with coherent `flock` semantics. It is not a cross-host lock qualification,
hostile-namespace proof, consensus or global exactly-once protocol, hosted CI
run, solver/coverage campaign, public benchmark, LAVA-M run, or performance
result.
