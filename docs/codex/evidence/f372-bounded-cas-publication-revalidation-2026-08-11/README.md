# F372 Bounded CAS Publication Revalidation Evidence

This directory contains deterministic local Linux correctness evidence for
F372. It distinguishes transient pathname-identity drift from a stable wrong
digest after concurrent content-addressed publication, while retaining a hard
32-attempt bound.

## Contents

- `run_bounded_cas_revalidation_checks.py`: one-shot counterfactual, bounded
  production retry, stable-corruption rejection, exhaustion, and real
  eight-writer convergence;
- `adversarial-cases.json` and `.log`: byte-stable exact outcomes;
- `directed-tests.log`: the existing CAS publication-race identity;
- `affected-tests.log`: distributed-state and QueryStore regressions;
- `convergence-stress.log`: 20 complete replays of the F366 real eight-writer
  driver against its archived JSON;
- `inventory-rebuild.log`: canonical 787-node identity equivalence;
- `full-gate.json` and `.log`: exact-identity, 16-capability Python gate;
- `static-checks.log`: formatting, compilation, historical replay, diagram,
  HTML twin, hash, and delivery-verifier results;
- `SHA256SUMS.txt`: digest manifest for every regular evidence file except
  itself.

## Reproduction

```bash
PYTHONDONTWRITEBYTECODE=1 python3 \
  docs/codex/evidence/f372-bounded-cas-publication-revalidation-2026-08-11/\
run_bounded_cas_revalidation_checks.py
```

The counterfactual replaces only the F372 post-publication verification method
with the previous one-shot verifier. Production cases use real CAS directories,
descriptor-relative publication, stable snapshots, threads, and atomic path
replacement. Two consecutive executions must be byte identical and report
`all_checks_passed: true`.

## Claim Boundary

This is local Linux mechanism evidence. It is not a power-loss experiment,
cross-host filesystem qualification, MPI campaign, public solver benchmark,
fuzzing campaign, or LAVA-M run. It does not measure I/O latency, contention
frequency, throughput, RSS, coverage, or bug discovery and makes no performance
uplift claim. Continuous replacement exhausts the fixed budget and fails closed.

