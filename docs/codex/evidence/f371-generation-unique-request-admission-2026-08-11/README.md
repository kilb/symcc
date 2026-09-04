# F371 Generation-Unique Request Admission Evidence

This directory contains deterministic local Linux correctness evidence for
F371. It proves that a logical query ID is admitted at most once per persistent
helper generation, so a delayed duplicate response from a completed call
cannot satisfy a same-ID retry.

## Contents

- `run_same_id_retry_checks.py`: real-process counterfactual and production
  driver, plus preflight, distinct-ID reuse, and bounded-history checks;
- `adversarial-cases.json` and `.log`: byte-stable observed outcomes;
- `directed-tests.log`: all 20 existing QueryStore identities and 12 subtests;
- `affected-tests.log`: QueryStore, QF_BV, semantic proposal, and distributed
  state regressions;
- `lit-tests.log`: filtered native query/string helper compatibility tests;
- `inventory-rebuild.log`: canonical 787-node identity equivalence;
- `full-gate.json` and `.log`: exact-identity, 16-capability Python gate;
- `static-checks.log`: formatting, compilation, historical replay, diagram,
  HTML twin, hash, and delivery-verifier results;
- `SHA256SUMS.txt`: digest manifest for every regular evidence file except
  itself.

## Reproduction

From the repository root:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 \
  docs/codex/evidence/f371-generation-unique-request-admission-2026-08-11/\
run_same_id_retry_checks.py
```

The counterfactual replaces only the F371 admission method. It retains the
real subprocess, pipes, request encoding, F368-F370 transport, framing, JSON,
and response-ID checks. The production branch does not replace transport or
generation lifecycle methods. Two consecutive executions must be byte
identical and report `all_checks_passed: true`.

## Claim Boundary

This is local Linux mechanism evidence. It does not execute a GitHub-hosted
job, the full LLVM lit suite, vendored QSYM/PIN tracing, actual MPI transport,
cross-host filesystems, a public solver benchmark, a fuzzing campaign, or
LAVA-M. It does not measure coverage, bug discovery, solver throughput, RSS,
retry frequency, prefix-cache loss, or cold-start cost and makes no
performance-uplift claim. F371 is per-helper-generation at-most-once
admission, not global exactly-once execution.

