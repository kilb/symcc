# F425 Native ConDPOR and C11 Atomic Re-execution Evidence

This directory seals the implementation, regression, finite semantic, and
native execution evidence for F425.

Verified results:

- the independent SC source enumerator permits exactly `(0,1)`, `(1,0)`, and
  `(1,1)` for store buffering; the production model reports SC 3 SAT / 1 UNSAT
  and rejects both reads-from-init;
- TSO and RA each report 4 SAT / 0 UNSAT and permit both reads-from-init;
- the RA message-passing oracle rejects stale data after an acquire reads from
  the release, and three same-object writers produce all and only six
  modification orders;
- LLVM 17 and LLVM 18 each complete 40/40 prefix-controlled native atomic
  replays, with zero fallback, commit mismatch, or pending-atomic conflict;
- each version's native campaign reaches a bounded fixed point in three fresh
  processes over three unique prefixes with zero invalid runs;
- the focused Python set passes 12 tests, and the full schedule/campaign pair
  passes 73 tests;
- the complete capability-closed Python gate passes 1,143 tests plus 253
  subtests with zero skip, xfail, deselection, or node-ID drift;
- LLVM 17 discovers 309 tests and passes 307 with two expected unsupported;
  LLVM 18 discovers 309 and passes 308 with one expected unsupported;
- both versions pass all three focused native atomic/campaign lit tests;
- ruff, py_compile, warnings-as-errors preload compilation, whitespace checks,
  SVG parsing, PNG rendering, and bounded-resource review pass.

The evidence establishes a bounded native mechanism. It does not establish a
complete ISO C11/C++11 model, herd7/diy corpus coverage, executable weak-memory
reads-from control, an unbounded ConDPOR sound/complete/optimal theorem, or a
public-target coverage/time improvement.

See the [F425 research report](../../research-progress/Native_ConDPOR_C11_Atomic_Reexecution_F425_2026-08-17.md)
and [pipeline schematic](../../diagrams/native-condpor/f425_native_condpor_c11_pipeline.svg).

## Reproduce

```bash
python3 benchmark/check_native_condpor_c11_oracles.py \
  --symcc build/symcc --runtime build/libsymcc_schedule_rt.so \
  --repetitions 20 --output /tmp/f425-llvm18.json
python3 benchmark/check_native_condpor_c11_oracles.py \
  --symcc build-llvm17/symcc \
  --runtime build-llvm17/libsymcc_schedule_rt.so \
  --repetitions 20 --output /tmp/f425-llvm17.json

python3 -m pytest -q test/test_schedule_exploration.py \
  test/test_native_condpor_campaign.py
python3 /usr/lib/llvm-18/build/utils/lit/lit.py -j8 -sv build/test
python3 /usr/lib/llvm-17/build/utils/lit/lit.py -j8 -sv build-llvm17/test
```
