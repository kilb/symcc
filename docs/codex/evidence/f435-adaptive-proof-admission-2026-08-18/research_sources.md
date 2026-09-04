# F435 primary research sources

1. Schreiber and Sanders, *Scalable Job Scheduling for Parallel SAT Solving*,
   arXiv 2205.06590 and the Mallob repository:
   https://arxiv.org/abs/2205.06590
   https://github.com/domschrei/mallob
2. Le Frioux et al., *Painless: A Framework for Parallel SAT Solving*:
   https://www.lrde.epita.fr/dload/papers/le-frioux.17.sat.pdf
   https://github.com/lip6/painless
3. *Streamlining Distributed SAT Solver Design*, SAT 2025:
   https://drops.dagstuhl.de/entities/document/10.4230/LIPIcs.SAT.2025.27
4. *Real-time Proof Checking for Distributed Incremental SAT Solving*,
   TACAS 2026:
   https://satres.kikit.kit.edu/papers/2026-tacas-distrincproof.pdf

F435 does not claim a complete reproduction of these systems. It implements a
project-native, proof-first, deterministic admission and feedback layer over the
existing F433 realtime CaDiCaL stream. Native ImpCheck/PalRUP wire compatibility,
native utilization/activity, worker pairing, Mallob-style malleability, WAN transport and
public multi-node experiments remain outside the delivered scope.

The SAT 2025 MallobSat evaluation reports no measurable benefit from forwarding
or shuffling per-clause LBD values. Accordingly, F435 does not treat missing LBD
as an efficacy gap: a future LBD field would be diagnostic only, while actual
consumer utilization and solve contribution are the relevant missing feedback.
