# Current Evaluation Statistical Summary

All rows retain the original campaign result. Location is the median; intervals are percentile bootstrap intervals. Because the campaign did not use randomized blocks, comparisons treat runs as independent samples and use a two-sided label-permutation test with Holm correction.

| configuration | target | metric | n | mean | median | median 95% CI | sd | range |
|---|---|---|---:|---:|---:|---:|---:|---:|
| current/afl-only | libarchive-archive_fuzzer | edge_cov_pct | 20 | 15.072 | 15.085 | [14.905, 15.330] | 0.457 | [14.140, 16.110] |
| current/afl-only | sqlite-sqlite_fuzzer | edge_cov_pct | 20 | 18.781 | 18.985 | [18.525, 19.300] | 0.926 | [16.780, 19.970] |
| current/hybrid | libarchive-archive_fuzzer | edge_cov_pct | 20 | 16.460 | 16.420 | [16.195, 16.760] | 0.567 | [15.310, 17.480] |
| current/hybrid | sqlite-sqlite_fuzzer | edge_cov_pct | 20 | 20.392 | 20.305 | [19.840, 20.950] | 0.736 | [19.350, 21.670] |
| legacy/afl-only | libarchive-archive_fuzzer | edge_cov_pct | 20 | 15.038 | 15.065 | [14.925, 15.110] | 0.699 | [13.850, 16.890] |
| legacy/afl-only | sqlite-sqlite_fuzzer | edge_cov_pct | 20 | 18.437 | 18.590 | [17.545, 19.345] | 1.029 | [17.070, 19.950] |
| legacy/hybrid | libarchive-archive_fuzzer | edge_cov_pct | 20 | 15.508 | 15.645 | [15.105, 15.855] | 0.644 | [14.080, 16.810] |
| legacy/hybrid | sqlite-sqlite_fuzzer | edge_cov_pct | 20 | 18.443 | 18.530 | [17.960, 19.005] | 0.870 | [16.820, 19.670] |
| current/afl-only | libarchive-archive_fuzzer | afl_bitmap_cvg | 20 | 15.091 | 15.105 | [14.925, 15.350] | 0.457 | [14.160, 16.130] |
| current/afl-only | sqlite-sqlite_fuzzer | afl_bitmap_cvg | 20 | 18.868 | 19.100 | [18.615, 19.375] | 0.921 | [16.900, 20.090] |
| current/hybrid | libarchive-archive_fuzzer | afl_bitmap_cvg | 20 | 15.354 | 15.355 | [15.200, 15.590] | 0.334 | [14.640, 15.840] |
| current/hybrid | sqlite-sqlite_fuzzer | afl_bitmap_cvg | 20 | 19.379 | 19.295 | [18.880, 19.735] | 0.764 | [18.390, 21.270] |
| legacy/afl-only | libarchive-archive_fuzzer | afl_bitmap_cvg | 20 | 15.057 | 15.080 | [14.945, 15.125] | 0.699 | [13.870, 16.910] |
| legacy/afl-only | sqlite-sqlite_fuzzer | afl_bitmap_cvg | 20 | 18.517 | 18.670 | [17.650, 19.400] | 1.017 | [17.140, 20.020] |
| legacy/hybrid | libarchive-archive_fuzzer | afl_bitmap_cvg | 20 | 15.128 | 15.215 | [14.875, 15.335] | 0.431 | [14.100, 16.000] |
| legacy/hybrid | sqlite-sqlite_fuzzer | afl_bitmap_cvg | 20 | 18.524 | 18.600 | [18.040, 19.100] | 0.863 | [16.920, 19.710] |
| current/afl-only | libarchive-archive_fuzzer | generated | 20 | 1427.100 | 1435.000 | [1388.000, 1497.500] | 114.097 | [1161.000, 1570.000] |
| current/afl-only | sqlite-sqlite_fuzzer | generated | 20 | 1418.850 | 1506.000 | [1364.000, 1598.000] | 305.602 | [773.000, 1785.000] |
| current/hybrid | libarchive-archive_fuzzer | generated | 20 | 4979.600 | 4976.500 | [4790.000, 5157.000] | 290.392 | [4458.000, 5546.000] |
| current/hybrid | sqlite-sqlite_fuzzer | generated | 20 | 5123.700 | 5156.500 | [4825.000, 5440.500] | 461.512 | [4275.000, 5826.000] |
| legacy/afl-only | libarchive-archive_fuzzer | generated | 20 | 1421.850 | 1479.000 | [1373.000, 1500.500] | 132.380 | [1149.000, 1605.000] |
| legacy/afl-only | sqlite-sqlite_fuzzer | generated | 20 | 1338.250 | 1338.000 | [1039.000, 1629.500] | 330.532 | [923.000, 1846.000] |
| legacy/hybrid | libarchive-archive_fuzzer | generated | 20 | 1618.600 | 1672.000 | [1527.000, 1726.000] | 183.039 | [1168.000, 1906.000] |
| legacy/hybrid | sqlite-sqlite_fuzzer | generated | 20 | 1452.200 | 1540.000 | [1271.000, 1687.500] | 332.603 | [815.000, 1947.000] |
| current/afl-only | libarchive-archive_fuzzer | afl_execs_done | 20 | 1090130.250 | 1091170.500 | [996727.500, 1226179.000] | 223731.479 | [622264.000, 1467756.000] |
| current/afl-only | sqlite-sqlite_fuzzer | afl_execs_done | 20 | 737673.800 | 807948.500 | [681339.000, 910388.000] | 253559.571 | [231065.000, 1028313.000] |
| current/hybrid | libarchive-archive_fuzzer | afl_execs_done | 20 | 4303479.100 | 4291861.000 | [3954406.500, 4675421.000] | 533563.060 | [3484379.000, 5370985.000] |
| current/hybrid | sqlite-sqlite_fuzzer | afl_execs_done | 20 | 3451601.900 | 3353240.000 | [3185684.500, 3723436.000] | 385408.527 | [2807797.000, 4158497.000] |
| legacy/afl-only | libarchive-archive_fuzzer | afl_execs_done | 20 | 1047686.200 | 1042032.500 | [843292.500, 1252775.500] | 270414.396 | [642888.000, 1448375.000] |
| legacy/afl-only | sqlite-sqlite_fuzzer | afl_execs_done | 20 | 676999.850 | 638434.500 | [431710.000, 917990.500] | 274139.913 | [339445.000, 1096910.000] |
| legacy/hybrid | libarchive-archive_fuzzer | afl_execs_done | 20 | 1087865.250 | 1068154.000 | [990827.500, 1221374.500] | 181082.866 | [752687.000, 1357583.000] |
| legacy/hybrid | sqlite-sqlite_fuzzer | afl_execs_done | 20 | 661319.700 | 651465.000 | [456292.000, 816258.500] | 246392.735 | [238198.000, 1035820.000] |

## Independent-sample comparisons

| treatment vs baseline | target | metric | n / n | mean delta | delta 95% CI | mean change | A12 | Cliff delta | Holm p |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| current/hybrid vs legacy/hybrid | libarchive-archive_fuzzer | edge_cov_pct | 20 / 20 | +0.953 | [+0.582, +1.321] | +6.1% | 0.871 | +0.742 | 0.00168 |
| current/hybrid vs legacy/hybrid | sqlite-sqlite_fuzzer | edge_cov_pct | 20 / 20 | +1.949 | [+1.466, +2.441] | +10.6% | 0.961 | +0.923 | 0.00032 |
| current/hybrid vs legacy/hybrid | libarchive-archive_fuzzer | afl_bitmap_cvg | 20 / 20 | +0.226 | [+0.002, +0.463] | +1.5% | 0.665 | +0.330 | 1 |
| current/hybrid vs legacy/hybrid | sqlite-sqlite_fuzzer | afl_bitmap_cvg | 20 / 20 | +0.855 | [+0.370, +1.362] | +4.6% | 0.752 | +0.505 | 0.04009 |
| current/hybrid vs legacy/hybrid | libarchive-archive_fuzzer | generated | 20 / 20 | +3361.000 | [+3214.100, +3507.550] | +207.6% | 1.000 | +1.000 | 0.00032 |
| current/hybrid vs legacy/hybrid | sqlite-sqlite_fuzzer | generated | 20 / 20 | +3671.500 | [+3430.950, +3917.500] | +252.8% | 1.000 | +1.000 | 0.00032 |
| current/hybrid vs legacy/hybrid | libarchive-archive_fuzzer | afl_execs_done | 20 / 20 | +3215613.850 | [+2977736.700, +3456973.700] | +295.6% | 1.000 | +1.000 | 0.00032 |
| current/hybrid vs legacy/hybrid | sqlite-sqlite_fuzzer | afl_execs_done | 20 / 20 | +2790282.200 | [+2596256.150, +2989190.450] | +421.9% | 1.000 | +1.000 | 0.00032 |
| legacy/hybrid vs legacy/afl-only | libarchive-archive_fuzzer | edge_cov_pct | 20 / 20 | +0.470 | [+0.057, +0.870] | +3.1% | 0.706 | +0.413 | 0.6057 |
| legacy/hybrid vs legacy/afl-only | sqlite-sqlite_fuzzer | edge_cov_pct | 20 / 20 | +0.006 | [-0.576, +0.574] | +0.0% | 0.506 | +0.012 | 1 |
| legacy/hybrid vs legacy/afl-only | libarchive-archive_fuzzer | afl_bitmap_cvg | 20 / 20 | +0.071 | [-0.287, +0.417] | +0.5% | 0.578 | +0.155 | 1 |
| legacy/hybrid vs legacy/afl-only | sqlite-sqlite_fuzzer | afl_bitmap_cvg | 20 / 20 | +0.008 | [-0.569, +0.570] | +0.0% | 0.509 | +0.018 | 1 |
| legacy/hybrid vs legacy/afl-only | libarchive-archive_fuzzer | generated | 20 / 20 | +196.750 | [+97.250, +293.700] | +13.8% | 0.825 | +0.650 | 0.0078 |
| legacy/hybrid vs legacy/afl-only | sqlite-sqlite_fuzzer | generated | 20 / 20 | +113.950 | [-90.000, +311.450] | +8.5% | 0.598 | +0.195 | 1 |
| legacy/hybrid vs legacy/afl-only | libarchive-archive_fuzzer | afl_execs_done | 20 / 20 | +40179.050 | [-97013.150, +180441.350] | +3.8% | 0.542 | +0.085 | 1 |
| legacy/hybrid vs legacy/afl-only | sqlite-sqlite_fuzzer | afl_execs_done | 20 / 20 | -15680.150 | [-171716.450, +139163.150] | -2.3% | 0.500 | +0.000 | 1 |
| current/hybrid vs current/afl-only | libarchive-archive_fuzzer | edge_cov_pct | 20 / 20 | +1.389 | [+1.083, +1.700] | +9.2% | 0.976 | +0.952 | 0.00032 |
| current/hybrid vs current/afl-only | sqlite-sqlite_fuzzer | edge_cov_pct | 20 / 20 | +1.611 | [+1.117, +2.130] | +8.6% | 0.932 | +0.865 | 0.00032 |
| current/hybrid vs current/afl-only | libarchive-archive_fuzzer | afl_bitmap_cvg | 20 / 20 | +0.263 | [+0.025, +0.508] | +1.7% | 0.693 | +0.385 | 0.772 |
| current/hybrid vs current/afl-only | sqlite-sqlite_fuzzer | afl_bitmap_cvg | 20 / 20 | +0.511 | [+0.018, +1.043] | +2.7% | 0.619 | +0.238 | 1 |
| current/hybrid vs current/afl-only | libarchive-archive_fuzzer | generated | 20 / 20 | +3552.500 | [+3417.150, +3685.300] | +248.9% | 1.000 | +1.000 | 0.00032 |
| current/hybrid vs current/afl-only | sqlite-sqlite_fuzzer | generated | 20 / 20 | +3704.850 | [+3470.450, +3939.500] | +261.1% | 1.000 | +1.000 | 0.00032 |
| current/hybrid vs current/afl-only | libarchive-archive_fuzzer | afl_execs_done | 20 / 20 | +3213348.850 | [+2968168.700, +3460414.150] | +294.8% | 1.000 | +1.000 | 0.00032 |
| current/hybrid vs current/afl-only | sqlite-sqlite_fuzzer | afl_execs_done | 20 / 20 | +2713928.100 | [+2520253.500, +2911401.950] | +367.9% | 1.000 | +1.000 | 0.00032 |
| current/afl-only vs legacy/afl-only | libarchive-archive_fuzzer | edge_cov_pct | 20 / 20 | +0.034 | [-0.333, +0.389] | +0.2% | 0.537 | +0.075 | 1 |
| current/afl-only vs legacy/afl-only | sqlite-sqlite_fuzzer | edge_cov_pct | 20 / 20 | +0.344 | [-0.257, +0.918] | +1.9% | 0.574 | +0.147 | 1 |
| current/afl-only vs legacy/afl-only | libarchive-archive_fuzzer | afl_bitmap_cvg | 20 / 20 | +0.034 | [-0.331, +0.390] | +0.2% | 0.537 | +0.075 | 1 |
| current/afl-only vs legacy/afl-only | sqlite-sqlite_fuzzer | afl_bitmap_cvg | 20 / 20 | +0.351 | [-0.246, +0.919] | +1.9% | 0.575 | +0.150 | 1 |
| current/afl-only vs legacy/afl-only | libarchive-archive_fuzzer | generated | 20 / 20 | +5.250 | [-68.750, +81.400] | +0.4% | 0.480 | -0.040 | 1 |
| current/afl-only vs legacy/afl-only | sqlite-sqlite_fuzzer | generated | 20 / 20 | +80.600 | [-115.650, +264.850] | +6.0% | 0.545 | +0.090 | 1 |
| current/afl-only vs legacy/afl-only | libarchive-archive_fuzzer | afl_execs_done | 20 / 20 | +42444.050 | [-108565.550, +189292.950] | +4.1% | 0.537 | +0.075 | 1 |
| current/afl-only vs legacy/afl-only | sqlite-sqlite_fuzzer | afl_execs_done | 20 / 20 | +60673.950 | [-101241.350, +216236.250] | +9.0% | 0.535 | +0.070 | 1 |
