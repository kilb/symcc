# Benchmark 确认性统计实现记录

## 目标

跨运行符号执行的效果不能用单次最好结果宣称。第 6 项新增可复现统计工具，要求 baseline 与 treatment 使用相同 benchmark、相同 seed/run 配对，并报告每次差值，而不是只报告均值。

## 实现

`util/benchmark_statistics.py` 提供 `summarize`、`cliffs_delta` 和 `paired_report`：

- summary：样本数、均值、中位数、极值和固定 seed 的 percentile bootstrap 95% 区间；
- paired report：baseline/treatment 各自 summary、逐 run improved/unchanged/regressed、配对差值区间和 Cliff's delta；
- 所有输入必须为有限数，配对长度必须一致，bootstrap 次数有界且可复现。

该工具不内置任何 benchmark 结果，因此不会在缺少 Lava-M 或公开套件真实测量时制造提升数据。真实实验 runner 应输出每个程序、配置、seed、wall time、branch/edge coverage、solver time 和 corpus size，再由该模块生成报告。

## 验证

新增测试覆盖固定 seed 可重复性、改进/回归计数、NaN 拒绝和配对长度校验。结果 2/2 通过，Ruff 检查通过。

## 当前实验状态

本阶段完成了统计基础设施，但尚未在当前工作区重新运行完整 Lava-M/公开 benchmark 矩阵；因此不能把本次提交描述为已经获得新的 SOTA 提升。后续应接入现有 benchmark runner，至少进行 3 个独立 seed、统一三小时预算、统一 native replay/coverage map，并把原始 JSON 与统计报告一起归档。
