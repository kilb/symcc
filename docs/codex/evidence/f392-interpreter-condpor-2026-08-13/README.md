# F392 executable evidence

This directory preserves the executable evidence for bounded interpreter-level
ConDPOR (`symcc-interpreter-condpor-v1`).

## Contents

- `regeneration-program.json` is the closed SC-QF_BV program used to witness
  control-flow regeneration.
- `regeneration-certificate.json` is the complete recomputable exploration
  certificate. It contains one normal execution reading `init:x`, one assertion
  failure reading `t1:e0`, one accepted backward revisit, and three QF_BV UNSAT
  prunes.
- `targeted-python.txt` records 12 focused pytest cases.
- `related-schedule-python.txt` records 72 focused plus legacy schedule tests.
- `warnings-as-errors.txt` records the 12-case unittest run with Python warnings
  promoted to errors.
- `pytest-inventory.txt` records the exact 950-node canonical inventory and its
  digest.
- `full-python-gate.json` records 950 passed plus 235 subtests with all 16
  capabilities present and zero skip/xfail/xpass/deselect/identity drift.
- `full-lit.txt` records 251 discovered tests: 250 passed and one pre-existing
  unsupported test.
- `static-checks.txt` records py_compile, Ruff, Ruff format, and diff checks.
- `mechanism-benchmark.json` records 11 samples for 1--5 writer coherence and
  control regeneration. It explicitly disclaims cross-system performance,
  coverage, and bug-finding claims.
- `environment.txt` records OS, Python, Z3, CPU, and filesystem context.
- `delivery-verifier.txt` records the complete Codex delivery verifier passing
  F00--F392, top-level SHA, and package checks.
- `SHA256SUMS.txt` seals every evidence payload other than the manifest itself.

## Reproduce

```bash
python3 util/symcc_condpor.py explore \
  docs/codex/evidence/f392-interpreter-condpor-2026-08-13/regeneration-program.json \
  --output /tmp/f392-certificate.json

python3 util/symcc_condpor.py verify /tmp/f392-certificate.json

pytest -q test/test_condpor_interpreter.py test/test_schedule_exploration.py

python3 util/python_test_gate.py \
  --output /tmp/f392-full-gate.json \
  --min-collected 950 \
  --max-skips 0 --max-xfails 0 --max-xpasses 0 --max-deselected 0 \
  --max-missing-nodeids 0 --max-unexpected-nodeids 0 \
  --require-nodeid-manifest test/pytest-nodeids.json \
  -- -q -W error -p no:cacheprovider

lit -j32 build/test

python3 benchmark/benchmark_condpor_interpreter.py \
  --samples 11 --max-writers 5
```

## Claim boundary

The evidence establishes executable event regeneration and bounded exhaustive
search only for the admitted closed finite SC-QF_BV IR when no recorded bound is
hit. It is not evidence of native pthread/LLVM equivalence, weak-memory support,
unbounded optimality, fuzzing coverage, LAVA-M findings, or end-to-end speedup.
