# F358 Renewal Control Transport Containment Evidence

This directory records mechanism-level evidence for the runtime cluster-lock
renewal request transport boundary. The deterministic driver calls the
production begin, poll, and delivery-observation APIs through a synthetic
point-to-point message bus. It injects ordinary failures before generation
creation, after partial send, during peer polling and admission, and while
observing nonblocking send completion.

The artifact checks that a post-begin failure retains the nonzero generation
and every successfully created send handle, that each completion handle is
observed at most once, and that completed, incomplete, and uncertain delivery
states remain distinct. It also checks bounded diagnostics and
`BaseException` pass-through.

This evidence does not use actual MPI transport, a collective, a multi-host
filesystem, ULFM, a solver, a fuzzing campaign, or LAVA-M. Partial delivery is
reported and failed closed; it is not rolled back, retried, or claimed to be
atomic. No throughput, coverage, bug, recovery, or benchmark uplift is claimed.

## Reproduction

```bash
PYTHONDONTWRITEBYTECODE=1 python3 \
  docs/codex/evidence/f358-renewal-control-transport-containment-2026-08-10/\
run_renewal_control_transport_integration.py
```

The checked-in JSON and log must be byte-identical. `SHA256SUMS.txt` covers all
regular files in this directory except itself.
