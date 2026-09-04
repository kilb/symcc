# F377 declarative pure external model evidence

This directory records the reproducible evidence for the bounded,
capability-closed pure external model mechanism implemented on 2026-08-12.

## Scope

- LLVM declaration and model admission;
- canonical `external_pure` artifact generation;
- independent runtime validation and QF_BV evaluation;
- symbolic feasibility forking and nounwind normal-edge routing;
- fresh-process restoration of the modeled expression from CAS;
- malformed, impure, incompatible, duplicate-site, and capability-closure
  rejection cases.

The evidence demonstrates implementation and semantic-mechanism behavior. It
does not claim benchmark, coverage, throughput, or vulnerability-discovery
improvement.

## Reproduction

```bash
cmake --build build -j2
python3 -m pytest -q \
  test/test_live_external_models.py \
  test/test_live_exception_semantics.py \
  test/test_live_typed_exception_semantics.py
python3 -m pytest -q test/test_live_external_models.py
python3 -m ruff check \
  util/live_continuation.py util/live_state_search.py \
  util/check_live_continuation_lowering.py \
  test/test_live_external_models.py
lit -sv --filter='live_continuation_lowering' build/test
```

`checks.txt` records the final observed results. The generated continuation
artifacts retain the canonical model, ABI widths, stable site, and function
provenance so that the evidence can be audited without executing a host
external function.
