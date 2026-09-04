# F305 replay-verified online EVP recovery evidence

Generated on 2026-08-04 with:

```bash
python3 benchmark/run_evp_recovery_smoke.py \
  --output docs/codex/evidence/f305-replay-verified-evp-recovery-2026-08-04
```

The run first publishes the exact domain `[1]`. It then replaces the otherwise
valid checkpoint record with `[2]` while deliberately preserving the artifact
label. Restart performs one deterministic recovery replay, detects one semantic
mismatch, marks the coordinator dirty, and replaces the sidecar with `[2]`.

The second phase observes `[3]`, publishes `[2,3]`, and injects an `OSError`
only for the state checkpoint write. The runtime generation remains usable, the
coordinator records one checkpoint failure and stays dirty. A subsequent
semantic no-op retries the checkpoint successfully and persists
`checkpoint_failures=1` with `dirty=false`.

This is deterministic mechanism and recovery evidence. It does not establish a
coverage, solver-time, campaign-throughput, or crash-frequency improvement on a
public benchmark.
