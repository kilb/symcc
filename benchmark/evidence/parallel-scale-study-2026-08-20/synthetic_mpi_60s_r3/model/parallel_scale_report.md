# Parallel scale analysis: synthetic-parallel_scaling

| workers | rounds | generated/s | unique/s | acceptance | edges |
|---:|---:|---:|---:|---:|---:|
| 1 | 3 | 42.90 | 16.21 | 37.8% | 155.0 |
| 3 | 3 | 67.60 | 23.80 | 35.2% | 182.3 |
| 7 | 3 | 95.28 | 31.36 | 32.9% | 193.0 |
| 15 | 3 | 117.88 | 36.47 | 30.9% | 190.3 |
| 31 | 3 | 131.19 | 40.04 | 30.5% | 191.3 |

## Model

`C(N) = gamma*N / (1 + sigma*(N-1) + kappa*N*(N-1))`

- useful-throughput USL: gamma=14.17, sigma=0.3434, kappa=0, R2=0.9762
- raw-throughput USL: gamma=35.56, sigma=0.2522, kappa=0, R2=0.9818
- coverage saturation: asymptote=193.0 edges, rho=0.6435, R2=0.8437
- recommended ceiling: **7** (coverage-novelty)

The estimate is workload- and budget-specific. Refit it after changing the target, seed corpus, campaign duration, scheduler, or machine.
