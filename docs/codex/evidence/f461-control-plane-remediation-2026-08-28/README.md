# F461 control-plane remediation evidence

This directory preserves machine-readable local evidence for the 2026-08-28
deep review remediation. It contains mechanism measurements and verification
summaries, not an end-to-end coverage, defect-yield, or multi-node speedup claim.

## Files

- `frontier-microbenchmark.json`: corrected v2 benchmark with one logical clock,
  checked completion/abandon outcomes, base+journal bytes, and 31 hot-snapshot
  samples per scale.
- `coverage-memory.json`: `tracemalloc` peaks for packed dense coverage grouping.
- `verification-summary.json`: focused, repository-wide, dual-LLVM, build and
  static-check results for the final source snapshot.
- `SHA256SUMS.txt`: hashes of the three evidence payloads and this README.

The frontier run is local overlayfs, single-process mechanism evidence. The
coverage-memory run measures Python allocation only. Neither substitutes for a
paired, equal-budget public-target campaign.
