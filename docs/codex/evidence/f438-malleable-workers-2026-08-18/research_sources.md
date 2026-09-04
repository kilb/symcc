# F438 Primary Research Sources

Access and scope were reviewed on 2026-08-18.

1. [Mallob: Scalable SAT Solving in the Cloud](https://arxiv.org/abs/2205.06590)
   defines malleability as adding or removing processing power during a
   computation and studies concurrent SAT jobs with dynamic allocation. F438
   adopts logical resource reallocation, not Mallob's full distributed stack.
2. [Distributed Incremental SAT Solving with Mallob](https://arxiv.org/abs/2505.18836)
   studies distributed incremental solving. F438 uses family-scoped feedback
   and persistent assignments but does not claim a solver reproduction.
3. [Streamlining Distributed SAT Solver Design](https://drops.dagstuhl.de/entities/document/10.4230/LIPIcs.SAT.2025.27)
   studies flexible load balancing and resource shifts in MallobSat. F438's
   deterministic score and two-phase transition are project-specific.
4. [Painless: A Framework for Parallel SAT Solving](https://www.lrde.epita.fr/dload/papers/le-frioux.17.sat.pdf)
   motivates modular parallel solver and clause-sharing components. F438 keeps
   physical slots separate from logical jobs and preserves proof-first checks.
5. [A Natively Parallel Proof Framework for Clause-Sharing SAT Solving](https://drops.dagstuhl.de/entities/document/10.4230/LIPIcs.SAT.2026.17)
   introduces PalRUP and decentralized persistent parallel checking. F438 does
   not implement PalRUP wire interoperability; that remains F439.
6. [Real-time Proof Checking for Distributed Incremental SAT Solving](https://satres.kikit.kit.edu/papers/2026-tacas-distrincproof.pdf)
   provides the proof-first incremental context for F433--F438. Resource
   reassignment never authorizes unchecked learned knowledge.
7. [Problem Partitioning via Proof Prefixes](https://drops.dagstuhl.de/entities/document/10.4230/LIPIcs.SAT.2025.3)
   derives massively parallel partitions from proof prefixes. F438 does not yet
   produce partition completeness or non-overlap certificates.
