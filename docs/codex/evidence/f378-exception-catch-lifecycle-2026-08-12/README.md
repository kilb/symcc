# F378 exception catch lifecycle evidence

This directory records reproducible evidence for the bounded scalar exception
token and catch lifecycle mechanism implemented on 2026-08-12.

## Scope

- exact LLVM admission of landingpad token, `__cxa_begin_catch`,
  `__cxa_end_catch`, and `__cxa_rethrow`;
- explicit unwinding, caught, and rethrowing checkpoint phases;
- normal catch destruction and rethrow-preserving end-catch;
- checkpoint/program capability binding and search-graph control edges;
- rejection of exception object materialization and malformed lifecycle state.

The evidence demonstrates implementation, semantic state transitions,
persistence, and fail-closed behavior. It does not claim complete C++ ABI
compatibility, benchmark throughput, coverage, or vulnerability-discovery
improvement.

## Reproduction

```bash
cmake --build build -j2
cmake --build build-llvm17 -j2
python3 -m pytest -q \
  test/test_live_exception_lifecycle.py \
  test/test_live_exception_semantics.py \
  test/test_live_typed_exception_semantics.py \
  test/test_live_state_scheduler.py
python3 -m ruff check \
  util/live_continuation.py util/live_state_search.py \
  util/check_live_continuation_lowering.py \
  test/test_live_exception_lifecycle.py
lit -sv --filter='live_continuation_lowering' build/test
```

`lifecycle-catch.json` and `lifecycle-rethrow.json` are successful LLVM
continuation artifacts. `object-materialization-reject.json` is the rejected
artifact report for a begin-catch return pointer consumed by `ptrtoint`.
`full-python-gate.json` records the exact 862-node capability-closed Python
gate, and `checks.txt` summarizes all final observed results.
