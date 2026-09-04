# F379 trivial scalar catch object evidence

This directory records reproducible evidence for the bounded direct scalar
projection from an Itanium-style catch object implemented on 2026-08-12.

## Scope

- exact admission of direct, non-atomic, non-volatile 1--64 bit integer loads
  from the `__cxa_begin_catch` result;
- lowering to the capability-closed `exception_value` operation;
- exact payload-width, caught-phase, and handler-frame checks;
- checkpoint restore by a fresh executor;
- rejection of GEP, store, atomic load, and pointer materialization.

The evidence establishes implementation, state transition, persistence, and
fail-closed behavior. It does not establish a general C++ exception object,
destruction, inheritance adjustment, benchmark throughput, coverage, or
vulnerability-discovery improvement.

## Reproduction

```bash
cmake --build build -j2
cmake --build build-llvm17 -j2
python3 -m pytest -q \
  test/test_live_scalar_catch_object.py \
  test/test_live_exception_lifecycle.py \
  test/test_live_exception_semantics.py \
  test/test_live_typed_exception_semantics.py
python3 -m ruff check \
  util/live_continuation.py util/check_live_continuation_lowering.py \
  test/test_live_scalar_catch_object.py
cmake --build build --target check -j2
python3 util/python_test_gate.py \
  --output full-python-gate.json --min-collected 868 \
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

`scalar-catch-object.json` is the successful continuation artifact. It returns
7 on the normal path and 42 on the typed catch path. The four `*-reject.json`
files preserve lowering diagnostics for unsupported object usage.
`full-python-gate.json` records the exact 868-node capability-closed gate, and
`checks.txt` records all final observed results.
