# F393 executable evidence

This directory preserves executable evidence for the recoverable UCSan JITI
object context implemented in F393.

## Contents

- `input-seed.ucsan` is the initial root plus cyclic alias graph.
- `snapshot-1.ucsan` is published after native object mutation.
- `snapshot-2.ucsan` is produced by a fresh process replaying snapshot 1.
- `snapshot-verification.json` is the strict canonical graph report.
- `snapshot-byte-identity.txt` records the equal SHA-256 identities.
- `targeted-python.txt` records 10 strict seed/graph tests.
- `warnings-as-errors.txt` records the same 10 tests with warnings promoted.
- `targeted-lit-llvm18.txt` and `targeted-lit-llvm17.txt` record 5/5 native
  UCSan tests on both toolchains.
- `full-python-gate.json` records 954 passed plus 235 subtests, exact node-ID
  identity, all 16 capabilities, and zero outcome degradation.
- `full-lit.txt` records 252 discovered, 251 passed, one pre-existing
  unsupported test, and zero failures.
- `mechanism-benchmark.json` records 31 whole-process samples, each containing
  two durable publications. It disclaims coverage and cross-system speedup.
- `static-checks.txt`, `pytest-inventory.txt`, and `environment.txt` preserve
  tool and environment gates.
- `checks.txt` is the machine-readable semantic and claim summary.
- `SHA256SUMS.txt` seals every payload other than the manifest itself.

## Reproduce

```bash
pytest -q test/test_ucsan_seed.py
lit -v -j8 --filter='ucsan' build/test
lit -v -j8 --filter='ucsan' build-llvm17/test

python3 util/ucsan_seed.py verify \
  docs/codex/evidence/f393-ucsan-recoverable-object-context-2026-08-13/snapshot-1.ucsan \
  --require-canonical

cmp \
  docs/codex/evidence/f393-ucsan-recoverable-object-context-2026-08-13/snapshot-1.ucsan \
  docs/codex/evidence/f393-ucsan-recoverable-object-context-2026-08-13/snapshot-2.ucsan
```

## Claim boundary

The evidence establishes strict v1 seed admission, bounded JITI object
materialization, canonical durable snapshot publication, and fresh-process
object-context replay for the admitted native test. It is not evidence of an
arbitrary native continuation, a full C++ ABI, the complete UCSan OOB/UAF/UBI
checker suite, fuzzing coverage improvement, bug yield, or paper-level
cross-system performance.
