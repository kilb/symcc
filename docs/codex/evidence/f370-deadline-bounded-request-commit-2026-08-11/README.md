# F370 Deadline-Bounded Request Commit Evidence

This directory contains deterministic local Linux correctness evidence for
F370. It proves that persistent-solver descriptor transfer, text-frame commit,
and response admission share one absolute monotonic deadline. Kernel
backpressure can no longer leave a worker indefinitely blocked before the
solver observes its own timeout.

## Contents

- `run_deadline_commit_checks.py`: counterfactual/production driver using real
  pipes, Unix `SOCK_SEQPACKET`, `SCM_RIGHTS`, saturated kernel send buffers,
  sealed QueryStore leases, stalled helpers, generation retirement, and cold
  recovery;
- `adversarial-cases.json` and `.log`: byte-stable legacy blocking and
  production deadline outcomes;
- `directed-tests.log`: all 20 existing QueryStore identities, including 12
  subtests and both stalled transport paths;
- `affected-tests.log`: QueryStore, QF_BV, semantic proposal, and distributed
  state regressions;
- `lit-tests.log`: filtered native query/string helper tests;
- `inventory-rebuild.log`: canonical 787-node identity byte-equivalence;
- `full-gate.json` and `.log`: exact-identity, 16-capability full Python gate;
- `static-checks.log`: formatting, compilation, historical replay, diagram,
  hash, HTML twin, and delivery-verifier results;
- `SHA256SUMS.txt`: digest manifest for every regular evidence file except
  itself.

## Reproduction

From the repository root:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 \
  docs/codex/evidence/f370-deadline-bounded-request-commit-2026-08-11/\
run_deadline_commit_checks.py
```

The legacy branches reproduce the exact blocking syscall patterns on real
kernel objects. The production branches use `PersistentSubprocessSolver`
without replacing its transport methods; only the grace constant is shortened
so the fault experiment completes quickly. Two consecutive executions must be
byte-identical and report `all_checks_passed: true`.

## Claim Boundary

This is local Linux mechanism evidence. It does not execute a GitHub-hosted
job, the full LLVM lit suite, vendored QSYM/PIN tracing, actual MPI transport,
cross-host filesystems, a public solver benchmark, a fuzzing campaign, or
LAVA-M. It does not measure large-witness throughput, memory, default-grace
tail latency, or prefix-cache restart cost and makes no coverage,
bug-discovery, throughput, or performance-uplift claim.
