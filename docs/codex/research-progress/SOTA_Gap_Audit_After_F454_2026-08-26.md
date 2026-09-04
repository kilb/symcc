# F454 后 SOTA 缺口审计

## 1. 审计结论

F454 已关闭“只有 PalRUP consumer、没有 solver-native producer”的缺口：多 rank CaDiCaL 原生证明、实时
短子句共享、官方全局确认和耐久发布均已落地。当前仍不能宣称“全部 SOTA 已完成”。剩余工作应区分
可工程验收的功能开发与必须依赖长时间公共实验的研究验证，避免用合成机制测试代替效果结论。

## 2. 按顺序的剩余项目

| 顺序 | 项目 | 当前缺口 | 建议验收条件 | 优先级 |
| ---: | --- | --- | --- | --- |
| 1 | F455 结构化 LLM concolic 闭环 | 已有 agent 接口，但缺少带 schema、预算、回放、失败回退和离线评价的完整决策闭环 | 固定模型/提示身份；结构化 action；预算与超时；确定性 fallback；shadow/ablation oracle；不进入求解正确性根 | P1 |
| 2 | F456 POSE 风格 heap 语义 | under-constrained/object 机制已存在，但指针与 heap object 的路径爆炸仍缺少 POSE 类按需符号化 | 对象身份与 lazy field materialization；alias 一致性；边界/释放语义；与现有 UCSan/UBI 联测；公开 heap case | P1 |
| 3 | R-track 公开长期实验 | 现有证据主要证明机制、合同和短时趋势，不能回答覆盖率、缺陷发现和多节点扩展收益 | 等 CPU 预算；AFL-only/混合/消融；至少多 seed；6h/24h；coverage AUC、time-to-edge、proof overhead、置信区间 | P0 研究验证 |

## 3. F455 的边界

LLM 只应提供候选 action，例如目标选择、约束切片、输入结构假设或调度优先级。所有 action 必须先通过
严格 schema、可用能力、预算和安全的语义检查，再交给现有 deterministic executor。模型输出不能直接
声明 SAT/UNSAT、不能绕过 Z3/PalRUP/model replay，也不能修改已发布 evidence。离线 replay 与 shadow
mode 应先于在线执行，用同一 task set 比较接受率、有效覆盖收益、延迟和 token 成本。

## 4. F456 的边界

POSE 类机制的目标是避免在函数入口一次性枚举复杂 heap 图。字段在第一次读取时按需创建符号值或对象
引用，alias 决策保持路径一致，并把对象生命周期、边界和初始化状态接入已有 memory semantics。该项
不是简单增加“符号指针”开关；若对象身份、释放和跨字段 alias 合同不完整，会产生不可重放的伪路径。

## 5. 公开实验为什么仍是未完成项

F454 的 1/2/4 rank 实验确认了子句共享和全局证明链路，但使用的是 72-variable 合成 UNSAT 公式，且
端到端时间主要由 proof checking 主导。它不能支持“并行更快”或“fuzzing 覆盖率更高”的结论。公开
实验必须把 solver、proof、调度、fuzzer 执行分别计时，并以等 CPU-hours 比较；否则更多 worker 自然
获得更多资源，无法区分算法收益和算力收益。

## 6. 不再列为功能缺口的项目

- F454 solver-native PalRUP producer：已实现并由官方 checker 复核；
- F453 online activity/cost cubing：闭环已实现，但公开效果仍归 R-track；
- F452 native checker compression、F451 certified partition recovery：工程验收已完成；
- “增加更多启发式名称”不构成独立 SOTA，除非有明确语义、可信边界、消融设计和可复现实验。

审计日期：2026-08-26。后续每关闭一个项目，应同步更新实现归档、研究报告、测试说明、原始 evidence
和交付校验器。

