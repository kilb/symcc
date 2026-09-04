# Verification Record

Run on 2026-07-30 UTC from `/home/ubuntu/code/symcc`.

| Command / gate | Result |
|---|---|
| `cmake --build build -j16` | exit 0 |
| `python3 -m py_compile benchmark/run_benchmark.py benchmark/analyze_current_evaluation.py test/test_current_evaluation_analysis.py` | exit 0 |
| `env PATH=/usr/lib/llvm-18/bin:$PATH lit -sv build/test` | 207 discovered, 207 passed, 135.22 s |
| targeted lit filter for current analysis and memory-state rejection | 2 passed |
| `git diff --check` | exit 0 |
| SVG parse with `xml.etree.ElementTree` | valid 1120 x 815 SVG |
| PNG render with headless Chrome | exit 0, 1120 x 815; manually inspected |
| SQLite SymCC target smoke run | exit 0 |
| libarchive SymCC target smoke run | exit 0 |
| 20-repeat legacy/current campaigns | 164 CSV rows including four seed rows; all status `success`, zero target crashes |
| 300 s current campaign | six CSV rows including two seed rows; all status `success`, zero target crashes |
| post-fix 10 s telemetry smoke | `generated=4434`, `afl_generated=4434`, `symcc_generated=0`, `symcc_interesting=0`; zero crashes |

The full lit suite was rerun after fixing final `symcc_interesting` parsing.
The original 300 s CSV predates that parsing fix and is intentionally retained
unchanged; its 47/50 values are early progress snapshots, not final cumulative
counts.
