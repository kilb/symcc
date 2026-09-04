# F442：并行规模模型、Hybrid Master 瓶颈定位与 Prefix-DAG MDP 有界刷新

> 截止日期：2026-08-20  
> 范围：并行 hybrid benchmark、master/worker profiling、USL/coverage saturation 模型、
> result triage 优化、Adaptive scheduler 内部 profiler、Prefix-DAG/ColorGo MDP bounded
> refresh。  
> 原始证据位于 `benchmark/evidence/parallel-scale-study-2026-08-20/`。

## 1. 研究背景

本阶段的核心问题不是“并行能不能跑起来”，而是：

1. 多少并行 worker 会真正转化为覆盖收益；
2. AFL 与 SymCC 混合运行时，吞吐增长是否被 master 协调成本吃掉；
3. 已引入的 S2F action seed、Prefix DAG、ColorGo/MultiGo/TACO、semantic proposal 等
   SOTA 调度机制在真实 parser 上的热路径在哪里；
4. 能否给后续并行规模决策建立一个可复核的经验模型。

为避免只用 generated/s 误判效果，本阶段同时记录 AFL exec/s、SymCC candidates、
master-accepted SymCC inputs、combined unique rate、edge coverage、coverage AUC、
worker phase timing、master scan/dispatch/recv/triage 和 triage 内部子阶段。

## 2. 新实现

### 2.1 Profile 与规模分析流水线

新增 `benchmark/analyze_parallel_profiles.py`，聚合 `benchmark/run_benchmark.py` 导出的
worker phase CSV、redundancy CSV、master `[PROF]`、master `[PROF-TRIAGE]` 和
benchmark CSV，输出：

- `profile_runs.csv`：每轮完整 profile；
- `profile_summary.csv/json`：按 total np 聚合均值和 95% CI；
- `profile_summary.md`：可直接放入报告的 markdown 表；
- `profile_bottlenecks.svg`：worker/master 阶段堆叠图。

新增 `util/parallel_scale_model.py` 和 `benchmark/analyze_parallel_scaling.py`，实现无 SciPy
依赖的 Universal Scalability Law 拟合和 coverage saturation 拟合。Hybrid 口径分开统计
AFL executions、SymCC candidates 和 combined unique，避免把不同单位混成一个“retention”。

### 2.2 Queue scan 与 profile artifact 修复

修复 `SYMCC_QUEUE_SCAN_MAX` 语义：limit 只计入 unseen queue entries，不计入已经在
`seen` 中的历史文件，防止 AFL queue 变长后尾部新 seed 被永久饿死。

新增 `SYMCC_QUEUE_POLL_INTERVAL`，默认 0.5 秒；只有所有 workers 都忙时才使用该间隔，
空闲 worker 仍触发立即 scan。随后修复 `last_scan_time` 的记录位置，改为扫描完成后记录，
避免一次长扫描耗尽 0.5 秒预算后立即重扫。

`benchmark/run_benchmark.py` 现在会把 `mpi_master.log` 与 `mpi_worker.err` 复制到
`SYMCC_WPROF_DIR`，使 master profile 与 worker profile 一起持久化。

### 2.3 Result triage group commit 与 semantic autosave 延迟

`util/mpi_fuzzing_helper.py` 中 `_batch_triage` 新增本地 group directory commit：

- testcase 文件仍逐个 fsync；
- 没有 distributed coverage claim callback 时，同一 batch 末尾只 fsync 一次 parent dir；
- 有 distributed coverage claim 时保持逐 candidate durable publish，避免跨节点 coverage
  claim 与本地 corpus entry durability 之间出现不一致窗口。

`util/semantic_proposals.py` 新增 `SemanticProposalGenerator(..., autosave=True)` 参数。
MPI master 使用 `autosave=False`，把 proposal state 保存交给周期性 coordinator checkpoint
和 shutdown checkpoint，避免每次 observation 都重写状态文件。

### 2.4 Adaptive scheduler 内部 profiler

`AdaptiveHybridScheduler.observe()` 新增可选 `profile: dict[str, float]` 参数。默认调用无行为
变化；当 master profiling 开启时，`util/mpi_fuzzing_helper.py` 会收集并打印：

- `adaptive.context`
- `adaptive.data_coverage`
- `adaptive.edge_dependence`
- `adaptive.pareto_corpus`
- `adaptive.strategy_model`
- `adaptive.path_cover`
- `adaptive.ect`
- `adaptive.prefix_dag`
- `adaptive.cstg`
- `adaptive.seed_worker`

`benchmark/analyze_parallel_profiles.py` 同步解析这些字段，单测
`test/test_parallel_profile_analysis.py` 覆盖 `adaptive.prefix_dag` 的 CSV 字段生成。

### 2.5 Prefix-DAG / ColorGo MDP bounded refresh

定位结果显示 `adaptive.prefix_dag` 是 `AdaptiveHybridScheduler.observe()` 的主耗时。根因是
Prefix DAG 在每次 ingest 后都执行 `_refresh_mdp_values()`，对所有 prefix nodes 和 transition
groups 做多轮 MDP value iteration。复杂度近似：

```text
O(observations * prefix_nodes * selective_mdp_iterations)
```

![Prefix DAG bounded MDP refresh](../diagrams/parallel-2026-08-20/prefix-mdp-bounded-refresh.svg)

新机制保留 Prefix DAG 和 ColorGo/MultiGo/TACO 的语义，但把全图刷新做成有界：

- 小图（默认 `SYMCC_SELECTIVE_MDP_SMALL_GRAPH_NODES=512`）仍每次刷新；
- 大图默认每 `SYMCC_SELECTIVE_MDP_REFRESH_INTERVAL=8` 次 observation 刷新一次；
- target reached 或 directed-distance 命中强制刷新；
- 本地节点压力、cost、target-path reward、seed path 和 action productivity 每次 ingest 仍即时更新；
- snapshot 与 master `Adaptive` 行记录 `mdp=refresh/skipped@interval`。

新增单测 `test_prefix_dag_mdp_refresh_is_bounded_on_large_graph`：阈值 0、interval 4、连续 7
次 ingest 后断言只刷新 1 次、跳过 6 次。

## 3. 实验结果

### 3.1 Pure MPI synthetic

60 秒、3 轮、`np=2,4,8,16,32`：

| SymCC workers | generated/s | unique/s | worker acceptance | edges |
|---:|---:|---:|---:|---:|
| 1 | 42.90 | 16.21 | 37.8% | 155.0 |
| 3 | 67.60 | 23.80 | 35.2% | 182.3 |
| 7 | 95.28 | 31.36 | 32.9% | 193.0 |
| 15 | 117.88 | 36.47 | 30.9% | 190.3 |
| 31 | 131.19 | 40.04 | 30.5% | 191.3 |

历史结论：吞吐随 workers 增长，该短预算下最高 coverage 观测出现在 7 workers。2026-08-29
使用 `v5` 证据门禁复核后，由于随机种子复用、暴露时间不等、覆盖来源及抽样
provenance 缺失，不能再将其
表述为稳定平台或正式上限。

### 3.2 Google FTS libxml2 hybrid scale

90 秒、3 轮、total `np=4,8,16,32`：

| total np | AFL exec/s | SymCC cand/s | unique/s | edges | AUC |
|---:|---:|---:|---:|---:|---:|
| 4 | 59,933 | 4.41 | 85.79 | 5355.3 | 4899.7 |
| 8 | 154,607 | 15.68 | 161.39 | 5478.3 | 5018.2 |
| 16 | 290,681 | 29.14 | 298.32 | 5533.7 | 5089.5 |
| 32 | 606,340 | 43.43 | 643.85 | 5615.0 | 5147.6 |

worker `exec` 占 accounted worker time 的 84%--90%，`showmap+dedup` 约 3%--5%；bitmap
sync/import/send 不是主要瓶颈。SymCC master accepted 比例从 26.34% 降到 5.33%，说明新增
worker 的主要问题是求解前新颖度下降。

### 3.3 Queue-poll A/B

16 total processes、7 SymCC workers、90 秒、3+3 轮：

| 指标 | 100 ms baseline | 500 ms optimized | 变化 |
|---|---:|---:|---:|
| master scan time | 30.19 s | 19.47 s | -35.49% |
| scan calls | 521.7 | 143.3 | -72.52% |
| worker busy | 85.86% | 87.54% | +1.69 pp |
| edges | 5533.7 | 5553.3 | 区间重叠 |
| AUC | 5089.5 | 5093.0 | 区间重叠 |

结论：master scan 开销确定下降，未观察到覆盖回归；覆盖提升不能按该 A/B 宣称显著。

### 3.4 长跑与 MDP bounded refresh

180 秒未限频 reference `gfts_hybrid_triacetail_np16_180s_r1`：

| 指标 | 数值 |
|---|---:|
| wall time | 184.33 s |
| master scan | 12.43 s |
| master dispatch | 32.65 s |
| master triage | 78.73 s |
| `adaptive_scheduler` | 59.37 s |
| `semantic_proposals` | 9.53 s |
| edges / AUC | 5690 / 5306.5 |

150 秒内部定位 `gfts_hybrid_adaptive_detail_np16_150s_r1`：

| 子阶段 | 时间 |
|---|---:|
| `adaptive_scheduler` | 2.29 s |
| `adaptive.prefix_dag` | 2.20 s |
| `adaptive.ect` | 0.04 s |
| `adaptive.edge_dependence` | 0.02 s |

150 秒 bounded refresh `gfts_hybrid_mdpbounded_np16_150s_r1`：

| 指标 | 数值 |
|---|---:|
| profile observation scale | 1133 observations |
| master scan | 12.60 s |
| master triage | 33.78 s |
| `adaptive_scheduler` | 14.28 s |
| `adaptive.prefix_dag` | 12.81 s |
| worker busy | 68.89% |
| SymCC generated | 7599 |
| edges / AUC | 5663 / 5295.9 |

与 180 秒未限频 reference 在相近 observation 规模比较，`adaptive_scheduler` 下降约 75.9%，
master triage 下降约 57.1%。两者不是等时多轮 A/B，因此 coverage 只能说“单轮未见结构性
回退”，不能说 bounded refresh 已显著提升覆盖。

## 4. 模型结论

并行规模上限采用：

```text
N* = min(N_resource, N_master, N_USL, N_novelty)
```

对 master-accepted SymCC inputs/s 拟合 USL：

```text
gamma = 0.8311
sigma = 0.3026
kappa = 0
R2 = 0.7231
10% doubling-gain ceiling = 11 SymCC workers
```

后续 F458/F461 正确性审查继续收紧了该分析器的使用边界：正式决策至少需要五个不同
并行度、每档三个独立随机种子、完整配对区组、等暴露时间、显式角色账本、固定 hybrid
分配比例、可信覆盖分母，并要求 USL 与 coverage saturation 拟合有效。hybrid 总吞吐不得
混加 AFL executions 与 SymCC candidates。
USL 来自既有扩展性模型；指数覆盖饱和与四类 ceiling 取最小值是项目的经验模型和保守决策
规则，不是文献定理。上面的 11 由五个实测 SymCC worker 规模点拟合，仍只是探针建议，不是
生产上限或可迁移到其他目标的结论。

历史 XML hybrid endpoint coverage saturation 拟合给出约 48/50 total processes；`v5`
复核结果为 `decision_eligible=false`、正式上限未知。8--12 和 48/50 只能作为后续实验探针，
不能作为当前工程默认值的证据；运行时仍可由 novelty-aware allocation 根据 live accepted
rate 动态停泊或恢复符号执行 worker。

## 5. 正确性与回归

已执行验证：

- `python3 -m ruff check util/hybrid_feedback.py util/mpi_fuzzing_helper.py benchmark/analyze_parallel_profiles.py ...`
- `python3 -m py_compile util/hybrid_feedback.py util/mpi_fuzzing_helper.py benchmark/analyze_parallel_profiles.py ...`
- `PYTHONPATH=util python3 -m pytest -q test/test_hybrid_feedback.py -k 'prefix_dag'`
- `PYTHONPATH=util python3 -m pytest -q test/test_parallel_profile_analysis.py`
- 150 秒 GFTS XML hybrid profile run：`gfts_hybrid_adaptive_detail_np16_150s_r1`
- 150 秒 GFTS XML bounded MDP run：`gfts_hybrid_mdpbounded_np16_150s_r1`

需要补充的论文级验证：

- `SYMCC_SELECTIVE_MDP_REFRESH_INTERVAL=4,8,16,32` 的 3--5 轮消融；
- 150/180/300 秒等时多轮对照；
- 增加 AFL-only equal-core control，避免把 hybrid 总扩展误读成 SymCC 因果增益；
- 完整 corpus coverage oracle，降低 20,000 inputs 抽样对大规模 queue 的偏差。

## 6. 创新性与挑战

本阶段贡献不在于单独实现一个启发式，而在于把并行 hybrid 系统的“覆盖收益、协调成本、
状态更新复杂度”放进同一套可测量框架：

- 用 USL 与 coverage saturation 同时约束并行规模，避免只看吞吐或只看 endpoint coverage；
- 把 master triage 从黑箱拆成可 profile 的 callback、proposal、adaptive 子阶段；
- 在保留 Prefix DAG / S2F / ColorGo/MultiGo/TACO 目标导向能力的前提下，降低全图 MDP
  刷新的渐进复杂度；
- 通过 snapshot 与 master log 暴露刷新/跳过计数，使调度器优化可以被实验复核。

后续最有研究价值的方向是把 bounded MDP refresh 与 novelty-aware worker allocation 合并：
当 coverage slope 与 accepted SymCC rate 同时下降时，调度器应减少 expensive global planning
和 SymCC active workers；当 Prefix DAG 出现高价值 frontier 或 directed target 时，再短时提升
刷新频率与 worker 数量。这比固定 worker cap 更接近自适应并行符号执行系统。
