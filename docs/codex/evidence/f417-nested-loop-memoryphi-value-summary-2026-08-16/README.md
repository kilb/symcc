# F417 Nested-Loop MemoryPhi Last-Write Value Summary Evidence

This directory seals the mechanism evidence for F417. It demonstrates a real
LLVM 17/18 producer, strict reconstruction by the continuation consumer,
target-endian constant-byte binding, bounded last-writer selection, and
fail-closed unsupported cases.

Verified scope:

- two focused LLVM fixtures pass 2/2 on LLVM 17 and 2/2 on LLVM 18, with 15
  RUN commands across the little/default- and big-endian modules;
- the F411--F417 related LLVM set passes 8/8 on both LLVM versions;
- F417 Python tests pass 5/5; the related Python set passes 35/35;
- the complete Python identity gate passes 1,086 tests plus 250 subtests with
  zero skip, xfail, xpass, deselection, collection error, missing node ID, or
  unexpected node ID;
- the complete LLVM 17 gate passes 284 tests with two pre-existing
  unsupported tests and zero failures;
- the independent finite oracle covers 3,456 cases, accepts 496 summaries,
  rejects 2,960 unsupported/partial cases, checks 16,416 runtime load vectors
  and 62,112 defined value bytes, and rejects 18/18 reference mutations;
- the generated 64-byte producer boundary contains four writer-value records,
  15 stored bytes, 456 lane witnesses, and 855 last-write cases;
- a real big-endian LLVM module binds `i16 0x1234` as bytes `[18, 52]`, while
  a non-byte-sized `i1` store is rejected instead of assigning padding bits.

One complete LLVM run was initially launched concurrently with the four-minute
Python gate. The pre-existing `poly_exact_widening_renaming.c` test reached its
roughly 500 ms solver budget and failed. It passed immediately in isolation;
the complete LLVM gate then passed under exclusive load. The initial raw log
was overwritten by the required final rerun, so `resource-contention-audit.txt`
records that limitation rather than claiming the missing log is sealed.

The benchmark JSON measures Python reference validation and first-match
selection only. It is not LLVM construction latency, executor throughput,
solver performance, fuzzing coverage, defect yield, or end-to-end speedup.

The authoritative interpretation and support boundary are in
[`Nested_Loop_MemoryPhi_Last_Write_Value_Summary_F417_2026-08-16.md`](../../research-progress/Nested_Loop_MemoryPhi_Last_Write_Value_Summary_F417_2026-08-16.md).
