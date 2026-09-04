# F363 Bounded Query IR Admission Evidence

This directory records the strict file-admission boundary introduced by F363
for asynchronous `symcc-query-ir-v1` envelopes. The production reader now
consumes at most `limit + 1` bytes from one open descriptor, performs size
admission before UTF-8/JSON construction, rejects duplicate object members and
non-finite JSON numbers, and only then enters semantic QueryStore ingestion.

## Recorded Evidence

- `run_adversarial_query_checks.py`: executable driver calling production
  `QueryStore.ingest_file()` and `ingest_spool()` APIs;
- `adversarial-cases.json` and `.log`: exact-limit acceptance, forged-stat
  oversize rejection, duplicate-member/non-finite isolation, and final store
  cardinalities;
- `directed-tests.log`: all 20 QueryStore tests;
- `affected-tests.log`: 124 QueryStore, QF_BV, schedule, and semantic-proposal
  tests that share Query IR contracts;
- `inventory-rebuild.log`: the canonical 787-node inventory rebuilt byte for
  byte;
- `full-gate.json` and `.log`: complete capability and identity gate;
- `static-checks.log`: Ruff, `py_compile`, whitespace, hashes, diagram, and
  delivery verification.

## Interpretation

The exact-boundary case accepts an 845-byte valid envelope under an 845-byte
limit. The same envelope under a 64-byte limit is rejected despite a forged
one-byte `Path.stat()` result; the production loader makes zero path-stat calls.
In one spool batch, only the valid envelope is accepted, while duplicate
`schema` and `NaN` priority envelopes are quarantined. SQLite contains exactly
one query and one witness, so rejected inputs create no partial QueryStore
state.

`SHA256SUMS.txt` covers every regular file in this directory except itself.
These are local correctness and reproducibility results. They are not a proof
against hostile filesystems or in-place writers, a GitHub-hosted run, a solver
or coverage campaign, a public benchmark, LAVA-M, or a performance result.
