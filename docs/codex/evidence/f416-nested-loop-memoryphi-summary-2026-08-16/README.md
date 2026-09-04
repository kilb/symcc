# F416 Nested-Loop MemoryPhi Summary Evidence

This directory seals the mechanism evidence for F416. It demonstrates a real
LLVM 17/18 producer, an independent strict continuation consumer, exact
two-level MemoryPhi composition, finite reference-model equivalence, and
fail-closed source/artifact boundaries.

Verified scope:

- F416 focused LLVM fixture: 1/1 on LLVM 17 and 1/1 on LLVM 18, with 14 RUN
  commands inside the fixture;
- F411--F416 related LLVM fixtures: 6/6 on both LLVM versions;
- F416 Python tests: 5/5; related F411--F416 Python tests: 30/30;
- complete Python gate: 1,081 passed plus 250 subtests, with zero skip, xfail,
  xpass, deselection, collection error, missing node ID, or unexpected node ID;
- complete LLVM 17 gate: 281 passed plus two pre-existing unsupported tests,
  with zero failures;
- independent finite oracle: 18,240 cases, 670 accepted, 17,570 rejected,
  21,480 runtime/composed bitmap equivalences, 52,662 last-writer provenance
  cells, and 26/26 reference mutations rejected;
- generated producer boundary: two loop levels, two MemoryPhi nodes, four
  writers, 456 witnesses, 855 alternatives, and nine fixed-point rounds.

The benchmark JSON measures Python reference-validator cost and analytic
certificate cardinality only. It is not LLVM construction latency, executor
throughput, solver performance, coverage, bug yield, an implementation of
general LoopSCC, or end-to-end speedup.

The authoritative interpretation and support boundary are in
[`Nested_Loop_MemoryPhi_Summary_Composition_F416_2026-08-16.md`](../../research-progress/Nested_Loop_MemoryPhi_Summary_Composition_F416_2026-08-16.md).
