# F298 AFL++ Native Peer Sync Evidence

- Run date: 2026-07-31 UTC
- Work directory: `/tmp/symcc_f298_peer_j8nt6rtm`
- AFL++: 4.40c
- Python: 3.12.3
- Open MPI: 4.1.6
- Campaign budget: 75 s, `np=4`, one AFL master and two SymCC workers
- Profile mode: off
- Deliberate fault injection: parent environment contained `AFL_NO_SYNC=1`

The hybrid runner removed the inherited `AFL_NO_SYNC`, set
`AFL_SYNC_TIME=1` and `AFL_FINAL_SYNC=1`, and used the native campaign peer
layout:

```text
afl_out/
  fuzzer01/queue/       AFL master corpus
  fuzzer01/.synced/     native next-ID cursors
  symcc01/queue/        SymCC peer corpus, id:000000... sequential
```

## Binary identities

| Artifact | SHA-256 |
| --- | --- |
| SymCC target | `7b58190a6430c1cc1b3fff52391d358a7856e0ef873d02c5b1a8af7338a010d7` |
| AFL target | `5576d6da30c54d30b5706086c953bdacbb6e42160f2d872388b4d0423ae54d3c` |
| Seed | `01d448afd928065458cf670b60f5a594d735af0172c8d67f22a81680132681ca` |

## Result

| Metric | Value | Meaning |
| --- | ---: | --- |
| SymCC peer published | 23 | Files in `symcc01/queue` |
| AFL peer cursor | 22 | Native cursor value in `.synced/symcc01` |
| SymCC-attributed imports | 4 | AFL queue names containing `sync:symcc01` |
| Scanned but not retained | 18 | `22 - 4`; duplicate or no new AFL coverage |
| Unscanned hard-stop tail | 1 | `23 - 22` |
| AFL corpus imported, all peers | 4 | `fuzzer_stats: corpus_imported` |
| AFL edge coverage | 170 / 715 (23.78%) | Native `fuzzer_stats` |
| AFL executions | 1,205,634 | Native `fuzzer_stats` |
| AFL throughput | 15,862.35 exec/s | Native `fuzzer_stats` |
| AFL queue entries | 159 | End-of-run queue count |
| SymCC generated / interesting | 37 / 23 | Helper artifacts |

`symcc_peer_sync_complete=0` is intentional, honest reporting: AFL++ 4.40c
checks `stop_soon` inside peer synchronization. A hard termination can interrupt
the final scan at a testcase boundary before the next-ID cursor is committed.
The runner therefore does not equate publication with AFL consumption.

## Content attribution

All four retained imports are byte-identical to their named SymCC source:

| SymCC ID | AFL queue ID | SHA-256 |
| ---: | ---: | --- |
| 000001 | 000127 | `2856c2e796a6dd2b136cfcdbf0607d2b61f160bea46a106878dade949ae0052e` |
| 000013 | 000128 | `90a8547caaea970bcaf11a1207eb9ab3cdb4eb18ac7a9906c3492a9543254bfa` |
| 000018 | 000139 | `3e9db5a88bd8401d15f8a2627da82e0ce40824aeb7fc1717d57c198e8f7cec1d` |
| 000020 | 000140 | `d01bfc67357c5640fc06f9ea742ea6e35d58f3639f7d1731a7d704c51c7389a4` |

## Contract evidence and boundary

AFL++ 4.40c `sync_fuzzers` reads a four-byte native-endian next-ID cursor and
advances it over `id:%06u` peer entries. By contrast, `-F` foreign directories
are discovered by whole-second file `mtime`; a high-frequency producer can
publish a file with an `mtime` equal to the last observed maximum. F298
therefore uses the native `symcc01` campaign peer for SymCC and reserves `-F`
for genuinely external engines.

Primary sources:

- [AFL++ 4.40c peer synchronization source](https://github.com/AFLplusplus/AFLplusplus/blob/v4.40c/src/afl-fuzz-init.c)
- [AFL++ 4.40c foreign-directory scanner](https://github.com/AFLplusplus/AFLplusplus/blob/v4.40c/src/afl-fuzz-run.c)
- [AFL++ synchronization environment variables](https://github.com/AFLplusplus/AFLplusplus/blob/v4.40c/docs/env_variables.md)

This is one deterministic mechanism run, not an equal-CPU multi-round
performance comparison. It proves online transfer, cursor observability and
byte-level attribution; it does not prove a general coverage uplift.

## Regression gates

After the mechanism run and the independent queue/hang/crash ID fix, the
sequential full gates were:

| Gate | Result |
| --- | --- |
| Python unittest discovery | 480 / 480 passed, 83.682 s (after loop-dedup fix) |
| LLVM 18 lit | 210 / 210 passed, 133.54 s |
| LLVM 17 lit | 209 passed + 1 unsupported, 133.41 s |

An earlier LLVM 18 invocation omitted the project virtual environment from
`PATH` and therefore could not import Lark/parglare; it was interrupted and is
not a code-test result. The recorded LLVM results use the project environment.
