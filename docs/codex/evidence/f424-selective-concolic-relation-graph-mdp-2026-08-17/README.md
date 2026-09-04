# F424 Selective Concolic Relation-Graph and MDP Evidence

This directory seals the implementation, regression, and bounded semantic
evidence for F424.

Verified mechanism results:

- the independent source oracle enumerates all 65,536 assignments of the
  four-variable/four-bit formula and all 16 witness configurations;
- the formula has 10 full models and one PC_c partial model;
- all 16 completions produce an exact full-formula candidate, with zero false
  SAT and zero false UNSAT;
- the relation graph has four edges, cut weight one, one shared variable, and
  one random-only variable;
- synchronous MDP iteration matches the analytic two-state fixed point to
  approximately 1.22e-13 and converges in 17 rounds with residual 5.54e-13;
- the targeted Python set passes 2/2 and the related set passes 135 tests plus
  25 subtests;
- the complete capability-closed Python gate passes 1,130 tests plus 253
  subtests with zero skip, xfail, deselection, or node-ID drift;
- the focused query helper integration passes 1/1 on LLVM 17 and LLVM 18;
- the complete LLVM 17 suite passes 304 tests with two expected unsupported
  tests; LLVM 18 passes 305 tests with one expected unsupported test;
- dual-version C++ rebuilds, ruff, py_compile, whitespace checks, diagram
  rendering, resource-bound review, and three authority reviews pass.

The evidence establishes a bounded QF_BV mechanism. It does not reproduce the
FM 2026 KLEE/JFS/METIS/LIBSVM implementation, QF_BVFP/public-function corpus,
reported coverage/time improvements, or a >=20-run equal-CPU campaign. See the
[`F424 research report`](../../research-progress/Selective_Concolic_Relation_Graph_and_MDP_F424_2026-08-17.md)
and [`post-F424 gap audit`](../../research-progress/SOTA_Gap_Audit_After_F424_2026-08-17.md).

## Reproduce

```bash
python3 benchmark/check_selective_concolic_mdp_oracles.py \
  --output /tmp/f424-oracle.json

python3 -m pytest -q test/test_selective_concolic_mdp.py
python3 -m pytest -q \
  test/test_selective_concolic_mdp.py \
  test/test_hybrid_feedback.py test/test_query_store.py \
  test/test_self_config.py test/test_parasuit_parameter_policy.py \
  test/test_parasuit_value_policy.py

lit -sv build/test --filter='query_solver_selective.py'
lit -sv build-llvm17/test --filter='query_solver_selective.py'
lit -j8 -sv -o /tmp/f424-llvm18-full.json build/test
lit -j8 -sv -o /tmp/f424-llvm17-full.json build-llvm17/test

PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 util/python_test_gate.py \
  --output /tmp/f424-full-python-gate.json --min-collected 1130 \
  --max-skips 0 --max-xfails 0 --max-xpasses 0 --max-deselected 0 \
  --max-missing-nodeids 0 --max-unexpected-nodeids 0 \
  --require-nodeid-manifest test/pytest-nodeids.json \
  --require-command cc --require-command z3 --require-command cvc5 \
  --require-command bitwuzla --require-command afl-clang-fast \
  --require-command afl-showmap --require-command mpiexec \
  --require-command openssl --require-command opt --require-command llvm-diff \
  --require-module mpi4py --require-module tree_sitter \
  --require-module tree_sitter_json --require-module lark \
  --require-module parglare --require-library z3 \
  -- -q -W error -p no:cacheprovider
```
