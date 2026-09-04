# F421 Affine Decision DAG Writer-Value Evidence

This directory seals the mechanism evidence for F421. The verified domain is
the exact F418 finite two-dimensional writer-address model plus a bounded,
postorder, shared Decision DAG whose guards compare an outer or inner induction
value with a constant and whose leaves are F419 affine bit-vector values.

Verified results:

- focused LLVM fixtures pass 2/2 on LLVM 17 and 2/2 on LLVM 18;
- the F416--F421 related LLVM set passes 14/14 on both LLVM versions;
- F421 Python tests pass 5/5 and the F416--F421 related set passes 32/32;
- the finite oracle evaluates 1,152 configurations and checks 55,296 load
  vectors, 608,256 scalar loads, and 165,888 defined bytes;
- the oracle observes 114,048 complete and 494,208 partial or uninitialized
  loads, and rejects 6/6 unsupported shapes;
- two oracle runs are byte-identical with SHA-256
  `235b7fed1b5110e125d56fe04752edd0b92ad3003821cef31b195398a50fcf13`;
- the producer and strict consumer build with LLVM 17 and LLVM 18 under the
  repository warning-as-error configuration;
- ruff, Python byte compilation, and whitespace checks pass.

The timing JSON measures a Python finite reference implementation only. It
does not measure LLVM lowering, executor throughput, solver time, coverage,
defect yield, or end-to-end speedup. F421 still executes the real loop. The
authoritative interpretation is in
[`Nested_Loop_MemoryPhi_Affine_Decision_DAG_Value_Summary_F421_2026-08-17.md`](../../research-progress/Nested_Loop_MemoryPhi_Affine_Decision_DAG_Value_Summary_F421_2026-08-17.md).

