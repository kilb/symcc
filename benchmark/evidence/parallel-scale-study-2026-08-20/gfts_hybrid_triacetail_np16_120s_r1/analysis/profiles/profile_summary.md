# Hybrid parallel profile summary

| total np | SymCC workers | edges | AUC | worker busy | worker exec | worker coverage/dedup | master scan | accepted |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 16 | 7 | 5486.0 | 5131.3 | 90.3% | 92.4% | 3.4% | 14.0s | 6.8% |

Worker phase percentages use accounted worker time as denominator. Master phase seconds are the last cumulative profile sample before campaign termination; consult `master_accounted_pct` before treating them as a complete wall-clock decomposition.
