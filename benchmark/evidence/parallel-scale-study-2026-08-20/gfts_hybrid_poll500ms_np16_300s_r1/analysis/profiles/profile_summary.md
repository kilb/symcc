# Hybrid parallel profile summary

| total np | SymCC workers | edges | AUC | worker busy | worker exec | worker coverage/dedup | master scan | accepted |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 16 | 7 | 5791.0 | 5442.3 | 58.2% | 42.1% | 4.2% | 36.6s | 4.6% |

Worker phase percentages use accounted worker time as denominator. Master phase seconds are the last cumulative profile sample before campaign termination; consult `master_accounted_pct` before treating them as a complete wall-clock decomposition.
