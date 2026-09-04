# 最近两周研究工作进展

> 统计周期：2026 年 8 月 20 日至 2026 年 9 月 3 日  
> 项目方向：面向混合模糊测试的并行符号执行框架  
> 证据口径：实现、回归测试、机制实验与受控跨节点实验；不将其等同于公开目标长时性能结论

## 一、阶段概述

本阶段围绕“并行能力能否稳定转化为有效覆盖”开展工作，共完成 F442、F443、F446--F464
等 **21 个有文档记录的实现或审查里程碑**。工作重点从继续堆叠求解策略，转向解决制约并行系统
可信扩展的基础问题：高并行度下的协调开销与重复计算、节点失效后的任务一致性、复杂 QF_BV
查询的可验证拆分、AFL 与 SymCC 覆盖状态收敛、长时运行的事务恢复，以及实验结论可能受到的
统计偏差。

阶段性成果是形成了一条较完整的研究闭环：系统能够对任务进行动态分配和故障恢复，能够把单个
困难查询拆分为带证明边界的并行子任务，能够将 LLM 建议纳入受控调度而不交出正确性裁决权，
并通过连续四轮反例驱动审查，修复符号语义、持久事务、结果传输和规模分析中的关键错误。

## 二、主要工作与取得的成果

### 1. 建立并行规模决策与瓶颈定位方法

引入 Universal Scalability Law（USL）、覆盖饱和曲线、物理资源约束和新颖度衰减四类因素，形成
保守规模上限 `N* = min(N_resource, N_master, N_USL, N_novelty)`。同时将 Master 热路径拆分为
扫描、派发、接收、结果裁决和调度器子阶段，并把 Prefix-DAG 的全图 MDP 更新改为受影响前缀的
有界刷新。

机制观测中，queue poll 优化使 Master 扫描时间由 **30.19 秒降至 19.47 秒**，扫描次数下降
**72.52%**；相近 observation 规模的单轮观测中，bounded refresh 将 adaptive scheduler 时间由
**59.37 秒降至 14.28 秒**，Master triage 由 **78.73 秒降至 33.78 秒**。后两组运行并非等时多轮
A/B，因此只能证明瓶颈得到缓解，不能据此宣称端到端覆盖提升。

规模实验进一步揭示了关键问题：9 个三档均成功的配对区组中，相对 8 worker，32/128 worker 的
`unique/s` 分别增加 **17.63%/29.29%**，但终点边覆盖分别变化 **-5.99%/-1.82%**，去重保留率
下降 **8.27%/13.98%**；128-worker 在 11 个完整区组中出现 **2 次内部失败**。因此系统明确拒绝
拟合单调覆盖上限。这一结果说明当前瓶颈已从“候选生成不足”转向“重复候选、串行裁决和覆盖饱和”，
也避免把吞吐提升误报为覆盖提升。

### 2. 完成面向连续故障的弹性并行框架

将 ULFM 恢复接入真实 Master/Worker 热路径，使用 `generation + shard + lease + work hash`
对任务进行代次栅栏；进一步实现多 Master、warm spare 晋升、共享 WAL 聚合和跨节点原子重放。
发生进程失效后，系统按 `Revoke -> Shrink -> survivor attestation -> Agree -> generation commit`
重建通信域，并将状态不确定的在途任务重新排队。该设计允许幂等重复计算，但阻止旧代结果污染
新代状态。

双主机受控实验使用 **10 ranks（8 active + 2 warm spares）**，连续退出 rank 2 和 rank 3 后，
备用 rank 8 和 rank 9 依次晋升；到 generation 2 仍保持 **8 active、2 Masters、6 Workers**，
并对 **820 个 corpus 对象和 2,491 个证据文件摘要**完成复核。这证明了跨节点连续恢复和
effectively-once publication 机制，但 15 秒受控任务不代表生产环境 MTTR、求解加速或覆盖收益。

### 3. 构建可认证的并行 QF_BV 求解链

围绕 Cube-and-Conquer、proof-carrying solving 和 solver-native clause sharing，完成以下闭环：

- 依据 proof prefix 与已检查的 activity 信息，将单个 QF_BV 查询划分为完整、互斥的 cubes；
- 以 fenced lease 并行执行 cubes，SAT 结果回放验证，UNSAT 结果沿 split tree 聚合为原查询证明；
- 将 cube 身份与 ULFM generation 绑定，使恢复后的跨节点结果仍可验证；
- 建立闭包绑定的 proof replay cache、原生差分 varint 子句压缩，以及 PalRUP 官方检查闭环；
- 使用在线 activity/cost 策略选择分块方式，但将启发式与正确性根严格分离。

机制实验中，512-cube 证书构建并内置重放中位数为 **139.881 ms**，独立重放为 **68.033 ms**；
64 层 proof-DAG 的授权缓存相对禁用缓存达到 **25.389 倍**机制重放倍率，但该倍率不包含 SAT search。
原生子句压缩在 3/8/32/256 literals 下分别减少 **24.3%/38.6%/48.4%/52.1%** 的 literal
payload；单 literal 反而增加 11.4%，该负收益被保留。1/2/4-rank solver-native PalRUP 实验均通过
官方检查，同时观察到 rank 增加后 proof/check 成本和 pending residue 上升，说明后续重点应是
通信准入与证明开销控制，而不是继续无条件增加并行度。

### 4. 探索智能化与复杂状态符号执行

实现结构化 Agentic Concolic 闭环：仅在 solver unknown、超时、Backsolver 失败或覆盖平台期触发
模型；模型只能提出调度动作或候选输入，候选仍须经过 schema、预算、目标执行、分支验证和 Master
权威 AFL novelty 检查。系统支持 online、shadow、fallback 三臂实验，并以 hash-chain ledger 记录
请求、响应、预算和真实执行反馈。固定后端实验验证了协议与消融隔离，尚未证明真实 LLM 能提升公开
目标覆盖率。

同时引入 POSE-C 风格 initial symbolic heap，以代理对象、alias ITE 和按需物化表示入口未知对象图，
避免经典 lazy initialization 为非控制流别名选择制造大量额外路径。独立 oracle 覆盖 null、alias、
fresh、store、free、snapshot 和模型物化；16 引用机制实验解析出的组合空间为
**82,864,869,804 种 alias/null partition**。该数据说明表示能力，并非已探索路径数或覆盖提升。

### 5. 闭合覆盖反馈、持久事务与符号语义

完成 AFL/SymCC 单调覆盖并集、启动回放、失败重试 ledger、版本化 bitmap 快照、批量 coverage claim
和 corpus prepared/decided/commit/recover 事务。Hybrid RESULT 正常路径改为内容寻址对象引用，
Master 在 admission 与 triage 两次复验摘要；worker 的 coverage 与内容去重状态只在整批成功后提交，
避免部分失败造成后续永久漏测。Data Map 运行时加入 `pthread_once`、线程局部重入保护和 never-zero
原子计数。

性能与可靠性复审还将 256 KiB dense coverage delta 的峰值内存由约 **24.21 MiB 降至 2.08 MiB**；
25k-state frontier 的 hot claim 与 hot snapshot 机制微基准分别约提升 **4.0 倍和 956 倍**。后者不包含
共享文件系统竞争、真实求解或 AFL 执行，不能换算为系统整体加速。

LLVM 语义层修复了 GEP 在 pointer width、address-space index width、超 4 GiB 结构偏移、allocation
padding 和 scalable vector `vscale` 下的表达式错误；不支持的 non-integral pointer address space
改为失败关闭。控制面则统一处理无 worker、配置错误、锁冲突、服务异常和 STOP/ACK 清理，防止“清理
成功”掩盖“运行失败”。

## 三、先进性、创新性与工作挑战

本阶段引入的是当前并行求解与混合测试中的前沿研究方向，而非简单拼接独立模块：ULFM 弹性恢复、
Cube-and-Conquer、可检查证明与 PalRUP、ImpCheck 风格原生子句压缩、Prefix-DAG/MDP 调度、POSE-C
符号堆和 LLM 辅助 concolic execution。工程上的主要创新体现在三点：

1. 将证明携带的查询分区与 generation-fenced 分布式任务生命周期连接起来，使启发式影响效率、
   独立 verifier 决定正确性；
2. 将 LLM 限定为受预算、可回放的 proposal provider，真实执行和全局覆盖继续作为唯一反馈依据；
3. 将“证据不充分时拒绝给出规模上限”固化为程序门禁，把统计可信度作为系统功能而非报告约定。

主要挑战包括：节点退出时无法区分已完成但未提交与真正丢失的任务；高并行度会同时放大重复候选、
Master 串行区和 proof checking 成本；LLVM 地址计算必须精确匹配不同 target data layout；持久事务的
线性化点需要与崩溃恢复逐一对应；而公开 benchmark 上的因果收益还需要等 CPU、长时间、多随机种子和
完整 coverage provenance，不能由单次微基准替代。

## 四、验证结果与证据边界

截至 2026 年 9 月 3 日冻结记录，最终完整回归结果为：

- Python：**1668 passed + 616 subtests**，342.17 秒，零失败；
- LLVM 18：349 项中 **348 passed + 1 unsupported**，306.90 秒，零失败；
- LLVM 17：隔离组合共 349 项，**347 passed + 2 unsupported**，零逻辑失败；其中全仓库
  provenance 自校验独立运行，避免与其他测试的临时工作树产物发生摘要竞态；
- Ruff、Python 编译检查、Data Map 严格 C11 `-Wall -Wextra -Werror` 构建与差异格式检查通过。

上述结果证明实现合同、异常路径和回归兼容性；跨节点实验进一步证明了受控环境下的连续恢复。现阶段
仍**没有新增公开目标上的长时、等 CPU、多随机种子对照实验**，因此不能给出“混合测试覆盖提升 X%”、
“缺陷发现增加 X 个”或“整体求解加速 X 倍”等结论。F447 的规模数据属于提前停止的工程诊断，F450、
F452、F461 的倍率属于特定内部机制。源码 manifest 可核验当前工作树内容，但 Git tracked/clean
交付门禁仍须在正式提交和 clean clone 中闭合。

## 五、阶段结论与下一步

最近两周已经把项目从“具备多种并行与 SOTA 组件”推进到“关键状态可恢复、结果可验证、实验结论有
准入门槛”的阶段。当前最值得优先解决的问题是单 Master 权威 coverage/corpus 提交瓶颈、长时
`.result_objects` 回收、proof sharing 背压，以及缺少公开目标 R 级证据。下一阶段应固定代码与环境，
按 AFL-only/hybrid、不同符号 worker 数进行等 CPU、长时间、多 seed 实验，报告覆盖 AUC、终点覆盖、
有效输入保留率、solver/proof/triage 时间和失败率，并用样本外规模点验证上限模型。

## 六、主要证据索引

- [并行规模模型与有界 Prefix-DAG MDP](Parallel_Scaling_Model_and_Bounded_Prefix_MDP_F442_2026-08-20.md)
- [跨节点连续 ULFM 恢复与规模证据](Cross_Node_Continuous_ULFM_and_Scale_Evidence_F447_2026-08-25.md)
- [Proof-Prefix 引导的可认证 QF_BV 分区](Proof_Prefix_Guided_Certified_Partitioning_F448_2026-08-25.md)
- [闭包绑定的证明重放缓存](Closure_Bound_Proof_Replay_Cache_F450_2026-08-25.md)
- [Solver-native 子句共享与 PalRUP 闭环](Solver_Native_Clause_Sharing_PalRUP_Production_F454_2026-08-26.md)
- [结构化 Agentic Concolic 闭环](Structured_Agentic_Concolic_Closed_Loop_F455_2026-08-26.md)
- [全局覆盖收敛与并行有效产出优化](Global_Coverage_Convergence_and_Parallel_Conversion_F457_2026-08-26.md)
- [第四轮深度审查与语义、传输、缩放闭合](Fourth_Deep_Review_Semantic_Transport_Scaling_F464_2026-09-03.md)
