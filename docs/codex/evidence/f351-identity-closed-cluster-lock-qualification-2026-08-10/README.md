# F351 Identity-Closed Cluster-Lock Qualification Evidence

This directory preserves local mechanism evidence for descriptor-anchored
state-root and lock-leaf qualification, filesystem-capability rebinding, and
runtime rejection of legacy unclosed evidence.

## Reproduction

From the repository root:

```bash
python3 docs/codex/evidence/f351-identity-closed-cluster-lock-qualification-2026-08-10/run_identity_closed_lock_integration.py
```

The driver invokes the production local capability probe, production
`qualify_mpi_cluster_advisory_lock()`, real regular files, `openat`, `flock`,
`fsync`, and `replace`. A thread message bus substitutes for MPI transport and
synthetic processor names activate the multi-processor branch. The only
fault-timing hook replaces the public stable-lock leaf immediately before the
production namespace-closure function; replacement bytes are exact while the
inode differs.

`identity-closed-lock-integration.json` and its byte-identical `.log` contain
eight exact checks. The three test logs preserve the final directed,
six-module, and complete warnings-as-errors Python results. `SHA256SUMS.txt`
covers every evidence file except itself.

## Environment

- Date: 2026-08-10 UTC
- Python: 3.12.3
- Kernel: Linux 7.0.0-28-generic x86_64
- Repository filesystem: overlayfs
- Actual MPI transport in this driver: false
- Actual multi-host storage clients: false
- Synthetic processor names: true

## Interpretation

The normal synthetic topology records `M=3`, `H=2`, two holder rounds, four
contention observations, two release observations, and three namespace
identity checks. It upgrades only to capability schema v3 and
`cross-host-mpi-lock-v2`. Exact-content leaf replacement makes both
participants fail after completing the behavioral rounds; stale state or
publication filesystem bindings fail before round zero; same-processor names
remain clean-but-unverified; and a legacy v1 result increments renewal
failure rather than success.

This is mechanism evidence, not deployment qualification. It does not run
`mpirun`, another physical host, NFS, Lustre, CephFS, server failover, a
network partition, a target, `afl-showmap`, a solver, or a fuzzing campaign.
It makes no throughput, coverage, bug-discovery, or LAVA-M uplift claim.
