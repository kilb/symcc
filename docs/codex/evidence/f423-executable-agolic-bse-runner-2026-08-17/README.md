# F423 Executable Agolic BSE Runner Evidence

This directory seals the implementation and bounded semantic evidence for the
F423 Agolic harness-entry and witness-guided BSE runner.

Verified results:

- the focused runner suite passes 15/15;
- the Agolic/live-state/persistent-frontier/exception/CBC/CGS/F422 related set
  passes 108 tests and 36 subtests;
- the capability-closed full Python gate passes 1,126 tests and 250 subtests,
  with no skip, xfail, xpass, deselection, collection error, missing node ID, or
  unexpected node ID;
- the independent two-byte source oracle covers 8 configurations, with 4
  released and 4 unreleased witnesses, 8 replay-equivalent candidates, both
  target outcomes in every released candidate set, 8 planner outcomes, and 6
  rejected evidence mutations;
- two oracle runs are byte-identical with SHA-256
  `76ac6084f9936b91791483ab25cbf65af89037a82274cfde63e20daa14588eb4`;
- target function/op ownership, witness replacement, non-terminal candidate,
  contradictory status, stale prior coverage, and outstanding-frontier bounds
  have explicit fail-closed regressions;
- ruff, Python byte compilation, whitespace checks, diagram rendering, and
  XML parsing pass.

F423 changes only Python executor/runner/test/documentation code. The LLVM
compiler/runtime suite was therefore not rerun for this increment. The
immediately preceding F422 evidence records LLVM 17 at 302 passed plus 2
unsupported and LLVM 18 at 303 passed plus 1 unsupported; those numbers are
historical adjacent evidence, not an F423 rerun.

The evidence establishes a real runner only for the admitted bounded
continuation IR. It does not establish native KLEE equivalence, arbitrary
external-effect semantics, public-target coverage improvement, defect yield,
or end-to-end speedup. See the
[`F423 research report`](../../research-progress/Executable_Agolic_Witness_Guided_BSE_Runner_F423_2026-08-17.md).

## Reproduce

```bash
python3 benchmark/check_agolic_bse_runner_oracles.py \
  --output /tmp/f423-oracle.json

PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q \
  test/test_agolic_bse_runner.py

PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 util/python_test_gate.py \
  --output /tmp/f423-full-python-gate.json \
  --min-collected 1126 \
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
