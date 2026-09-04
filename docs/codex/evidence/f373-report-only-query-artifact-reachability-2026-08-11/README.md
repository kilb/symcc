# F373 Report-Only Query Artifact Reachability Evidence

This directory contains deterministic local Linux mechanism evidence for the
QueryStore SQLite-to-CAS reachability audit. It proves that a database-only
view misses an unindexed physical orphan and a noncanonical namespace entry,
while the production report observes both without deleting any object.

## Contents

- `run_artifact_reachability_checks.py`: indexed/unindexed orphan
  counterexample, bounded partial scan, publication fence, and real CLI exits;
- `adversarial-cases.json` and `.log`: byte-stable structured outcomes;
- `directed-tests.log`: the extended existing QueryStore identity;
- `query-store-tests.log` and `affected-tests.log`: module regressions;
- `historical-replay.log`: F363--F372 drivers replayed byte-for-byte;
- `inventory-rebuild.log`: exact canonical 787-node identity inventory;
- `full-gate.json` and `.log`: 16-capability, exact-identity Python gate;
- `checks.txt`: machine-readable implementation and claim boundaries;
- `static-checks.log`: source, diagram, hash, and delivery checks;
- `SHA256SUMS.txt`: every regular evidence file except itself.

## Reproduction

```bash
PYTHONDONTWRITEBYTECODE=1 python3 \
  docs/codex/evidence/f373-report-only-query-artifact-reachability-2026-08-11/\
run_artifact_reachability_checks.py
```

Two consecutive runs must be byte identical, report
`all_checks_passed: true`, and have SHA-256
`43b0efaf58398fdea5e2a12544fb589c8349ac46a148e1caf41e837705955f09`.

## Claim Boundary

This is local cooperative-filesystem evidence. The inventory validates
descriptor-anchored namespace shape and regular-file metadata, not object
content digests. The implementation is report-only and contains no sweep,
unlink, or database-row deletion path. It is not cross-host filesystem
qualification, power-loss recovery, a solver/fuzzing campaign, LAVA-M evidence,
or a performance result.
