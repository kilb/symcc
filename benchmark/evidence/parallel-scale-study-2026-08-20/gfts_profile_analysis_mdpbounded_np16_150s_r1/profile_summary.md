# Hybrid parallel profile summary

| total np | SymCC workers | edges | AUC | worker busy | worker exec | worker coverage/dedup | master scan | accepted |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 16 | 7 | 5663.0 | 5295.9 | 68.9% | 56.8% | 5.5% | 12.6s | 4.4% |

## Master triage detail

| total np | dominant sub-phases |
|---:|---|
| 16 | batch_core=25.91s, observation_callback=22.30s, adaptive_scheduler=14.28s, adaptive_prefix_dag=12.81s, semantic_proposals=7.35s, queue_publish=1.06s, adaptive_ect=0.60s, adaptive_edge_dependence=0.55s |

Worker phase percentages use accounted worker time as denominator. Master phase seconds are the last cumulative profile sample before campaign termination; consult `master_accounted_pct` before treating them as a complete wall-clock decomposition.
