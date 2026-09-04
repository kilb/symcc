# F389 executable evidence

This directory binds the mechanism evidence for the bounded multi-arm cyclic
byte-lane writer graph. It is not a coverage, throughput, solver-speed,
bug-finding, or public-benchmark result.

- `multiarm-llvm18.json` and `multiarm-llvm17.json`: the same direct three-leaf
  loop lowered by both LLVM versions.
- `forwarded-llvm18.json`: all three branch-to-arm corridors are explicitly
  replayed in the forwarded variant.
- Each artifact has one graph, two cyclic endpoints, one transfer, three arms,
  two store arms, one pure-carry arm, three graph store IDs including the seed,
  and two poison-bound arm stores.
- `artifact-checks.txt`: all three artifacts pass capability, marker, address,
  and poison-source active tampering.
- `targeted-python.txt`: five F389 model tests, including shadow and carry-arm
  counterexamples.
- `writer-graph-python.txt`: all 25 F385--F389 writer-graph tests.
- `related-live-python.txt`: all 84 live-state tests plus 51 subtests.
- `full-python-gate.json`: exact 913-node gate plus 229 subtests with zero
  identity or outcome degradation.
- `full-lit.txt`: controlled 32-worker lit result.
- `checks.txt`, `static-checks.txt`, and `SHA256SUMS.txt`: semantic summary,
  static/delivery gates, and evidence digests.

The root arm is replayed from its edge to the root branch. Inner-true and
inner-false arms are independently replayed to the inner branch. Seed and
join-prefix paths remain separate proofs. Every internal path step has one
predecessor; writer paths are bounded to 64 blocks and forwarded corridors to
16 blocks. First covering stores must match function-local ID, address-derived
byte offset, and width.

This contract does not cover recursive condition trees, general SCC or
multi-latch MemorySSA, symbolic pointers, concurrent memory, or independent
derivation of LLVM poison predicates.
