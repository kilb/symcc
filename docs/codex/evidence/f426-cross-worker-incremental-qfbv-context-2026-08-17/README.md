# F426 Cross-Worker Incremental QF_BV Context Evidence

This directory seals the implementation, finite differential, real-solver,
failure-path, and full-regression evidence for F426.

Verified results:

- an independent encoder agrees with the production manifest builder for 32
  chains over depths 1--4, four offset bases, and two bit-vector operators;
- two real cvc5 backends solve three SAT queries with assignments 66, 67, and
  68; worker A extends an exact local parent and worker B observes an exact
  shared context while its local cache is cold;
- all three models pass QueryStore completion, with two immutable context
  objects, one cross-worker exact hit, one parent reuse, and zero quota timeout;
- tamper, symlink, concurrent publication, stale-token, TTL reclaim, global
  quota, timeout, cancellation, bounds, and store-configuration drift paths are
  exercised;
- 21 focused Python tests pass; the related set passes 49 tests plus 25
  subtests;
- the capability-closed Python gate passes 1,153 tests plus 253 subtests with
  zero skip, xfail, deselection, collection error, or node-ID drift;
- LLVM 17 discovers 310 tests and passes 308 with two expected unsupported;
  LLVM 18 discovers 310 and passes 309 with one expected unsupported;
- both LLVM versions pass the two focused F426 lit drivers;
- ruff, py_compile, whitespace checking, SVG parsing, and PNG inspection pass.

The evidence establishes content-addressed formula-plan transport and verified
SAT consumption. It does not establish transport of serialized solver internals
or learned clauses, a checkable SMT UNSAT proof, or an end-to-end speedup on a
public benchmark.

See the [F426 research report](../../research-progress/Cross_Worker_Incremental_QFBV_Context_F426_2026-08-17.md)
and [architecture schematic](../../diagrams/solver-context/f426_cross_worker_qfbv_context.svg).

## Reproduce

```bash
python3 benchmark/check_cross_worker_context_oracles.py \
  --iterations 20 --output /tmp/f426-context.json
python3 -m pytest -q test/test_cross_worker_context.py \
  test/test_qf_bv_backend.py test/test_query_store.py
python3 /usr/lib/llvm-18/build/utils/lit/lit.py -j8 -sv build/test
python3 /usr/lib/llvm-17/build/utils/lit/lit.py -j8 -sv build-llvm17/test
```

