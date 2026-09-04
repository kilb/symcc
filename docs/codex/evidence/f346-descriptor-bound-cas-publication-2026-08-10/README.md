# F346 Descriptor-Bound CAS Publication Evidence

This directory records unit/regression results and a deterministic local
production-primitive integration for descriptor-bound content-addressed object
publication.

## Files

- `run_cas_publication_integration.py`: real-file driver with deterministic
  post-rename mutation, replacement, exact-competitor, and symlink injection.
- `cas-publication-integration.json`: eight recomputable mechanism checks.
- `cas-publication-integration.log`: exact structured integration output.
- `directed-tests.log`: distributed-state and MPI-lifecycle regression summary.
- `integration-tests.log`: six related modules with warnings as errors.
- `full-tests.log`: every `test/test_*.py` module with warnings as errors.
- `checks.txt`: implementation facts, observations, and claim boundaries.
- `SHA256SUMS.txt`: exact manifest for all other files in this directory.

## Reproduce

From this directory:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 run_cas_publication_integration.py \
  > cas-publication-integration.json
cp cas-publication-integration.json cas-publication-integration.log
```

From the repository root:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -W error \
  test/test_distributed_state.py test/test_mpi_lifecycle.py

PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -W error \
  test/test_distributed_state.py test/test_mpi_filesystem_qualification.py \
  test/test_mpi_lifecycle.py test/test_hybrid_feedback.py \
  test/test_afl_profile_orchestration.py test/test_adaptive_components.py

PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -W error test/test_*.py
```

## Interpretation

The integration invokes the production `ContentAddressedInputStore.put()` on
real files and patches only the publication boundary to deterministically
create interleavings that are otherwise timing-dependent. The uncontended case
performs no stable fallback hash. A same-inode mutation and a wrong-content
replacement are rejected after one fallback hash; an exact-content replacement
is accepted after one fallback hash; a symlink is rejected without following or
hashing its target. Failed publications create no positive identity-cache fact,
and temporary names are cleaned.

These artifacts establish local mechanism behavior and regression
compatibility. They do not establish race frequency, distributed scaling,
campaign throughput, solver acceleration, coverage improvement, bug discovery,
or LAVA-M uplift. No MPI transport, target, afl-showmap, symbolic solver, or
fuzzing campaign is executed by the integration driver.
