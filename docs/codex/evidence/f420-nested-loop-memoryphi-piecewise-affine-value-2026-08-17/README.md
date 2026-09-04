# F420 Piecewise-Affine Writer-Value Evidence

This directory seals the mechanism evidence for F420. The verified boundary is
the exact F418 two-dimensional writer-address domain plus a direct unsigned or
equality induction guard selecting between two byte-complete F419 affine
bit-vector value arms.

Verified results:

- focused LLVM fixtures pass 3/3 on LLVM 17 and 3/3 on LLVM 18;
- the F416--F420 related LLVM set passes 12/12 on both LLVM versions;
- F420 Python tests pass 6/6 and the related Python set passes 27/27;
- the capability-closed Python gate passes 1,103 tests and 250 subtests with
  exact node-ID identity and no skip, xfail, xpass, deselection, or failure;
- the controlled eight-worker LLVM 17 gate discovers 298 tests, passes 296,
  and retains two existing unsupported tests;
- the finite oracle evaluates 96 configurations, accepts 48 complete
  summaries, rejects 48 unsupported shapes, checks 6,144 load vectors and
  58,752 defined bytes, and rejects 22/22 mutations;
- two oracle runs are byte-identical with SHA-256
  `c6d13d7149e38ad67e09726746f466583735d9870c2d6205010d4c7fe64a631b`;
- the reference certificate has 24 writer instances, 46 witnesses, 69
  last-write cases, and 46 guard-specialized byte expressions.

The reference timing JSON measures Python certificate validation and finite
guard selection only. It does not measure LLVM analysis, executor throughput,
SMT performance, fuzzing coverage, defect yield, or end-to-end speedup.

The authoritative interpretation is in
[`Nested_Loop_MemoryPhi_Piecewise_Affine_Value_Summary_F420_2026-08-17.md`](../../research-progress/Nested_Loop_MemoryPhi_Piecewise_Affine_Value_Summary_F420_2026-08-17.md).

