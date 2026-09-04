# 研究进展

本目录用于集中存放项目研究进展材料，包括阶段性总结、技术方案、实现记录、实验结果和后续研究计划。

## 条目

- [2026-08-12 Agolic 式跨运行规划器](2026-08-12-agolic-run-level-planning.md)：跨运行目标规划、回放证据、witness 模式、持久化状态与测试结果。
- [2026-08-12 Live Symbolic-State Search Strategy](2026-08-12-live-state-search-strategy.md)：活动符号状态的多目标、UCB 和 worker 亲和调度。
- [2026-08-12 Verifier-in-the-loop](2026-08-12-verifier-in-the-loop.md)：候选生成、具体回放、证书验证和 quorum admission。
- [2026-08-12 Cross-theory Selective Concolic](2026-08-12-cross-theory-selective-concolic.md)：跨理论依赖闭包、concrete 固定和 full-query 回退。
- [2026-08-12 UCSan/POSE Heap Path Optimality](2026-08-12-heap-path-optimality.md)：对象路径、alias、目标距离和内存成本的 frontier 优化。
- [2026-08-12 Benchmark 确认性统计](2026-08-12-benchmark-confirmatory-statistics.md)：配对 bootstrap、Cliff's delta 和可复现实验报告。

新增进展时可复制 [`TEMPLATE.md`](TEMPLATE.md) 作为报告骨架。目录内的文档用于建立统一入口；Codex 产生的详细记录继续存放在 `../codex/research-progress/`，由本目录索引，避免不同开发工具直接修改同一文件。

## 内容要求

每份研究进展文档应尽量包含以下内容：

1. 研究背景与目标；
2. 技术原理及相关工作；
3. 架构设计与实现细节；
4. 正确性验证与测试方法；
5. 实验配置、原始数据和结果分析；
6. 已知限制、风险与后续计划；
7. 对应代码、测试、图表和证据文件的路径。

## 命名约定

建议使用以下格式：

```text
<主题>_<功能编号>_<YYYY-MM-DD>.md
```

例如：

```text
Cross_Coordinator_Target_Fencing_F311_2026-08-07.md
```

实验数据和图片应分别保存在相应的证据目录与图表目录中，并在文档内使用相对路径引用。尚未经过真实实验验证的结论必须明确标注为设计预期或待验证事项，不应表述为已经取得的性能提升。

## 现有材料

Codex 独立维护的研究进展位于 [`../codex/research-progress/`](../codex/research-progress/)，继续保留在该隔离目录中，避免与其他开发工具维护的文档发生覆盖冲突。

最新归档为 [`F384：Shared PHI Edge Discriminator`](../codex/research-progress/Shared_PHI_Edge_Discriminator_F384_2026-08-13.md)。该实现把同一PHI block中多个pointer PHI隐含的共同predecessor关系编码为共享edge tag与可执行合同，使F383恢复端能独立复核PHI-correlated multi-cell图。大型回归发现并修复function-pointer capability依赖，reordered fixture证明实现不依赖各PHI incoming列表顺序；LLVM17/18 artifact、完整241+1 lit及888+229 Python门禁通过。该能力只覆盖有界同block predecessor关系，不等价于一般path relation、heap graph或points-to analysis，也不提供公开benchmark覆盖或性能提升结论。
