# F415 Ordered Multi-Writer Transfer Evidence

This directory seals the mechanism evidence for F415. It proves production
LLVM lowering, strict independent artifact replay, bounded reference-model
equivalence, cross-LLVM compatibility, and complete regression status. It does
not claim public-target coverage, solver throughput, defect yield, or
end-to-end speedup.

## Result Summary

- LLVM 17/18 focused fixture: 1/1 each.
- F411--F415 related LLVM fixtures: 5/5 on LLVM 17 and 5/5 on LLVM 18.
- F415 Python tests: 5/5; related F411--F415 Python tests: 25/25.
- Full Python gate: 1,076 passed plus 250 subtests; zero skips, xfails,
  deselections, missing node IDs, or unexpected node IDs.
- Full LLVM 17 gate: 279 passed, 2 existing unsupported, zero failed.
- Oracle: 13,632 cases; 3,312 accepted; 10,320 rejected; 231,282 runtime
  bitmap equivalences; 718,968 last-writer provenance cells; 22/22 mutations
  rejected.
- Maximum producer fixture: four latches, four writers per latch, 16 writers,
  456 witnesses, and 7,296 alternatives.
- Reference benchmark: 8 writer metadata records, 456 witnesses, 2,964
  alternatives, and median 211,515 ns/certificate over 11 x 1,000 Python
  validations.

## Files

- `environment.txt`: exact toolchain, bounds, and claim boundary.
- `source-research.txt`: primary-source research basis and local inference.
- `source-contract.txt`: production schema/capability/runtime contract.
- `ordered-multilatch-memoryphi-oracles.json`: deterministic finite oracle.
- `ordered-multilatch-memoryphi-benchmark.json`: frozen mechanism benchmark.
- `targeted-*`, `related-*`, `full-*`: focused, compatibility, and complete
  gates.
- `static-checks.txt`: builds, Ruff, Python compilation, diff, index, diagram,
  and fixture checks.
- `SHA256SUMS.txt`: complete manifest of every other file in this directory.

The report is
[`Ordered_MultiWriter_MemoryPhi_Transfer_F415_2026-08-16.md`](../../research-progress/Ordered_MultiWriter_MemoryPhi_Transfer_F415_2026-08-16.md).

