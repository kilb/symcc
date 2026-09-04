# F354 Rank-Contained Renewal Completion Evidence

This directory records mechanism-level evidence for the runtime renewal
completion containment boundary.

The integration driver uses the production `ClusterLockRenewalController`,
`complete_cluster_lock_renewal`, capability upgrade, proof transcript, and a
real local filesystem probe. It distinguishes a committed proof rejection from
pre-commit arithmetic/internal exceptions, verifies exact snapshot retention,
bounds diagnostic text, rejects an invalid controller, and confirms that
`BaseException` process-control signals are not swallowed.

The artifact does not run real MPI transport, a multi-host shared filesystem,
a solver, `afl-showmap`, a fuzzing campaign, or LAVA-M. It supports no
throughput, coverage, bug, or LAVA-M uplift claim.

## Reproduction

```bash
PYTHONDONTWRITEBYTECODE=1 python3 \
  docs/codex/evidence/f354-rank-contained-renewal-completion-2026-08-10/\
run_rank_contained_renewal_integration.py
```

The checked-in JSON and log must be byte-identical. `SHA256SUMS.txt` covers all
regular files in this directory except itself.
