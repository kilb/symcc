# Hybrid parallel profile summary

| total np | SymCC workers | edges | AUC | worker busy | worker exec | worker coverage/dedup | master scan | accepted |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 16 | 7 | 5702.0 | 5316.4 | 69.9% | 57.8% | 4.2% | 13.3s | 7.5% |

## Master triage detail

| total np | dominant sub-phases |
|---:|---|
| 16 | batch_core=4.99s, observation_callback=2.75s, adaptive_scheduler=2.29s, adaptive_prefix_dag=2.20s, queue_publish=0.84s, semantic_proposals=0.40s, directory_commit=0.07s, adaptive_ect=0.04s |

Worker phase percentages use accounted worker time as denominator. Master phase seconds are the last cumulative profile sample before campaign termination; consult `master_accounted_pct` before treating them as a complete wall-clock decomposition.
