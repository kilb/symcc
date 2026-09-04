# F348 Budgeted Transitive Live-State Evidence

Date: 2026-08-10 UTC

This directory records bounded evidence for transitive, memoized, and
resource-budgeted restoration of content-addressed live continuations.

## Implementation under test

- One restore follows the checkpoint parent chain and validates every
  referenced solver-frame, expression, symbolic-store, memory-root,
  memory-page, symbolic cell, and program object.
- A restore-local digest map reuses each parsed mapping and charges each Merkle
  DAG node once, even when parent and child checkpoints share most state.
- Independent unique-object and canonical-byte limits fail before an
  over-budget object is parsed or retained.
- Deep page schemas, duplicate symbolic names, duplicate page/cell locations,
  frame structure, and parent checkpoint identities are validated.
- JSON or schema failures revoke positive identity-cache facts.

## Commands and exact results

Directed graph-budget tests:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -W error \
  test/test_distributed_state.py test/test_mpi_lifecycle.py \
  -k 'restore_is_transitively_validated_and_graph_budgeted or restore_rejects_deep_schema_and_semantic_cache_failures or graph_budget_configuration_requires_positive_integers or live_state_graph_limits_are_shared_and_bounded'
4 passed, 208 deselected, 4 subtests passed in 0.63s
```

Related six-module regression:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -W error \
  test/test_distributed_state.py test/test_mpi_filesystem_qualification.py \
  test/test_mpi_lifecycle.py test/test_hybrid_feedback.py \
  test/test_afl_profile_orchestration.py test/test_adaptive_components.py
356 passed, 81 subtests passed in 17.20s
```

Complete project Python regression:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -W error test/test_*.py
749 passed, 101 subtests passed in 94.35s (0:01:34)
```

Production-primitive integration:

```text
PYTHONHASHSEED=0 python3 \
  docs/codex/evidence/f348-budgeted-transitive-live-state-2026-08-10/\
run_graph_budget_integration.py
```

The integration result has 9/9 exact checks true. A two-checkpoint parent DAG
contains eight unique objects and 2,062 canonical bytes. Shared roots and
expressions require exactly eight stable snapshots. Exact limits pass; one
fewer object or one fewer byte fails. A wrong-schema page reachable only from
the parent, a duplicate-name symbolic store, and invalid JSON all fail closed.

## Artifact inventory

- `checks.txt`: environment, implementation, observations, complexity, and
  claim boundary.
- `directed-tests.log`: focused graph-budget tests.
- `related-tests.log`: six-module regression.
- `full-tests.log`: complete project Python regression.
- `run_graph_budget_integration.py`: deterministic driver using production
  `LiveStateStore` restoration and stable snapshot code.
- `graph-budget-integration.json`: canonical integration result.
- `graph-budget-integration.log`: exact replay output.
- `SHA256SUMS.txt`: complete nested manifest for every file above except the
  manifest itself.

## Evidence boundary

This is local mechanism evidence over real content-addressed files with
deterministically constructed malformed graphs and exact boundary budgets. It
does not run MPI, a target, `afl-showmap`, a symbolic solver, or a fuzzing
campaign. It therefore makes no throughput, coverage, bug-discovery, or
LAVA-M uplift claim. The canonical-byte budget is not a precise measurement of
Python object overhead, and graph validation is not a Byzantine storage proof.
