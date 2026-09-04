# F365 Outcome-Separated Query Spool Evidence

This directory records the explicit validation, persistence, and publication
boundaries introduced by F365.

## Recorded Evidence

- `run_outcome_separation_checks.py`: executable legacy counterfactual and
  production fault-injection driver;
- `adversarial-cases.json` and `.log`: invalid-input rejection, reader I/O,
  SQLite persistence, accepted publication, and retry states;
- `directed-tests.log`: 20 QueryStore tests plus two existing lock subtests;
- `affected-tests.log`: 124 QueryStore, QF_BV, schedule, and semantic-proposal
  tests plus the same two subtests;
- `inventory-rebuild.log`: byte-identical 787-node canonical inventory;
- `full-gate.json` and `.log`: complete 16-capability and exact-identity gate;
- `static-checks.log`: Ruff, `py_compile`, whitespace, diagram, hash, and
  delivery verification.

## Interpretation

The legacy control flow calls the store once, then misclassifies an accepted
publication `OSError` as `(0,1)`, moves the valid input to rejected, and writes
an error sidecar. The production path propagates reader, persistence, and
publication failures without rejected/error artifacts and preserves the input
in incoming. A retry accepts the file and converges on exactly one query and
one witness, including when the first attempt committed before publication.

Malformed JSON remains the only tested dead-letter outcome: `(0,1)`, zero
stored queries/witnesses, and a compatibility `ValueError` sidecar.

`SHA256SUMS.txt` covers every regular file in this directory except itself.
This is local mechanism evidence. It is not hosted CI, cross-host filesystem
qualification, durable dead-letter atomicity, orphan-CAS garbage collection,
distributed consensus, global exactly-once execution, a solver/coverage
campaign, LAVA-M evaluation, or a performance result.
