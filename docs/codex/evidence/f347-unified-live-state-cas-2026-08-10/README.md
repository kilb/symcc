# F347 Unified Live-State CAS Evidence

Date: 2026-08-10 UTC

This directory records the bounded evidence for suffix-aware CAS publication
and stable no-follow reads in `LiveStateStore`.

## Implementation under test

- `ContentAddressedInputStore` accepts a validated leaf suffix while preserving
  its digest-sharded namespace and F346 publication proof chain.
- `LiveStateStore` retains the existing
  `objects/<digest-prefix>/<digest-rest>.json` layout.
- Every live-state write now uses the shared CAS writer rather than trusting
  `os.path.isfile()` or maintaining a weaker publication implementation.
- Every live-state read uses one bounded, no-follow regular-file snapshot and
  closes descriptor/path identity before JSON parsing.
- Successful reads remember the verified inode identity; failed or
  digest-mismatched reads remove prior positive cache knowledge.

## Commands and exact results

Directed storage tests:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -W error \
  test/test_distributed_state.py \
  -k 'ContentAddressedInputStoreTests or LiveStateStoreTests'
9 passed, 141 deselected, 3 subtests passed in 0.33s
```

Related six-module regression:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -W error \
  test/test_distributed_state.py test/test_mpi_filesystem_qualification.py \
  test/test_mpi_lifecycle.py test/test_hybrid_feedback.py \
  test/test_afl_profile_orchestration.py test/test_adaptive_components.py
352 passed, 77 subtests passed in 16.98s
```

Complete project Python regression:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -W error test/test_*.py
745 passed, 97 subtests passed in 93.88s (0:01:33)
```

Production-primitive integration:

```text
PYTHONHASHSEED=0 python3 \
  docs/codex/evidence/f347-unified-live-state-cas-2026-08-10/\
run_live_state_cas_integration.py
```

The integration result has 8/8 exact checks true. It materializes and restores
a six-object continuation graph, verifies the compatible `.json` layout,
repairs a pre-existing exact-content symlink without following it, repairs a
cached corrupt object, accepts one exact competing writer, rejects a path
replacement during a read, rejects an exact-content read symlink, clears failed
read identity facts, and leaves no temporary publication names.

## Artifact inventory

- `checks.txt`: environment, implementation, observations, complexity, and
  claim boundary.
- `directed-tests.log`: focused storage test command and result.
- `related-tests.log`: six-module regression command and result.
- `full-tests.log`: complete project Python regression command and result.
- `run_live_state_cas_integration.py`: deterministic integration driver using
  production `LiveStateStore`, `ContentAddressedInputStore`, and stable snapshot
  code.
- `live-state-cas-integration.json`: canonical integration result.
- `live-state-cas-integration.log`: exact replay output.
- `SHA256SUMS.txt`: complete nested manifest for every file above except the
  manifest itself.

## Evidence boundary

This is local mechanism evidence on real regular files and symlinks with
deterministic publication/read fault injection. It does not run MPI, a target,
`afl-showmap`, a symbolic solver, or a fuzzing campaign. It therefore makes no
throughput, coverage, bug-discovery, or LAVA-M uplift claim. Final-component
no-follow and trusted filesystem metadata do not constitute a Byzantine
storage proof or a multi-host NFS/Lustre race-frequency measurement.
