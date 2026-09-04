# F356 Collective-Safe Qualification Observation Evidence

This directory records mechanism-level evidence for rank-local filesystem and
processor observations before the bounded cluster-lock qualification protocol.

The integration driver uses the production observation and public
qualification APIs, a real local filesystem capability probe, and a synthetic
two-rank message bus. It verifies ordinary exception containment, continued
processor observation after a filesystem-probe failure, input normalization,
bounded diagnostics, `BaseException` pass-through, and convergence of both
synthetic ranks on one explicit processor-observation failure.

The artifact does not run real MPI transport, a multi-host shared filesystem,
ULFM, a solver, `afl-showmap`, a fuzzing campaign, or LAVA-M. It supports no
throughput, coverage, bug, recovery, or LAVA-M uplift claim.

## Reproduction

```bash
PYTHONDONTWRITEBYTECODE=1 python3 \
  docs/codex/evidence/f356-collective-safe-qualification-observation-2026-08-10/\
run_collective_safe_observation_integration.py
```

The checked-in JSON and log must be byte-identical. `SHA256SUMS.txt` covers all
regular files in this directory except itself.
