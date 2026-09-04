# SymCC-Parallel 文档索引

本目录按“使用、实现、研究、实验、历史”五类组织。新增功能应先更新权威文档，
避免同一状态在多份阶段报告中产生冲突。

## 权威入口

| 主题 | 文档 | 用途 |
| --- | --- | --- |
| 新增实现总账 | [`New_Implementation_Archive.md`](New_Implementation_Archive.md) | 方案、代码、测试、结果和局限 |
| 当前技术全景 | [`Current_Technology_Compendium.md`](Current_Technology_Compendium.md) | 按执行链通俗解释 F00-F296，并附无遗漏自动索引 |
| 开发历史追踪 | [`Development_History_Traceability.md`](Development_History_Traceability.md) | 139 个提交、开发阶段、F00-F296 与证据映射 |
| 完整配置 | [`Configuration.txt`](Configuration.txt) | 环境变量、默认值、输入输出 |
| SOTA 调研 | [`sota_hybrid_execution_2026.md`](sota_hybrid_execution_2026.md) | 文献依据、架构映射、未决差距 |
| 严格 SOTA 差距审计 | [`SOTA_Gap_Audit_2026-07-24.md`](SOTA_Gap_Audit_2026-07-24.md) | 论文语义与当前代码逐项核对、P0/P1/P2 实施顺序 |
| 研究进展归档 | [`research-progress/`](research-progress/) | 阶段性总结、技术方案、实现记录、实验结果与后续计划 |
| 近期工作与计划 | [`Recent_Work_and_Next_Research_Plan.md`](Recent_Work_and_Next_Research_Plan.md) | 阶段总结、研究问题和优先级 |
| 项目汇报 | [`Project_Report_2026-07.md`](Project_Report_2026-07.md) | 架构、关键技术原理、SOTA 落地边界与实验结论；两套图集共 46 张可复现配图 |
| 项目进展演示稿 | [`Project_Progress_Presentation_2026-07-30.md`](Project_Progress_Presentation_2026-07-30.md) / [`PPTX`](Project_Progress_Presentation_2026-07-30.pptx) | 28 页 16:9 汇报稿，27 页含讲者备注；覆盖架构、执行流、关键技术、SOTA 映射和当前实验 |
| 当前实测评估 | [`Current_Implementation_Evaluation_2026-07-30.md`](Current_Implementation_Evaluation_2026-07-30.md) | SQLite/libarchive 20 轮配置消融、300 s 描述性复测、LAVA-M base64 13-seed solver ablation 与 evidence 链接 |
| 正确性审查修复 | [`Correctness_Review_Fixes_2026-07.md`](Correctness_Review_Fixes_2026-07.md) | 求解、运行时、稳定 ID、fencing 的错误模型与验证 |
| MPI 架构 | [`MPI_Parallelization.txt`](MPI_Parallelization.txt) | MPI master/worker 基础设计 |
| Benchmark 使用 | [`../benchmark/README.md`](../benchmark/README.md) | 构建、运行、产物和分析 |

## 专题文档

- [`SymCC_Technical_Deep_Dive.md`](SymCC_Technical_Deep_Dive.md)：核心编译与执行机制；
- [`Current_Technology_Compendium.md`](Current_Technology_Compendium.md)：当前新增
  技术的全景说明，覆盖体系结构、执行次序、可信边界、成熟度、配置导航及
  F00-F296 完整索引；索引由
  [`generate_current_technology_index.py`](generate_current_technology_index.py)
  从权威档案生成并检查连续性；
- [`Parallel_Architecture_Report.md`](Parallel_Architecture_Report.md)：并行架构分析；
- [`Parallel_Architecture_v2.md`](Parallel_Architecture_v2.md)：worker triage、协议和
  扩展优化的历史定量记录；
- [`engine_abstraction.md`](engine_abstraction.md)：执行引擎抽象；
- [`symsan_ported_techniques.md`](symsan_ported_techniques.md)：SymSan 技术迁移；
- [`symsan_hybrid_benchmark_6targets.md`](symsan_hybrid_benchmark_6targets.md)：
  SymSan 六目标三轮 hybrid 结果；
- [`SymSan_工作总结_PPT补充材料.md`](SymSan_工作总结_PPT补充材料.md)：
  双引擎阶段总结与汇报材料；
- [`Architecture_QA3.md`](Architecture_QA3.md)：并行框架执行次序与位图、约束形态与
  编译方式、求解策略占比、种子影响、ICSE'23（CoFuzz）复现、基准与 LAVA-M 的
  逐问解答；含为该文当场补测的实测数据（策略占比、端到端漏斗、嵌套深度对照、
  持久模式 A/B、真实 base64 DSE 轨迹）与复现脚本。专项实测属于单次机制短测；
  生产策略占比和 LAVA time-to-bug 仍需等 CPU、多轮确认；
- [`Research_and_Optimization_Report.md`](Research_and_Optimization_Report.md)：
  GRIMOIRE、LAF/NGRAM、honggfuzz 和异构协同的历史实验；
- [`Experiments.txt`](Experiments.txt)：实验背景与原始方法；
- [`Testing.txt`](Testing.txt)：上游测试说明。

`Work_Progress_Report*`、`Final_Work_Report.md`、`PPT_Material*` 等属于历史阶段材料。
它们可用于追溯，但不作为当前功能状态的权威来源。历史报告中的定量结果按 B 级
证据引用，不能在缺少等 CPU、多轮统计和消融时升级为 R 级结论。

## 新功能文档门槛

每个新增功能在完成前必须：

1. 更新 [`New_Implementation_Archive.md`](New_Implementation_Archive.md)，记录研究
   问题、方案、实现路径、测试证据、结果等级和已知局限；
2. 更新
   [`Development_History_Traceability.md`](Development_History_Traceability.md)，
   把提交区间映射到功能 ID；
3. 在 [`Configuration.txt`](Configuration.txt) 记录新增配置、默认行为与产物；
4. 增加自动化测试并在技术档案中给出测试文件；
5. 若改变 benchmark 字段或运行方式，更新
   [`../benchmark/README.md`](../benchmark/README.md)；
6. 若引入新的论文方法或改变 SOTA 判断，更新
   [`sota_hybrid_execution_2026.md`](sota_hybrid_execution_2026.md)。

统一记录模板位于
[`New_Implementation_Archive.md`](New_Implementation_Archive.md#20-后续新增功能的强制记录模板)。
