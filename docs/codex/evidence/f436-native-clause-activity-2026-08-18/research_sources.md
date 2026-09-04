# F436 Research Sources

Primary research and upstream interfaces used to scope F436:

1. Audemard et al., *Painless: A Framework for Parallel SAT Solving*, SAT
   2017. Modular diversification and clause-sharing architecture.
   <https://www.lrde.epita.fr/dload/papers/le-frioux.17.sat.pdf>
2. Schreiber and Sanders, *Scalable SAT Solving in the Cloud*, SAT 2022
   (Mallob). Malleable jobs and distributed clause sharing.
   <https://arxiv.org/abs/2205.06590>
3. Sanders et al., *Streamlining Distributed SAT Solver Design*, SAT 2025.
   Controlled analysis of distributed clause-sharing design choices.
   <https://drops.dagstuhl.de/entities/document/10.4230/LIPIcs.SAT.2025.27>
4. Schreiber et al., *Real-time Proof Checking for Distributed Incremental SAT
   Solving*, TACAS 2026. Incremental distributed proof checking and ImpCheck.
   <https://satres.kikit.kit.edu/papers/2026-tacas-distrincproof.pdf>
5. Schreiber et al., *A Natively Parallel Proof Framework for Clause-Sharing
   SAT Solving*, SAT 2026. PalRUP proof artifacts and a sequential trusted
   checker boundary.
   <https://drops.dagstuhl.de/entities/document/10.4230/LIPIcs.SAT.2026.17>
6. CaDiCaL 3.0.1 source and `ExternalPropagator` interface at pinned revision
   `c60730422e758ef1cebe7aeddf2dda31c996bf04`.
   <https://github.com/arminbiere/cadical>

F436 adopts consumer-side native trail observation after proof checking. It
does not claim wire compatibility with ImpCheck/PalRUP, Mallob-style resource
malleability, or a reproduction of the cited systems' performance results.
