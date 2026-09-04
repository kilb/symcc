# F454 evidence bundle

This directory records the 2026-08-26 acceptance evidence for solver-native
clause sharing and official PalRUP confirmation.

## Contents

- `oracle.json`: canonical 1/2/4-rank, three-round mechanism result and SAT
  negative control;
- `oracle.stdout.json`: the exact JSON emitted by the oracle command;
- `focused-tests.log`: F454 producer tests (`13 passed + 7 subtests`);
- `coupled-tests.log`: F439/F440/F454 coupled tests
  (`34 passed + 26 subtests`);
- `full-python-gate.log`, `.json`, `.time.txt`: capability-closed complete gate
  (`1496 passed + 317 subtests`, 16/16 capabilities, exact node IDs);
- `sanitizer-result.json`: 2-rank wrapper-boundary ASan/UBSan result;
- `sanitizer.txt`: sanitizer build/run scope, options and stderr result;
- `static-checks.txt`: lint, shell syntax, strict C++ build and ABI checks;
- `build-metadata.txt`, `source-manifest.txt`, `environment.txt`: fixed toolchain
  and source identities;
- `review.txt`: eleven review rounds and the corrections they produced;
- `SHA256SUMS.txt`: hashes for the evidence files, excluding itself.

## Result boundary

All nine main samples passed the initial official check, a complete staging
recheck, and a final-path recheck after publication. The 2- and 4-rank samples
imported shared clauses; all main
samples satisfied fan-out conservation and had zero dropped clauses. After all
solver threads joined, the terminal pending medians were 0/1,002/10,192 for
1/2/4 ranks and the sample maximum was 10,441. That residue is recorded rather than
hidden and is outside the proof-correctness boundary. A separate capacity-one
test covered the drop path without invalidating the proof.
The deterministic SAT formula was rejected and created no root.

The median native pool times for 1/2/4 ranks were 0.264/0.224/0.273 seconds,
while median end-to-end times were 3.218/6.974/9.760 seconds. This is mechanism
evidence and exposes proof/checking overhead; it is not a solver-speedup result.

The sanitizer run instruments the SymCC wrapper/C ABI boundary while linking the
fixed uninstrumented upstream CaDiCaL static archive. Leak detection is disabled
because the host Python interpreter is uninstrumented. It therefore cannot be
represented as a full-stack sanitizer or leak qualification.

Primary report:
`../../research-progress/Solver_Native_Clause_Sharing_PalRUP_Production_F454_2026-08-26.md`.
