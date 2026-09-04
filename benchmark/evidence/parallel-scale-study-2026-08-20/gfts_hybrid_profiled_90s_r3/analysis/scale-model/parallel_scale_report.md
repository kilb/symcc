# Parallel scale analysis: gfts-xml_read_fuzzer

| parallelism | rounds | total work/s | AFL exec/s | SymCC cand/s | unique/s | retention | edges |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 3 | 59937.47 | 59933.06 | 4.41 | 85.79 | 0.143% | 5355.3 |
| 8 | 3 | 154622.47 | 154606.80 | 15.68 | 161.39 | 0.104% | 5478.3 |
| 16 | 3 | 290709.79 | 290680.65 | 29.14 | 298.32 | 0.103% | 5533.7 |
| 32 | 3 | 606383.15 | 606339.72 | 43.43 | 643.85 | 0.106% | 5615.0 |

## Model

`C(N) = gamma*N / (1 + sigma*(N-1) + kappa*N*(N-1))`

- useful-throughput USL: gamma=19.86, sigma=0, kappa=0, R2=0.9966
- raw-throughput USL: gamma=1.877e+04, sigma=0, kappa=0, R2=0.9935
- coverage saturation: asymptote=5615.0 edges, rho=0.1233, R2=0.8899
- recommended ceiling: **50** (coverage-novelty)

The estimate is workload- and budget-specific. Refit it after changing the target, seed corpus, campaign duration, scheduler, or machine.
