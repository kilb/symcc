# F453 后 SOTA 缺口审计与实施顺序

## 1. 审计结论

F453 已关闭上一轮首个 P1 缺口：F436 的 checked activity、F448 的认证分区和 F449--F451 的 verified
outcome/cost 现在形成按 query family 持久化的在线闭环。策略具有固定身份、精确选择 propensity、并发
pending 约束、确定性回退和等 CPU-ms 预算；其输出不能绕过分区、model 或 proof 复核。

这不意味着公共目标端到端收益或全部 SOTA 已完成。机制 oracle 反而表明小型矛盾公式上 static 明显更快，
说明在线门控的必要性，也说明公开 benchmark 的长时多 seed 证据仍属于独立 R-track。

## 2. F453 完成定义复核

| F452 后完成定义 | F453 证据 | 判断 |
| --- | --- | --- |
| 只消费可复核 activity/成本 | ACK 配对 receipt 由 F448 重放；outcome 重新解析、重算摘要和预算 | 已关闭 |
| 持久 policy identity | SQLite metadata 绑定 canonical policy、SHA-256、protocol 与路径身份 | 已关闭 |
| 有界探索和确定性 fallback | arm/cube 两层 warm-up、pending 上限、ε/UCB 固定点、无能力回退 static | 已关闭 |
| 三路等预算机制对照 | static/activity/cost 各 5 轮；统一 8000 CPU-ms 配置上限；prerun 内扣 | E-mechanism |
| query-family prerun→partition→outcome | query service、F448、F449、QueryStore 与持久 ledger 生产接线 | 已关闭 |

## 3. 剩余实施顺序

| 顺序 | 优先级 / 编号 | 真正缺口 | 完成定义 |
| ---: | --- | --- | --- |
| 1 | P1 / F454 | **solver-native PalRUP producer**：F439/F440 已有官方 consumer、redistribute 和 global confirm，但 fragment 仍主要来自转换器/fixture | CaDiCaL worker 原生产生 rank fragment；稳定 ID namespace；异常退出不产生可接受完成标记；官方 checker 端到端；项目 parser 不得自证 |
| 2 | P1 / F455 | **结构化 LLM concolic 完整回路**：已有 bounded verified proposal，缺 history-guided seed acquisition 和 iterative solve-complete | 模型只提出结构/输入；本地 parser、Query IR 与真实目标裁决；预算、取消、重放、无模型 fallback；公开 parser 消融 |
| 3 | P2 / F456 | **完整 POSE initial symbolic heap 域**：当前是 bounded C heap/MemoryPhi adaptation，不是完整对象域 | fresh/null/alias materialization、field update、heap quotient、仅在真实 CFG branch 分叉；形式不变量与公开 heap benchmark |
| 4 | R-track | **公共目标长时证据** | 预注册多规模、多 seed、6h/24h AFL-only/static/activity/cost/feature ablation；coverage AUC、time-to-edge、失败率、CPU-hours 与置信区间 |

## 4. 下一项为什么是 F454

项目已经有 PalRUP wire codec、官方三阶段 checker 和分布式 global confirmation，但还缺从真实 solver worker
直接产生 fragment 的生产热路径。这个缺口比继续扩展在线策略更接近 proof-parallel execution 的可信根：
如果 fragment 由离线转换器生成，consumer 正确也不能证明 worker 崩溃、ID 冲突和未完成流在原生生产阶段
被正确处理。F454 应先固定 CaDiCaL producer ABI、稳定 clause ID namespace、flush/close 语义和官方 checker
版本，再做 fault injection 与多 rank 端到端验证。

## 5. 声明边界

F453 的 15 次机制 oracle 证明选择、activity 复核、cost 反馈和预算合同可执行。它没有公开 fuzz target、
长时 campaign 或统计功效；guided arm 在该 fixture 上慢于 static。任何“覆盖率提升”“并行加速”或“缺陷
发现提升”仍必须由 R-track 的独立预注册实验支持。

## 6. 资料

- [`F453 研究报告`](Online_Activity_Cost_Guided_Cubing_F453_2026-08-26.md)；
- [Smart Cubing 预印本](https://arxiv.org/abs/2501.17201)；
- [`F452 后审计`](SOTA_Gap_Audit_After_F452_2026-08-26.md)；
- [`F439 PalRUP wire`](LIDRUP_PalRUP_Proof_Wire_Interoperability_F439_2026-08-18.md)；
- [`F440 PalRUP global confirmation`](PalRUP_Global_Confirmation_Pipeline_F440_2026-08-18.md)。
