# Parallel scale analysis: gfts-xml_read_fuzzer

| compute workers | concolic | AFL | rounds | total work/s | AFL exec/s | SymCC cand/s | unique/s | retention | edges |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 3 | 1 | 2 | 3 | 59937.47 | 59933.06 | 4.41 | 85.79 | 0.143% | 5355.3 |
| 7 | 3 | 4 | 3 | 154622.47 | 154606.80 | 15.68 | 161.39 | 0.104% | 5478.3 |
| 15 | 7 | 8 | 3 | 290709.79 | 290680.65 | 29.14 | 298.32 | 0.103% | 5533.7 |
| 31 | 12 | 19 | 3 | 606383.15 | 606339.72 | 43.43 | 643.85 | 0.106% | 5615.0 |

## Evidence quality

- attempted=12, successful=12, failed=0, timed-out=0, success-rate=1.000
- decision eligible: **false**
- withholding reasons: 4 allocation(s) have fewer than 3 independent random seeds; 4 allocation(s) reuse a random seed; repeated executions are technical replicates, not independent evidence; only 4 parallelism levels; 5 required; role assignments were inferred for 12 run(s); coverage measurement provenance is missing for 12 row(s); coverage sampling provenance is missing for 12 row(s); planned wall budgets differ by 8.87% across runs; endpoint coverage requires equal exposure; hybrid AFL/SymCC allocation ratio changes across scale levels

## Model

`C(N) = gamma*N / (1 + sigma*(N-1) + kappa*N*(N-1))`

- useful-throughput USL: gamma=21.35, sigma=0.001106, kappa=0, R2=0.9941
- raw-throughput USL withheld: withheld: hybrid total work mixes AFL executions and concolic candidates; use the component fits
- SymCC component USL (concolic-worker axis): R2=0.8809
- AFL component USL (AFL-instance axis): R2=0.9942
- coverage saturation: asymptote=5615.0 edges, rho=0.1233, R2=0.8899
- recommended ceiling: **withheld**; exploratory point estimate=49

The estimate is workload- and budget-specific. Refit it after changing the target, seed corpus, campaign duration, scheduler, or machine.
