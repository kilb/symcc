# F456 evidence bundle

This directory seals the 2026-08-26 mechanism acceptance evidence for the
bounded POSE-C initial symbolic heap.

## Contents

- `oracle.json` and `oracle.stdout.json`: exact 9-check finite-domain and
  motivating-case path oracle;
- `mechanism-benchmark.json` and `.stdout.json`: 11-round 1/2/4/8/16-reference
  Python mechanism costs;
- `focused-tests.log`: 25 tests plus 256 subtests;
- `coupled-tests.log`: 232 tests plus 282 subtests across F456, distributed
  state, continuation/frontier and cross-worker context;
- `full-python-gate.json`, `.log`, `.time.txt`: 1542 tests plus 579 subtests,
  16/16 capabilities and exact 1542/1542 node IDs;
- `static-checks.txt`: lint, compile, diff, SVG, Z3 and oracle checks;
- `environment.txt`: execution/tool identity;
- `source-research.txt`: primary-source version and claim boundary;
- `review.txt`: eleven review rounds and resulting corrections;
- `source-manifest.txt`: implementation/test/document SHA-256 identities;
- `SHA256SUMS.txt`: every evidence file except itself.

## Measured mechanism result

The independent oracle passed 9/9 checks. Three-reference read and conditional
store each agreed with concrete alias semantics on 27/27 assignments. The
local swap, sum and bounded-list-max10 cases produced 2, 1 and 12 CFG paths,
respectively, and zero heap-generated paths. The mechanism benchmark's
16-reference row represented 82,864,869,804 alias/null partitions with 289
terms and an 85,960-byte snapshot; median Python build/load/snapshot cost was
2,662.702 microseconds over 11 runs.

## Claim boundary

The 21/23/78 lazy trace counts are values reported by POSE arXiv v2, not local
measurements. Local timing is Python domain cost, not a lazy-initialization,
solver, fuzzing, coverage, defect-yield or end-to-end speedup experiment. The
implemented contract is a bounded C-layout adaptation; automatic arbitrary
LLVM/C heap recovery and concurrent heap semantics remain outside it.

Primary report:
`../../research-progress/POSE_C_Initial_Symbolic_Heap_F456_2026-08-26.md`.
