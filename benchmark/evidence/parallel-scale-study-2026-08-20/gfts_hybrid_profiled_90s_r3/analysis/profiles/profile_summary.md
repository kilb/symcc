# Hybrid parallel profile summary

| total np | SymCC workers | edges | AUC | worker busy | worker exec | worker coverage/dedup | master scan | accepted |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 1 | 5355.3 | 4899.7 | 88.8% | 83.9% | 2.9% | 36.8s | 26.3% |
| 8 | 3 | 5478.3 | 5018.2 | 90.2% | 90.1% | 4.8% | 34.4s | 8.8% |
| 16 | 7 | 5533.7 | 5089.5 | 85.9% | 88.3% | 3.8% | 30.2s | 6.6% |
| 32 | 12 | 5615.0 | 5147.6 | 82.1% | 88.8% | 4.0% | 28.9s | 5.3% |

Worker phase percentages use accounted worker time as denominator. Master phase seconds are the last cumulative profile sample before campaign termination; consult `master_accounted_pct` before treating them as a complete wall-clock decomposition.

## Accepted SymCC throughput model

The useful concolic rate is master-accepted SymCC inputs per second, excluding AFL executions and worker-rejected candidates.

- USL sigma=0.3026, kappa=0, R2=0.7231
- 10% doubling-gain ceiling: 11 SymCC workers
