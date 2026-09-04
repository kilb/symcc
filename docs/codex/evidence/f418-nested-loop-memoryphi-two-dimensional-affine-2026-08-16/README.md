# F418 Two-Dimensional Affine Nested-Loop Summary Evidence

This directory seals the mechanism evidence for F418. It covers the real LLVM
producer, strict independent reconstruction, target-endian constant values,
finite two-dimensional instance enumeration, lexicographic last-write
selection, generated boundaries, and fail-closed unsupported shapes.

Verified scope:

- three focused LLVM fixtures pass on both LLVM 17 and LLVM 18, with 17 RUN
  commands across default/little endian, real big endian, generated cardinality,
  isolated affine-stride, dynamic-value fallback, and non-affine rejection;
- the F416--F418 related LLVM and Python suites pass without regression;
- the related LLVM set is 6/6 on each LLVM version and the related Python set
  is 15/15;
- the capability-closed Python gate passes 1,091 tests and 250 subtests with
  exact node-ID identity and no skip/xfail/deselection;
- the controlled LLVM 17 complete gate passes 288 tests with two existing
  unsupported tests and no failure;
- the independent reference oracle evaluates 4,608 shapes, accepts 28 complete
  summaries, rejects 4,580 unsupported/partial shapes, checks 608 concrete vs
  summarized load vectors and 3,384 defined bytes, and rejects 18/18 mutations;
- two oracle runs are byte-identical with SHA-256
  `7be3e10a9aead4949038be68c0bd5a4b82639156fa5c64f55df9494d2d1ddd4e`;
- the reference benchmark contains 24 writer-instance records, 12 fixed-point
  pairs, 46 lane witnesses, and 69 last-write cases;
- a real big-endian module returns `0xAA34`, while dynamic values and
  `outer*inner` indices do not receive a v8 certificate;
- review found and fixed a missing strided-capability condition when the affine
  inner coefficient or pointer scale, rather than the induction step or writer
  width, creates the address stride.

The timing JSON measures Python reference reconstruction and selection only.
It is not LLVM lowering latency, continuation throughput, SMT performance,
fuzzing coverage, defect yield, or end-to-end speedup.

The first LLVM 17 complete run used the default 192-worker fanout and an
existing QF_BV campaign rejected its conformance artifact. That test passed in
isolation, and the full 290-test suite then passed with eight workers. The
initial, isolated, and controlled-complete logs are all retained; the evidence
records concurrency sensitivity without asserting an unproved root cause.

The authoritative support boundary and interpretation are in
[`Nested_Loop_MemoryPhi_Two_Dimensional_Affine_Summary_F418_2026-08-16.md`](../../research-progress/Nested_Loop_MemoryPhi_Two_Dimensional_Affine_Summary_F418_2026-08-16.md).
