# Cross-theory Selective Concolic 实现记录

## 技术动机

Selective concolic execution 不能只按变量名删约束。字符串长度、位向量编码、整数转换、数组/内存访问常形成跨理论依赖；遗漏一个依赖会让 selective query 产生不可回放的模型。为此本阶段实现依赖闭包优先、无法证明则 full-query 回退的策略。

## 实现细节

`util/cross_theory_selective.py` 将约束表示为带 theory、变量和依赖边的 `ConstraintAtom`。给定目标变量集合，selector 计算跨理论依赖闭包；闭包内约束进入 selective query，闭包外变量只有在 concrete assignment 中存在时才固定。任一依赖缺失、变量没有 concrete 值、输入超过预算或 atom 非法时，输出 `mode=full`，保留完整理论查询。这样优化只减少查询规模，不改变可满足性语义；fallback 原因写入 `SelectiveDecision` 便于 telemetry 和后续统计。

## Review 与测试

专项测试覆盖 STRING→BV 跨理论闭包、固定未选变量、未知依赖回退、缺失 concrete 值回退和重复 atom ID。验证结果：3/3 通过，Ruff 检查通过。

该模块尚未替换现有 Z3 query builder；接入时应由 query generator 提供真实 atom dependency graph，并将 `mode`、selected atoms、fixed variables、fallback reason 写入已有 selective-query telemetry。
