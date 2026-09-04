# F419 Affine Symbolic Writer-Value Evidence

This directory seals the mechanism evidence for F419. The verified boundary is
the exact F418 two-dimensional writer-address domain plus byte-complete affine
bit-vector values over the outer IV, inner IV, and at most one entry integer
argument.

Verified results:

- focused LLVM fixtures pass 3/3 on LLVM 17 and 3/3 on LLVM 18;
- the F416--F419 related LLVM set passes 9/9 on both LLVM versions;
- F419 Python tests pass 6/6 and the related Python set passes 21/21;
- the capability-closed Python gate passes 1,097 tests and 250 subtests with
  exact node-ID identity and no skip, xfail, xpass, deselection, or failure;
- the controlled eight-worker LLVM 17 gate discovers 294 tests, passes 292,
  and retains two existing unsupported tests;
- the finite oracle evaluates 1,152 configurations, accepts six complete
  summaries, rejects 1,146 unsupported/partial shapes, checks 384 load vectors
  and 1,392 defined bytes, and rejects 18/18 mutations;
- two oracle runs are byte-identical with SHA-256
  `fae5d9ccaaa58ed3735bfce1ca83885cb6f4d3b67087e0e3b636e196097dfa04`;
- the reference certificate has 24 writer instances, 46 witnesses, 69
  last-write cases, and 46 symbolic byte expressions.

The reference timing JSON measures Python certificate validation and expression
selection only. It does not measure LLVM analysis, executor throughput, SMT
performance, fuzzing coverage, defect yield, or end-to-end speedup.

The authoritative interpretation is in
[`Nested_Loop_MemoryPhi_Affine_Symbolic_Value_Summary_F419_2026-08-17.md`](../../research-progress/Nested_Loop_MemoryPhi_Affine_Symbolic_Value_Summary_F419_2026-08-17.md).

