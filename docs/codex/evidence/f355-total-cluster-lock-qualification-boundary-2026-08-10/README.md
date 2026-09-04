# F355 Total Cluster-Lock Qualification Boundary Evidence

This directory records mechanism-level evidence for the public MPI cluster-lock
qualification exception boundary.

The integration driver uses the production public qualification API and a real
local filesystem capability probe. It checks a clean single-member protocol
result, a real communicator-adapter exception, a failing monotonic clock,
diagnostic bounding and sanitation, exception-rendering failure, capability and
generation normalization, and `BaseException` pass-through. A deterministic
monotonic clock is used only to make the clean-path JSON timing reproducible;
the filesystem capability probe and qualification filesystem operations are
real local operations.

The artifact does not run real MPI transport, a multi-host shared filesystem,
a solver, `afl-showmap`, a fuzzing campaign, or LAVA-M. It supports no
throughput, coverage, bug, or LAVA-M uplift claim.

## Reproduction

```bash
PYTHONDONTWRITEBYTECODE=1 python3 \
  docs/codex/evidence/f355-total-cluster-lock-qualification-boundary-2026-08-10/\
run_total_qualification_boundary_integration.py
```

The checked-in JSON and log must be byte-identical. `SHA256SUMS.txt` covers all
regular files in this directory except itself.
