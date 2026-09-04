# F349 Descriptor-Anchored CAS Namespace Evidence

This directory records local mechanism evidence for the descriptor-anchored
content-addressed namespace used by input and live-continuation objects.

## Reproduction

From the repository root:

```bash
python3 docs/codex/evidence/f349-descriptor-anchored-cas-namespace-2026-08-10/run_descriptor_anchored_namespace_integration.py
```

The driver uses production `ContentAddressedInputStore` and `LiveStateStore`
code. `unittest.mock` injects deterministic root/shard replacement immediately
after publication and immediately after the first read. It does not replace the
filesystem implementation or reproduce the algorithm in the evidence driver.

`descriptor-anchored-namespace-integration.json` and the byte-identical `.log`
contain 11 exact checks. The test logs preserve the commands and summaries for
the directed, six-module, and complete warnings-as-errors Python regressions.
`SHA256SUMS.txt` covers every evidence file except itself.

## Interpretation

The result proves that tested production operations remain attached to opened
root/shard directory inodes, reject public namespace replacement before cache
admission, do not write through root/shard symlink aliases, preserve exact
same-digest leaf-writer convergence, and leave no temporary names.

This is local mechanism evidence. It does not establish behavior on NFS,
Lustre, or another host; it does not run MPI, a target, `afl-showmap`, a solver,
or a fuzzing campaign; and it makes no performance, coverage, bug-discovery, or
LAVA-M uplift claim. The configured CAS root is opened by absolute pathname, so
ancestors above that root remain a trusted deployment boundary. The
implementation uses portable Python `dir_fd`/`openat`-family operations rather
than claiming full Linux `openat2(RESOLVE_BENEATH)` containment.
