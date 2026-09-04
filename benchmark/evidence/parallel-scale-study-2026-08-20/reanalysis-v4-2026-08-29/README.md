# Parallel-scale evidence reanalysis (v4)

This directory preserves a 2026-08-29 audit of the 2026-08-20 scaling data.
The original CSV and v1 model outputs remain unchanged in their historical
directories.

## Result

Neither historical campaign is eligible to produce a resource-allocation
decision under `symcc-parallel-scale-analysis-v4`.

| Campaign | Historical result | v4 formal result | Exploratory output only |
|---|---:|---:|---:|
| Synthetic MPI, 60 s | 7 workers | unavailable | USL probe: 9 workers |
| libxml2 hybrid, 90 s | 50 processes | unavailable | coverage probe: 49 processes |

The synthetic campaign reuses random seed 0 at all three repeats, infers all
role ledgers, varies planned wall exposure by 1.90%, lacks v4 coverage
provenance, and has non-monotone endpoint coverage. The hybrid campaign also
reuses seed 0, contains only four scale levels, changes the AFL/SymCC allocation
ratio, varies exposure by 8.87%, and lacks coverage provenance. Its seed rows
report inconsistent baselines, so the audit input contains only hybrid rows and
declares the historical 2320/50880 baseline explicitly.

`ceiling.recommended_parallelism` is therefore `null` in both v4 JSON files.
The exploratory values are retained only to choose future measurement points;
they are not deployment limits or measured optima.

## Evidence

- `synthetic/parallel_scale_model.json`: direct v4 analysis of the original
  synthetic CSV.
- `hybrid/audit_input_without_seed_rows.csv`: deterministic union of the 12
  historical hybrid rows; raw source files were not modified.
- `hybrid/model/parallel_scale_model.json`: v4 hybrid analysis.

Future decision-grade runs must use at least five scale levels, at least three
independent random seeds per level, complete paired seed blocks, equal planned
wall exposure, explicit role ledgers, a fixed hybrid allocation ray, and an
`existing_edges` coverage denominator recorded by the benchmark.
