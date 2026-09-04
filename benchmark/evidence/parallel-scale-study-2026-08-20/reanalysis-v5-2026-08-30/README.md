# Parallel-scale evidence reanalysis (v5)

This directory preserves the 2026-08-30 reanalysis of the historical
2026-08-20 campaigns. The v5 gate adds explicit corpus-sampling provenance to
the existing coverage-denominator, allocation-ledger, independent-seed,
equal-exposure, and paired-block requirements.

## Result

Neither campaign is decision-grade under
`symcc-parallel-scale-analysis-v5`.

| Campaign | Historical claim | v5 formal ceiling | Exploratory probe |
|---|---:|---:|---:|
| Synthetic MPI, 60 s | 7 workers | unavailable | 9 workers |
| libxml2 hybrid, 90 s | 50 processes | unavailable | 49 processes |

The synthetic campaign reuses seed 0, infers all role ledgers, varies planned
wall exposure by 1.90%, has non-monotone endpoint coverage, and lacks both
existing-edge and corpus-sampling provenance. The hybrid campaign additionally
has only four scale levels, changes its AFL/SymCC allocation ray, varies
exposure by 8.87%, and lacks both coverage provenance fields. Its inconsistent
historical seed rows remain excluded; the audit declares the historical
2320/50880 baseline explicitly.

Both JSON files therefore set `ceiling.recommended_parallelism` to `null`.
The exploratory probes are useful only for selecting future measurement
points.

Future decision-grade campaigns must record complete, unsampled endpoint
coverage with an `existing_edges` denominator, at least five scale levels,
three or more independent paired seeds per level, equal planned exposure,
explicit role ledgers, and a fixed hybrid allocation ray.
