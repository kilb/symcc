# F422 Executable Nested-Loop Memory Summary Transfer Evidence

This directory seals the implementation and bounded semantic evidence for the
first executable loop-summary transfer in the live continuation engine.

Verified results:

- the independent source-loop oracle covers 176 configurations;
- 2,112/2,112 memory bytes and 2,112/2,112 initializedness markers agree;
- 33/33 fully initialized i16 loads agree, while 143 partial or uninitialized
  loads retain their definedness domain and are not assigned an arbitrary
  expected value;
- all 176 enabled executions apply one transfer, fork zero states, and require
  at most 27 steps;
- the focused executable/fallback fixtures pass 2/2 on LLVM 17 and LLVM 18;
- the F416--F422 related LLVM set passes 16/16 on both LLVM versions;
- the related Python set passes 35/35;
- the complete Python gate passes 1,111 tests and 250 subtests with no skip,
  xfail, deselection, missing node ID, or unexpected node ID;
- the complete LLVM 18 suite passes 303 tests with one expected unsupported
  test; LLVM 17 passes 302 tests with two expected unsupported tests;
- warning-as-error builds, ruff, py_compile, whitespace checks, transactional
  failure injection, and diagram rendering pass.

The evidence is a bounded refinement result for the admitted i2/i2, i16,
stack-memory Decision-DAG fixture. It is not evidence for general LoopSCC,
heap or multi-object replacement, solver-time improvement, fuzzing coverage,
defect yield, or end-to-end campaign speedup. See the
[`F422 research report`](../../research-progress/Executable_Nested_Loop_Memory_Summary_Transfer_F422_2026-08-17.md).
