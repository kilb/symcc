# F353 Failure-Atomic Renewal Result Admission Evidence

This directory records the mechanism-level evidence for failure-atomic runtime
cluster-lock renewal result admission.

The integration driver uses the production `ClusterLockRenewalController`, the
production shared-filesystem capability upgrade gate, and a real local
filesystem probe. It exercises one valid completion, five malformed result
shapes, two injected internal failures, and one exact-type generation failure.

The artifact is local controller evidence. It does not use real MPI transport,
a multi-host shared filesystem, a solver, `afl-showmap`, a fuzzing campaign, or
LAVA-M. It therefore supports no throughput, coverage, bug, or LAVA-M uplift
claim.

## Reproduction

```bash
PYTHONDONTWRITEBYTECODE=1 python3 \
  docs/codex/evidence/f353-failure-atomic-renewal-admission-2026-08-10/\
run_failure_atomic_renewal_integration.py
```

The checked-in JSON and log must be byte-identical. `SHA256SUMS.txt` covers all
regular files in this directory except itself.
