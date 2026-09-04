# F390 executable evidence

This directory binds mechanism evidence for the bounded recursive cyclic
byte-lane writer graph. It is not a coverage, throughput, solver-speed,
bug-finding, LAVA-M, or public-benchmark result.

- `recursive-llvm18.json` and `recursive-llvm17.json` contain the same direct
  three-branch/four-leaf tree lowered by both LLVM versions.
- `forwarded-llvm18.json` preserves explicit branch-to-leaf jump corridors.
- `grouped-llvm18.json` is the conservative negative boundary: it has no F390
  capability, marker, or graph poison source.
- Each base artifact has one graph, two cyclic endpoints, one recursive
  transfer, three branches, three store leaves, one pure-carry leaf, four
  graph store IDs including the seed, and three poison-bound leaf stores.
- `artifact-checks.txt` actively removes capability/marker and drifts a store
  address/poison source for all three base artifacts.
- `targeted-python.txt` covers six direct/forwarded, shadow, carry, poison,
  closure, and specialized-borrowing tests.
- `writer-graph-python.txt` covers all 31 F385--F390 writer-graph tests.
- `related-live-python.txt`, `full-python-gate.json`, and `full-lit.txt` bind
  the related and complete regression results.

The verifier derives every leaf's immediate parent from the validated tree;
the artifact does not self-assert that proof boundary. Seed, join-prefix, and
leaf paths are replayed separately. Writer paths are bounded to 64 blocks,
forwarding corridors to 16 blocks, and every internal reverse step has one
predecessor.

This contract excludes branch-scoped shared writers in grouped/repeated,
composed, multicarry, multigroup, and trigroup variants. It also excludes
general MemorySSA, arbitrary SCCs or multiple latches, symbolic pointers,
concurrent memory, and independent reconstruction of LLVM poison predicates.
