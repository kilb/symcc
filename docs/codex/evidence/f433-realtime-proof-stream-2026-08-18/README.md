# F433 evidence: realtime checked QF_BV proof stream

Date: 2026-08-18

This directory contains first-party mechanism evidence for F433. It does not
contain a fuzzing coverage, defect-yield, network, or multi-node scaling claim.

## Artifacts

| File | Meaning |
|---|---|
| `oracle_run1.json` | 512 randomized real-CaDiCaL baseline/stream comparisons, seed 62515 |
| `oracle_run2.json` | exact repeated oracle run for reproducibility |
| `benchmark_run1.json` | 64-round native cold/warm and realtime idle/checked-import cost |
| `benchmark_run2.json` | independent repeated mechanism benchmark |
| `focused_tests.txt` | focused pytest node identities and outcome |
| `full_python_tests.txt` | complete Python identity/capability gate outcome |
| `full_python_gate.json` | machine-readable capability, outcome and node-ID gate |
| `full_suite_summary.json` | cross-gate result summary used by delivery verification |
| `llvm17_lit.txt` | LLVM 17 lit discovery and failure summary |
| `llvm18_lit.txt` | LLVM 18 lit discovery and failure summary |
| `f433_realtime_proof_stream.{svg,png}` | sealed architecture/trust-boundary figure |
| `static_checks.txt` | Python/C++/shell/diff static gate results |
| `review_findings.txt` | deep-review findings and dispositions |
| `source_research.txt` | primary literature and exact upstream source pins |
| `source_manifest.txt` | implementation, test, report and diagram SHA-256 identities |
| `SHA256SUMS.txt` | hashes for every evidence file except itself |

## Result summary

- Oracle run 1: 512 cases, 0 mismatch, 512 delivered imports, 512 replayed ACKs.
- Oracle run 2: 512 cases, 0 mismatch, 512 delivered imports, 512 replayed ACKs.
- Both runs reject a mutated ACK and obtain native result `0` before explicit
  termination reset and result `10` after reset.
- Benchmark run 1: 64/64 imports delivered; checked-import cold total cost is
  1.498516x the equivalent native cold path.
- Benchmark run 2: 64/64 imports delivered; checked-import cold total cost is
  1.487772x the equivalent native cold path.
- Idle stream median is 2367 us and 2339 us. This is explicit overhead on an
  unusually short 110--116 us warm solve, not a performance improvement.
- The capability-closed Python gate passed 1242 tests plus 291 subtests with
  exact node-ID equality and no skips; LLVM 17 and LLVM 18 each discovered 323
  lit tests with no failure or unresolved result.

The real native identity in all four JSON artifacts is:

```text
symcc-qfbv-realtime-v1|cadical-3.0.1-c607304
```

The tested shim SHA-256 is:

```text
5afef95ef6744d9a5aed36a4cd4375b9268dcbe256439634f9999f6412a8cb06
```

## Reproduction

```bash
python3 test/qfbv_realtime_stream_oracle.py \
  --library /path/to/libsymcc_qfbv_cadical_realtime.so \
  --cases 512 --seed 62515

python3 test/qfbv_realtime_stream_benchmark.py \
  --library /path/to/libsymcc_qfbv_cadical_realtime.so \
  --rounds 64
```

The shim must be built by `benchmark/install_cadical_3_0_1.sh`, which pins
CaDiCaL commit `c60730422e758ef1cebe7aeddf2dda31c996bf04`.
