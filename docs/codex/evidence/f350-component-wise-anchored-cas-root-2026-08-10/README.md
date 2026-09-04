# F350 Component-Wise Anchored CAS Root Evidence

This directory preserves local mechanism evidence for component-wise,
no-follow creation and reopening of the content-addressed input/live-state
root.

## Reproduction

From the repository root:

```bash
python3 docs/codex/evidence/f350-component-wise-anchored-cas-root-2026-08-10/run_component_wise_root_integration.py
```

The driver invokes production `ContentAddressedInputStore` and
`LiveStateStore`. It uses real regular files, directories, and symbolic links
on the local overlayfs. `unittest.mock` only traces production `mkdirat` calls
and fixes the timing of ancestor replacement, first read, and exact leaf-writer
competition; it does not replace the filesystem implementation or reproduce
the algorithm.

`component-wise-root-integration.json` and its byte-identical `.log` contain
11 exact checks. The three test logs preserve the final directed, six-module,
and complete warnings-as-errors Python regression commands and summaries.
`SHA256SUMS.txt` covers every evidence file except itself.

## Environment

- Date: 2026-08-10 UTC
- Python: 3.12.3
- Kernel: Linux 7.0.0-28-generic x86_64
- Repository filesystem: overlayfs
- `os.open/os.mkdir/os.stat` support `dir_fd`: true
- `os.stat(..., follow_symlinks=False)` support: true
- `O_DIRECTORY`: 65536
- `O_NOFOLLOW`: 131072

## Interpretation

The result proves the tested local production mechanisms: missing root
components are created relative to already opened parent descriptors; no
configured-root component may be a symbolic link; in-flight publication/read
stays attached to its opened root/shard even after an ancestor rename; public
root drift is rejected before positive-cache admission; exact same-digest leaf
writers still converge; and relative configured roots yield stable absolute
public object paths.

This evidence does not exercise `openat2`, mount replacement, NFS, Lustre,
another host, MPI, a target, `afl-showmap`, a symbolic solver, or a fuzzing
campaign. It makes no throughput, coverage, bug-discovery, or LAVA-M uplift
claim.
