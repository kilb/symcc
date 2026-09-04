# F437 Primary Research Sources

- Mallob, *Scalable Job Scheduling for Parallel SAT Solving*:
  <https://arxiv.org/abs/2205.06590>. F437 learns fixed-worker pair utility;
  it does not implement Mallob's malleable grow/shrink allocation.
- SAT 2025, *Streamlining Distributed SAT Solver Design*:
  <https://drops.dagstuhl.de/entities/document/10.4230/LIPIcs.SAT.2025.27>.
  It motivates measuring sharing utility beyond conventional clause metadata.
- TACAS 2026, *Real-time Proof Checking for Distributed Incremental SAT
  Solving*: <https://satres.kikit.kit.edu/papers/2026-tacas-distrincproof.pdf>.
  F437 remains on the project-native JSON/LRUP stream and does not claim wire
  interoperability.
- SAT 2026, *A Natively Parallel Proof Framework for Clause-Sharing SAT
  Solving*: <https://drops.dagstuhl.de/entities/document/10.4230/LIPIcs.SAT.2026.17>.
  This motivates F439 PalRUP/ImpCheck interoperability, which remains open.
- SAT 2025, *Problem Partitioning via Proof Prefixes*:
  <https://drops.dagstuhl.de/entities/document/10.4230/LIPIcs.SAT.2025.3>.
  Proof-prefix partitioning remains a separate P0 gap.

These sources motivate architecture and remaining gaps. Their performance
claims are not copied to this implementation.
