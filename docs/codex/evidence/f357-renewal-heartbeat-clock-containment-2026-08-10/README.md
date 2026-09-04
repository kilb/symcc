# F357 Renewal Heartbeat and Clock Containment Evidence

This directory records mechanism-level evidence for the work-lease heartbeat
and completion-clock boundaries surrounding runtime cluster-lock renewal.

The integration driver calls the production heartbeat observer, qualification
input observer, public qualification API, and renewal completion API. It uses a
real local filesystem capability probe and a synthetic two-rank message bus to
show that one rank's heartbeat exception becomes membership-visible negative
evidence. It also checks malformed heartbeat returns, bounded diagnostics,
single-call semantics, completion-clock failure atomicity, and `BaseException`
pass-through.

The artifact does not run real MPI transport, a multi-host shared filesystem,
ULFM, a solver, `afl-showmap`, a fuzzing campaign, or LAVA-M. A heartbeat may
have completed external filesystem side effects before reporting failure; the
implementation deliberately does not retry it. This evidence supports no
throughput, coverage, bug, recovery, or LAVA-M uplift claim.

## Reproduction

```bash
PYTHONDONTWRITEBYTECODE=1 python3 \
  docs/codex/evidence/f357-renewal-heartbeat-clock-containment-2026-08-10/\
run_renewal_heartbeat_clock_integration.py
```

The checked-in JSON and log must be byte-identical. `SHA256SUMS.txt` covers all
regular files in this directory except itself.
