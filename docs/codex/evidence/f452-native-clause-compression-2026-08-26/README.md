# F452 evidence: native checker clause compression

This directory seals implementation, regression, sanitizer, mechanism-cost,
and real CaDiCaL hot-path evidence for F452.

## Claim supported

Proof-authorized clauses in the native realtime import path use a canonical
unsigned-sort, delta, variable-length encoding. Encodings of at most seven
bytes are stored inline; larger encodings use explicit owned storage. CaDiCaL
consumes literals through a bounded cursor and emits an ACK only after the
complete clause has been decoded. Python and QueryStore bind telemetry to the
compression protocol and reject incomplete or inconsistent evidence.

This evidence supports a checker storage/codec mechanism claim. It does not
claim solver speedup, process-RSS reduction, fuzzing coverage, defect yield, or
reproduction of the upstream 1216-core experiment.

## Artifacts

- `focused-tests.log`: 23 focused compression tests.
- `broad-tests.log`: 37 compression/realtime coupled tests.
- `full-python-gate.json` and `.log`: identity-exact 1467-test gate plus 310
  subtests; all required capabilities present.
- `full-python-gate.time.txt`: shell wall/user/system timing.
- `oracle.json`: 20000 property cases, nine payload/latency workloads, and a
  real CaDiCaL 3.0.1 two-clause delivery case.
- `environment.txt`: compiler, interpreter, source commit, and nodeid identity.
- `review.txt`: six review rounds and fixes.
- `static-checks.txt`: Python compile, GCC/Clang Werror builds, ABI symbols,
  ASan/UBSan, SVG parsing, shell parsing, and whitespace checks.
- `SHA256SUMS.txt`: complete manifest over every artifact except itself.

## Reproduction

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q \
  -p no:cacheprovider -W error test/test_qfbv_clause_compression.py

PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q \
  -p no:cacheprovider -W error \
  test/test_qfbv_clause_compression.py test/test_qfbv_realtime_stream.py

python3 benchmark/check_qfbv_clause_compression_oracles.py \
  --cases 20000 --clauses 512 --repeats 9 \
  --cadical-source /path/to/pinned/cadical-3.0.1 \
  --output /tmp/f452-oracle.json
```

The CaDiCaL source must be commit
`c60730422e758ef1cebe7aeddf2dda31c996bf04`, built with its shared library.

