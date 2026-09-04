# F451 后 SOTA 缺口审计与实施顺序

## 1. 审计结论

F451 已关闭 F450 审计中的第一项 P0 缺口：F449 的认证 cube 不再局限于同进程 backend slot，而是
成为 stable-endpoint、generation-fenced、可持久恢复的跨节点工作。当前项目已经具备从完备互斥分区、
叶级 SAT/UNSAT 检查，到节点失效后的重排和全局收敛的连续机制链。

这仍不等于“所有 SOTA 均已完整复现”。剩余项目分为三类：可信求解热路径仍有明确机制缺口；已有前置
信号但尚未形成论文级闭环；机制已实现但缺公共目标长时 R 级证据。本审计继续使用
I（实现）/T（测试）/E（机制实验）/R（公共目标等资源重复实验）四级口径。

## 2. F451 关闭了什么

| F450 后缺口 | F451 当前证据 | 判断 |
| --- | --- | --- |
| cube lease 未映射到跨节点 worker | inner cube token 与 outer generation/shard/work fence 规范绑定 | 已关闭 |
| communicator replacement 后旧结果可能越代提交 | 心跳和结果均需双层 current；旧代 token 精确拒绝 | 已关闭 |
| survivor-owned 在途任务状态不确定 | 全部 ambiguous in-flight work 进入新代 recovery queue | 已关闭 |
| 跨 ledger 崩溃窗口 | dispatch/binding 与 inner-result/outer-finish 双向 reconciliation | 已关闭 |
| 恢复后 SAT/UNSAT 收敛 | SAT 全局取消 current peers；UNSAT 继续重放完整 checked leaves | 已关闭 |
| 物理跨主机应用协议 | 3/3 轮双主机 adapter，远端真实 exit 86 后无丢叶收敛 | E-crosshost-adapter；不是新 MPI 性能证据 |

## 3. 剩余实施顺序

| 顺序 | 优先级 / 编号 | 真正缺口 | 完成定义 |
| ---: | --- | --- | --- |
| 1 | P0 / F452 | **native checker clause compression**：F450 避免重复语义重放，但热命中仍读取、传输和哈希完整 literal closure | 固定论文/源码合同；unsigned literal sorting；delta + canonical variable-byte 编码；不超过 7-byte 小 clause inline；严格 round-trip、overflow、truncation、non-canonical、endianness 反例；接入 native checker 热路径；内存/字节/时间消融 |
| 2 | P1 / F453 | **online activity/cost-guided cubing**：checked activity 和执行成本尚未形成 query-family 的 prerun→partition→outcome 在线学习闭环 | 只消费 checked activity 与观测成本；持久 policy identity；有界探索和确定性 fallback；static/activity/cost 三路等预算机制对照 |
| 3 | P1 / F454 | **solver-native PalRUP producer**：已有官方 checker consumer/redistribute/confirm pipeline，但 production fragment 仍来自转换器或 fixture | CaDiCaL worker 原生产生 rank fragment；稳定 rank/ID namespace；crash-close；官方 checker 端到端；项目 parser 不得自证 |
| 4 | P1 / F455 | **结构化 LLM concolic 完整回路**：已有 bounded verified proposal，缺 history-guided seed acquisition 和 iterative solve-complete | LLM 只提出结构/输入；本地 parser、Query IR 和真实目标裁决；预算、取消、重放、无模型 fallback；公开结构化 parser 消融 |
| 5 | P2 / F456 | **完整 POSE initial symbolic heap 域**：当前仅有 bounded C heap/MemoryPhi adaptation | fresh/null/alias materialization、field update、heap quotient、只在真实 CFG branch 分叉；形式不变量与公开 heap benchmark |
| 6 | R-track | **长时、多节点公共目标证据** | 预注册多规模、多独立 seed、6h/24h AFL-only/hybrid/feature ablation；覆盖 AUC、time-to-edge、失败率、CPU-hours 与置信区间；保留超时和零收益目标 |

## 4. 为什么 F452 下一步优先

F450 把重复完整 proof replay 降为闭包见证，但其热路径复杂度仍与 closure 的对象数和编码字节数线性相关。
TACAS 2026 的实时分布式增量 SAT 工作把 checker 内存、传输和即时确认视为核心工程约束，并采用 literal
排序、差分和变长编码压缩 checker 内部 clause。F452 因而不是另一个调度启发式，而是当前可信求解链
中最靠近热路径、最容易用确定性反例和资源消融证明的剩余 P0 项。

实现必须保持三条边界：压缩格式不是新的证明系统；解码后 clause 仍进入现有 proof checker；任何
non-canonical、溢出、截断或资源超限输入均失败关闭，不能回退成未检查导入。

## 5. 主要依据

- Schreiber et al., *Real-time Proof Checking for Distributed Incremental SAT Solving*, TACAS 2026：增量 assumptions、checked clause sharing、实时确认与 checker 内 clause compression；
- Schreiber et al., *Mallob: Scalable Automated Reasoning on Demand*, CAV 2026：proof-checked、incremental、malleable distributed reasoning；
- *A Natively Parallel Proof Framework for Clause-Sharing SAT Solving*, SAT 2026：PalRUP 并行 proof producer/checker；
- *Smart Cubing for Graph Search*, 2025：prerun、cubing strategy 与 algorithm configuration 的联合设计；
- Cottontail, IEEE S&amp;P 2026，以及 HyLLfuzz, 2024：LLM proposal 必须与确定性执行验证组合；
- POSE, SANER 2026：initial symbolic heap 与 heap path optimality。

## 6. 声明边界

F451 可以声明跨节点认证 cube 的协议闭环和机制证据，不能声明 SSH 是生产 transport，也不能由三轮
adapter 实验外推 solver、coverage 或 defect-yield 提升。F452--F456 的“完成”仍需各自满足上表的
I/T/E 定义；R-track 不能由单元测试或合成微基准替代。
