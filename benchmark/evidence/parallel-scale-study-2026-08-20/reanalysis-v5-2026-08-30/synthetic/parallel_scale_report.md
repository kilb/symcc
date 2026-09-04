# Parallel scale analysis: synthetic-parallel_scaling

| compute workers | concolic | AFL | rounds | total work/s | AFL exec/s | SymCC cand/s | unique/s | retention | edges |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1 | 0 | 3 | 42.90 | 0.00 | 42.90 | 16.21 | 37.794% | 155.0 |
| 3 | 3 | 0 | 3 | 67.60 | 0.00 | 67.60 | 23.80 | 35.203% | 182.3 |
| 7 | 7 | 0 | 3 | 95.28 | 0.00 | 95.28 | 31.36 | 32.915% | 193.0 |
| 15 | 15 | 0 | 3 | 117.88 | 0.00 | 117.88 | 36.47 | 30.938% | 190.3 |
| 31 | 31 | 0 | 3 | 131.19 | 0.00 | 131.19 | 40.04 | 30.518% | 191.3 |

## Evidence quality

- attempted=15, successful=15, failed=0, timed-out=0, success-rate=1.000
- decision eligible: **false**
- withholding reasons: 5 allocation(s) have fewer than 3 independent random seeds; 5 allocation(s) reuse a random seed; repeated executions are technical replicates, not independent evidence; role assignments were inferred for 15 run(s); coverage measurement provenance is missing for 16 row(s); coverage sampling provenance is missing for 16 row(s); planned wall budgets differ by 1.90% across runs; endpoint coverage requires equal exposure; coverage-saturation model is unavailable: invalid coverage observation

## Model

`C(N) = gamma*N / (1 + sigma*(N-1) + kappa*N*(N-1))`

- useful-throughput USL: gamma=14.17, sigma=0.3434, kappa=0, R2=0.9762
- raw-throughput USL: gamma=35.56, sigma=0.2522, kappa=0, R2=0.9818
- SymCC component USL (concolic-worker axis): R2=0.9818
- coverage saturation: not fitted because the observations violate the monotone model (invalid coverage observation)
- recommended ceiling: **withheld**; exploratory point estimate=9

The estimate is workload- and budget-specific. Refit it after changing the target, seed corpus, campaign duration, scheduler, or machine.
