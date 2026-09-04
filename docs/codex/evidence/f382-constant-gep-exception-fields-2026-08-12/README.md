# F382 evidence

This directory preserves executable evidence for DataLayout-precise constant-GEP exception object fields.

- `aggregate-fields.json`: handwritten LLVM aggregate lowered to an executable four-slot continuation artifact.
- `source-llvm18.json`, `source-llvm17.json`: the same real C++ `throw struct` fixture compiled, canonicalized and lowered with LLVM 18/17.
- `*-lowering.txt`: exact frontend/export driver records, including separate compile and opt commands for source fixtures.
- `dynamic-field-reject.json`, `dynamic-store-reject.json`, `oob-store-reject.json`: fail-closed artifacts.
- `targeted-python.txt`: exception runtime and contract tests.
- `source-lit.txt`: old C source plus new C++ source regression gate.
- `full-lit.txt`: complete LLVM lit suite.
- `full-python-gate.json`: capability-closed canonical Python gate.
- `static-checks.txt`: LLVM 17/18 builds, Ruff, py_compile and whitespace checks.
- `checks.txt`: mechanically recomputed semantic summary.
- `SHA256SUMS.txt`: digest manifest for every other file in this directory.

The recorded complete gates are 239 passed plus one unsupported lit test, and
878 passed plus 229 passed subtests in the capability-closed Python gate. The
canonical inventory has no missing or unexpected node IDs and no skipped,
xfailed, xpassed, or deselected outcomes. The principal reproduction commands
are:

```bash
python3 -m pytest -q \
  test/test_live_exception_object_arena.py \
  test/test_live_scalar_catch_object.py \
  test/test_live_exception_lifecycle.py \
  test/test_live_typed_exception_semantics.py \
  test/test_live_exception_semantics.py \
  test/test_live_external_models.py -W error -p no:cacheprovider
python3 /usr/lib/llvm-18/build/utils/lit/lit.py -sv build/test
python3 util/python_test_gate.py \
  --output full-python-gate.json --min-collected 878 \
  --max-skips 0 --max-xfails 0 --max-xpasses 0 --max-deselected 0 \
  --max-missing-nodeids 0 --max-unexpected-nodeids 0 \
  --require-nodeid-manifest test/pytest-nodeids.json \
  --require-command cc --require-command z3 --require-command cvc5 \
  --require-command bitwuzla --require-command afl-clang-fast \
  --require-command afl-showmap --require-command mpiexec \
  --require-command openssl --require-command opt \
  --require-command llvm-diff --require-module mpi4py \
  --require-module tree_sitter --require-module tree_sitter_json \
  --require-module lark --require-module parglare --require-library z3 \
  -- -q -W error -p no:cacheprovider
```

The evidence proves mechanism correctness for a bounded trivial-integer aggregate subset. It does not prove complete C++ exception ABI support, benchmark coverage improvement, or performance improvement.
