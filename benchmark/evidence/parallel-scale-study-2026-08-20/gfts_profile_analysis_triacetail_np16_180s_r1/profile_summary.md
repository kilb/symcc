# Hybrid parallel profile summary

| total np | SymCC workers | edges | AUC | worker busy | worker exec | worker coverage/dedup | master scan | accepted |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 16 | 7 | 5690.0 | 5306.5 | 60.9% | 50.1% | 3.0% | 12.4s | 7.5% |

## Master triage detail

| total np | dominant sub-phases |
|---:|---|
| 16 | batch_core_s=72.44s, observation_callback_s=69.46s, adaptive_scheduler_s=59.37s, semantic_proposals_s=9.53s, queue_publish_s=0.99s, policy_updates_s=0.41s, directory_commit_s=0.40s, semantic_fallback_s=0.14s |

Worker phase percentages use accounted worker time as denominator. Master phase seconds are the last cumulative profile sample before campaign termination; consult `master_accounted_pct` before treating them as a complete wall-clock decomposition.
