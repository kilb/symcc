# F452 后 SOTA 缺口审计与实施顺序

## 1. 审计结论

F452 已关闭上一轮唯一 P0 实现缺口：checked-import queue 与 clause-activity tracker 不再长期保存完整
32-bit literal vector，而使用协议化、可强制、可审计的 native compressed clause。20k property oracle、
真实 CaDiCaL callback 和完整 capability gate 支持 I/T/E-mechanism 结论。

这不等于剩余 SOTA 或 R 级实验全部完成。后续从 P1 开始，优先级由“可信求解热路径正确性”转向“在线
分区决策质量、原生并行证明生产、结构化 agent 闭环和更完整 heap 域”。

## 2. F452 关闭情况

| F451 后完成定义 | F452 证据 | 判断 |
| --- | --- | --- |
| 固定论文与源码合同 | TACAS 2026 artifact；ImpCheck commit `b5f37b2` | 已关闭 |
| unsigned sort + delta + canonical varint | 已知字节与 20k property oracle | 已关闭 |
| `<=7 B` inline | 明确 7/8-byte 边界、inline/heap telemetry | 已关闭 |
| corruption/overflow/resource 反例 | 专项参数化反例、零部分写、ASan/UBSan 100k | 已关闭 |
| native checker 热路径 | CaDiCaL 3.0.1 callback 2/2 ACK、38 literals 解码 | 已关闭 |
| 字节与时间消融 | 1--256 literals 九档、512 clauses、9 repeats | E-mechanism；不是 RSS/solver R 级 |

## 3. 剩余实施顺序

| 顺序 | 优先级 / 编号 | 真正缺口 | 完成定义 |
| ---: | --- | --- | --- |
| 1 | P1 / F453 | **online activity/cost-guided cubing**：F448 排序使用 checked activity，但未把 prerun 成本、partition outcome 和 query family 形成在线闭环 | 只消费可复核 activity/成本；持久 policy identity；有界探索和确定性 fallback；static/activity/cost 三路等预算机制对照 |
| 2 | P1 / F454 | **solver-native PalRUP producer**：F439/F440 已有官方 consumer/redistribute/confirm，生产 fragment 仍来自转换器或 fixture | CaDiCaL worker 原生产生 rank fragment；稳定 ID namespace；crash-close；官方 checker 端到端；项目 parser 不得自证 |
| 3 | P1 / F455 | **结构化 LLM concolic 完整回路**：已有 bounded verified proposal，缺 history-guided seed acquisition 和 iterative solve-complete | 模型只提结构/输入；本地 parser、Query IR 和真实目标裁决；预算、取消、重放、无模型 fallback；公开 parser 消融 |
| 4 | P2 / F456 | **完整 POSE initial symbolic heap 域**：当前仅有 bounded C heap/MemoryPhi adaptation | fresh/null/alias materialization、field update、heap quotient、只在真实 CFG branch 分叉；形式不变量与公开 heap benchmark |
| 5 | R-track | **公共目标长时证据** | 预注册多规模、多 seed、6h/24h AFL-only/hybrid/feature ablation；覆盖 AUC、time-to-edge、失败率、CPU-hours 与置信区间 |

## 4. 下一项为什么是 F453

F448 已有 static distance 与 checked activity，F449--F451 已有 cube outcome、成本和故障状态，F452 又
降低了 checked clause 的表示成本。当前最直接的算法缺口不是继续增加 proof 表示，而是把这些可信信号
闭合为 query-family 在线 cubing policy。F453 的困难在于 activity 与 solve cost 都是带选择偏差的结果：
策略只能通过有界探索、持久 propensity/identity 和等预算反事实避免把“被选中较多”误认为“本身更好”。

## 5. 声明边界

F452 只关闭 native checker clause representation。单 literal payload 在本地分布中增加 11.4%；真实
hot-path 的 73.7% payload reduction 来自两条机制 case，不能外推成进程 RSS 或 SAT speedup。F453--
F456 仍需各自达到 I/T/E；公共目标性能只能由独立 R-track 支持。

## 6. 资料

- [`F452 研究报告`](Native_Checker_Clause_Compression_F452_2026-08-26.md)；
- [TACAS 2026 论文条目](https://publikationen.bibliothek.kit.edu/1000193848)；
- [官方 artifact](https://zenodo.org/records/18330441)；
- [`F451 后审计`](SOTA_Gap_Audit_After_F451_2026-08-26.md)。
