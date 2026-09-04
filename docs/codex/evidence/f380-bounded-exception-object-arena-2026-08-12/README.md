# F380 bounded exception object arena evidence

This directory records reproducible evidence for the generation- and
ownership-certified exception object arena implemented on 2026-08-12.

## Scope

- fixed, non-zero `__cxa_allocate_exception` admission and deterministic arena
  layout;
- symbolic object stores followed by trivial-destructor `__cxa_throw`;
- same-frame and cross-frame ownership transfer, typed catch, object-memory
  load, and normal destruction;
- generation retention across slot reuse and fresh-executor checkpoint restore;
- fail-closed dynamic size, non-null destructor, interior pointer, ordinary
  heap-operation, mixed scalar/object producers, uninitialized-byte, bounds,
  owner, and generation cases.

The evidence establishes the implemented bounded mechanism. It does not
establish general C++ object exceptions, non-trivial destruction, RTTI
inheritance adjustment, nested caught stacks, benchmark throughput, coverage,
or vulnerability-discovery improvement.

## Reproduction

```bash
cmake --build build --target SymCC -j2
cmake --build build-llvm17 --target SymCC -j2
python3 -m pytest -q \
  test/test_live_exception_object_arena.py \
  test/test_live_scalar_catch_object.py \
  test/test_live_exception_lifecycle.py \
  test/test_live_typed_exception_semantics.py
cmake --build build --target check -j2
python3 util/python_test_gate.py \
  --output full-python-gate.json --min-collected 876 \
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

`exception-object-arena.json` returns 7 on the normal path and 42 from the
caught object's memory. `exception-object-cross-frame.json` returns 7 or 84
and demonstrates that call-form throw preserves the transferred object while
the producer frame is removed. The four named `*-reject.json` files preserve
ABI and producer-consistency admission diagnostics. `full-python-gate.json`
records the exact canonical
Python test identity set, and `checks.txt` records the final observed results.
