# F368 Persistent Solver Generation Reset Evidence

This directory contains local Linux mechanism evidence for F368. It proves
that a partial persistent-solver request cannot leave descriptor messages for a
later request to consume: any uncertain send/write/read/parse transaction
retires the process, stdio pipes, and descriptor socketpair as one generation.

## Contents

- `run_generation_reset_checks.py`: deterministic production/counterfactual
  driver using real `SOCK_SEQPACKET`, `SCM_RIGHTS`, sealed QueryStore leases,
  process replacement, response framing, and timeout injection;
- `adversarial-cases.json` and `.log`: exact legacy mismatch and production
  recovery observations;
- `directed-tests.log`: the 20 existing QueryStore identities with expanded
  response-ID, oversize, and post-descriptor-send recovery assertions;
- `affected-tests.log`: QueryStore, QF_BV, semantic proposal, and distributed
  state regression scope;
- `lit-tests.log`: filtered real C++ query/string helper scope;
- `inventory-rebuild.log`: canonical 787-nodeid byte-equivalence check;
- `full-gate.json` and `.log`: exact-identity, 16-capability full Python gate;
- `static-checks.log`: formatting, compilation, historical replay, diagram,
  hash, HTML twin, and delivery-verifier checks;
- `SHA256SUMS.txt`: digest manifest for every regular evidence file except
  itself.

## Reproduction

From the repository root:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 \
  docs/codex/evidence/f368-persistent-solver-generation-reset-2026-08-11/run_generation_reset_checks.py \
  --output /tmp/f368-generation-reset.json
```

The JSON is deterministic; two consecutive runs must be byte-identical and
report `all_checks_passed: true`.

## Claim Boundary

This is local Linux correctness evidence. It does not execute a GitHub-hosted
job, the full LLVM lit suite, vendored QSYM/PIN tracing, actual MPI transport,
cross-host filesystems, a public solver benchmark, a fuzzing campaign, or
LAVA-M. It makes no throughput, coverage, bug-discovery, or performance-uplift
claim. A failed transaction intentionally loses the in-memory prefix cache of
that helper generation; its cost under high failure rates remains unmeasured.
