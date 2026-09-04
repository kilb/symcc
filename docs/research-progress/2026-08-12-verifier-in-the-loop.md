# Verifier-in-the-loop 实现记录

## 目标

Gordian、NeuroSCA 及相关 agentic/semantic execution 工作共同强调：模型或启发式生成的输入、语义变换和约束补全只能是候选，必须经过真实执行或可检查证书验证后才能进入 coverage corpus。本阶段为现有 `VerifiedProposalManager` 增加通用的验证闭环门面。

## 实现

`util/verifier_loop.py` 提供 `VerifierInTheLoop`。调用方注册命名验证器，例如 `concrete_replay`、`semantic` 或 `certificate`。每个验证器只能返回结构化 evidence；系统记录 passed、target reach、coverage delta、certificate、耗时和原因。候选通过 quorum 且 coverage-affecting proposal 必须存在并通过 `concrete_replay` 时才被接受。相同 `proposal_id` 返回同一不可变 decision，避免重复执行和审计结果漂移。

验证器调用有总时间预算，异常/非对象/非有限数会降级为负证据。决策使用规范化 JSON SHA-256 形成 `decision_id`，便于和现有 `VerifiedProposalManager` 的 proposal 状态关联。该门面不执行任意生成代码，也不替代 manager 中已有的 parser、grammar 和 candidate 文件校验。

## 测试结果

`test/test_verifier_loop.py` 覆盖 quorum 接受、具体回放失败强制拒绝、验证器格式错误、decision 幂等和超时返回后的拒绝。结果：4/4 通过，Ruff 检查通过。

## 接入边界

真实 runner 应把 `concrete_replay` 绑定到原生目标程序和统一 coverage map，把语义/形式验证器绑定到已有 `VerifiedProposalManager`。验证器只返回证据，资源隔离、进程终止和文件清理仍由 runner 负责。
