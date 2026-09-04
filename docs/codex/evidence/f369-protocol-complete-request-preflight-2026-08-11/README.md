# F369 Protocol-Complete Request Preflight Evidence

This directory contains deterministic local Linux correctness evidence for
F369. It proves that every field of the final persistent-solver request is
validated before helper selection, descriptor transfer, or pipe output. A
newline-bearing compatibility path can no longer create a delayed response
whose chosen request ID is accepted by a later request.

## Contents

- `run_request_preflight_checks.py`: production/counterfactual driver using a
  real persistent subprocess, text pipes, Unix `SOCK_SEQPACKET`, `SCM_RIGHTS`,
  sealed QueryStore leases, and deliberately separated response timing;
- `adversarial-cases.json` and `.log`: exact legacy stale-model observation and
  production rejection/recovery outcomes;
- `directed-tests.log`: all 20 existing QueryStore identities, expanded to
  cover newline, TAB, CR, NUL, non-ASCII descriptor IDs, and oversized IDs;
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
  docs/codex/evidence/f369-protocol-complete-request-preflight-2026-08-11/run_request_preflight_checks.py \
  --output /tmp/f369-request-preflight.json
```

The counterfactual disables only the new pure preflight method. The production
transport, helper, pipes, request framing, response-ID check, and generation
lifecycle remain unchanged. Two consecutive executions must be byte-identical
and report `all_checks_passed: true`.

## Claim Boundary

This is local Linux mechanism evidence. It does not execute a GitHub-hosted
job, the full LLVM lit suite, vendored QSYM/PIN tracing, actual MPI transport,
cross-host filesystems, a public solver benchmark, a fuzzing campaign, or
LAVA-M. It measures neither request-preflight overhead nor prefix-cache hit
rate and makes no throughput, coverage, bug-discovery, or performance-uplift
claim. The compatibility pathname mode remains weaker than sealed descriptor
transport with respect to post-validation pathname mutation.
