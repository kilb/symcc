# F432 Incremental QF_BV SAT Evidence

This directory seals implementation, semantic-oracle, mechanism-cost, review,
and gate evidence for F432. The authoritative interpretation is
`../../research-progress/Verified_Incremental_QFBV_SAT_and_Proof_DAG_F432_2026-08-17.md`.

Key files:

- `oracle-run-{1,2}.json`: two seeded 512-case real-CaDiCaL runs plus cvc5 matrix cross-check and LRAT lifting;
- `benchmark-run-{1,2}.json`: two 64-round cold-process/native-context mechanism profiles;
- `actual-backend-smoke.json`: real CLI and shared-library QueryStore completion;
- `full-python-gate.json`: canonical identity and outcome gate;
- `full-suite-summary.json`: machine-readable synthesis of every final gate;
- `related-python-gate.txt`, `llvm17-lit.txt`, `llvm18-lit.txt`: regression gates;
- `f432_qfbv_incremental_sat.{svg,png}`: reviewed architecture and trust-boundary figure;
- `source-research.txt`, `environment.txt`, `review-findings.txt`, `claim-boundary.txt`: provenance and interpretation;
- `source-contract.txt`: SHA-256 contract for every authoritative F432 implementation and test path;
- `SHA256SUMS.txt`: complete non-self manifest, generated after all other files.

The evidence supports QF_BV semantic correctness and same-process mechanism
cost only. It does not establish fuzzing coverage, bug yield, multi-node
scalability, mid-solve real-time proof streaming, or complete reproduction of
the cited systems.
