# F455 后 SOTA 缺口审计

## 1. 审计结论

F455 已关闭“存在 agent hook 但没有可审计闭环”的工程缺口：反应式触发、固定模型/提示/transport 身份、
严格 schema、portfolio 共享预算、online/shadow/fallback 配对、候选真实性验证、权威 AFL 覆盖回馈和跨
重启 hash-chain ledger 均已落地。模型仍不在 SAT/UNSAT、目标可达、parser 有效性或覆盖新颖性的正确性
根中。

按当前审计顺序，尚未实现的明确功能项只剩 F456 完整 POSE 风格 initial symbolic heap 域。公共目标上的
长时等 CPU 评价仍是 P0 研究任务，但它是效果证据，不应伪装成新的运行时启发式功能。

## 2. 剩余顺序

| 顺序 | 项目 | 尚缺内容 | 验收边界 | 优先级 |
| ---: | --- | --- | --- | --- |
| 1 | F456 POSE 风格 initial symbolic heap | F405--F422 已有有界 C heap、MemoryPhi 与 lifetime union，但没有入口未知对象图的 fresh/null/alias 按需 materialization | 稳定对象身份；lazy field read/write；alias quotient；free/bounds/init 一致性；只在真实 CFG branch 分叉；形式 oracle 与公开 heap case | P1 |
| 2 | F455/F456 R-track | F455 目前只有固定 backend 机制 oracle，F456 尚未开始；没有真实模型/公开 heap 目标的长期统计 | 预注册版本与目标；等 CPU-hours；多 seed；6h/24h；online/shadow/fallback；coverage AUC、time-to-target、solver/model cost、失败率与置信区间 | P0 研究验证 |

## 3. F456 为什么不能由现有 heap 功能冒充

[POSE](https://arxiv.org/abs/2407.16827) 的关键不是增加一个“符号指针”开关，而是在初始 heap 未知时，
把对象可能为 `null`、既有 alias 或 fresh object 的关系保持在符号 heap 公式中，避免为与程序控制流无关
的 heap 选择制造额外路径。现有 F405--F422 从编译器给定的有限对象/points-to 候选出发，已经处理一批
lifetime、初始化、guard、MemorySSA 和跨过程摘要问题，但它没有定义一般 initial symbolic heap 的对象
materialization 与 quotient 语义。因此 F456 必须新增形式域、持久身份和反例 oracle，不能只改调度器。

## 4. F455 尚待研究验证的内容

F455 oracle 证明协议和消融完整性：三臂各评估四次门控，均触发 3、抑制 1；online/shadow 各得到 3 个
严格有效响应，fallback 零调用；online 准入 3 个候选，shadow 只记录不准入。固定 command backend
不是语言模型，这些结果不支持覆盖提升。后续实验至少需要一个结构化 parser 目标、一个语义 barrier
目标和一个非结构化负对照，并同时报告调用率、proposal 准入率、真实目标命中率、全局新 edge/调用、
token/CPU-hour 和无收益目标。

## 5. 不再列为功能缺口

- F455 结构化 agentic concolic 控制面：机制实现与测试已完成；
- “再增加一个 LLM route 名称”：没有新语义、可信边界和配对实验时不构成 SOTA；
- F454 solver-native PalRUP、F453 online cubing、F452 checker compression：功能已验收，真实收益归各自
  R-track；
- 用论文报告的覆盖率或速度替代本地实验：不属于实现，也不能成为本项目结果。

审计日期：2026-08-26。下一项按顺序实施 F456；完成前不得把当前 bounded C-heap adaptation 表述为
完整 POSE。
