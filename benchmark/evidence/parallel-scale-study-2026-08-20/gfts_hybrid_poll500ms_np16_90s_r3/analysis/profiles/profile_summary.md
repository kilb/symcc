# Hybrid parallel profile summary

| total np | SymCC workers | edges | AUC | worker busy | worker exec | worker coverage/dedup | master scan | accepted |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 16 | 7 | 5553.3 | 5093.0 | 87.5% | 92.0% | 3.4% | 19.5s | 5.8% |

Worker phase percentages use accounted worker time as denominator. Master phase seconds are the last cumulative profile sample before campaign termination; consult `master_accounted_pct` before treating them as a complete wall-clock decomposition.
