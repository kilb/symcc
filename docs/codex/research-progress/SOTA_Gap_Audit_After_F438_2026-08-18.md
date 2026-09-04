# F438 后 SOTA 技术缺口审计

- 审计日期：2026-08-18
- 审计基线：F00--F438 当前工作树、F438 完整门禁和两组本机 5-rank 物理归属机制实验
- 结论：仍有未完成的 SOTA 工作；F438 关闭逻辑 malleability 的 P0 正确性缺口，但 proof 互操作、动态集群运行时和公开 R 级实验尚未完成

## 1. 已经完成到什么程度

F438 已具备：确定性多 job 槽分配、滞回、grow/drain/migrate/shrink、两阶段 prepare/receipt/commit、
generation/token fencing、在线真实完成与重启持久 lease 的区分、proof cursor、QueryStore 原子 checkpoint、
单协调者锁、完整 trace 重放和真实 MPI rank 操作归属证明。

因此，“可塑资源调整完全未实现”已经不是准确描述。但下列说法仍然不成立：动态 MPI rank 创建已完成、
节点丢失可自动修复、已兼容 PalRUP/ImpCheck wire format、已在公开 benchmark 证明覆盖率或速度提升。

## 2. 剩余优先级

| 顺序 | 优先级 | 缺口 | 为什么仍是 SOTA 问题 | 验收底线 |
| ---: | --- | --- | --- | --- |
| 1 | P0 / F439 | PalRUP/ImpCheck/LIDRUP proof wire 互操作 | 当前 proof DAG 是项目内协议，跨实现组合验证仍弱 | 固定版本工具链；导入/导出；独立 checker；乱序、缺失、重复、篡改负例；不以自身 parser 自证 |
| 2 | P0 | 动态运行时与故障恢复 | F438 只在预启动 slot 上逻辑伸缩 | 至少一个实际可增减 executor 的运行时；节点丢失、重复 coordinator、partition/rejoin、proof/lease 恢复故障矩阵 |
| 3 | P0/R | 8/32/128 worker 公开目标实验 | 本机机制 oracle 不能给 scalability 结论 | 预注册 workload/seed/budget；强弱扩展；利用率、checker CPU、网络字节、solve/coverage/defect 指标；置信区间和失败样本 |
| 4 | P1 | proof-prefix partitioning | 当前共享知识，不生成可组合的搜索空间划分证明 | prefix 完备/不交叠证书、动态再分割、独立合并验证、与 F438 迁移联动 |
| 5 | P1 | proof-aware global scheduling | F437/F438 只用局部历史和 backlog，未优化全局 proof critical path | 把 proof DAG 关键路径、checker 队列、网络成本纳入约束优化；与简单 backlog policy 做消融 |
| 6 | P1 | 多理论增量上下文 | 强复用集中于 QF_BV；数组、字符串、浮点仍不对称 | 每个理论定义可验证 reuse identity、失效规则和完整公式复核，不允许仅凭结构 hash 授权 |
| 7 | P2 | 学习策略的安全在线优化 | 当前整数启发式确定但保守 | constrained bandit/off-policy evaluator；探索预算、回滚、漂移检测；决策 artifact 可重放 |
| 8 | P2 | 形式化协议模型 | Python verifier 是可执行规范，但不是机器检查证明 | TLA+/Ivy/PlusCal 模型覆盖 prepare/drain/commit/crash，模型反例进入测试 corpus |

## 3. 下一项 F439 的建议拆分

1. **格式边界**：定义项目 proof record 到外部 proof command/ID 的双向映射，拒绝隐式默认值。
2. **独立检查**：导出 artifact 必须由固定版本外部 checker 在 fresh process 检查；导入先检查再进入 CAS。
3. **增量会话**：activation、deletion、assumption scope 和 solve generation 必须显式编码。
4. **并行组合**：不同 publisher 的局部 proof 需要全局唯一 ID 或可验证 rename；缺失依赖必须失败关闭。
5. **持久恢复**：checkpoint 绑定 checker/toolchain 内容身份，旧版本结果不能静默复用。
6. **实验**：至少包含双 publisher 乱序流、重启、重复 record、删除后引用、错误 rename 和最终 UNSAT 合并。

## 4. 近期研究依据

- [A Natively Parallel Proof Framework for Clause-Sharing SAT Solving, SAT 2026](https://drops.dagstuhl.de/entities/document/10.4230/LIPIcs.SAT.2026.17)
- [Real-time Proof Checking for Distributed Incremental SAT Solving, TACAS 2026](https://satres.kikit.kit.edu/papers/2026-tacas-distrincproof.pdf)
- [Problem Partitioning via Proof Prefixes, SAT 2025](https://drops.dagstuhl.de/entities/document/10.4230/LIPIcs.SAT.2025.3)
- [Mallob: Scalable SAT Solving in the Cloud](https://arxiv.org/abs/2205.06590)
- [Distributed Incremental SAT Solving with Mallob](https://arxiv.org/abs/2505.18836)
- [Streamlining Distributed SAT Solver Design, SAT 2025](https://drops.dagstuhl.de/entities/document/10.4230/LIPIcs.SAT.2025.27)

## 5. 结论

当前最合理顺序仍是：**F439 proof wire 互操作 -> 动态运行时/故障恢复 -> 8/32/128 worker 公开 R 级实验 ->
proof-prefix partitioning -> proof-aware 全局调度 -> 理论扩展与形式化**。其中前三项决定系统能否从
`I/T/E-local` 上升为可对外比较的分布式实证系统，不能以更多 synthetic oracle 替代。
