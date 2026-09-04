# F456 后 SOTA 差距审计

## 1. 审计结论

F456 已关闭 2026-08-26 功能审计中的最后一个已登记运行时缺口：有界 C layout 上的 POSE 风格 initial
symbolic heap 现已具备 stable proxy identity、lazy byte/reference-field refinement、ITE alias、conditional
store/free、alias quotient、QF_BV/Z3 导出、严格 model gate、concrete graph 物化以及 continuation CAS 恢复。

因此，当前没有另一个可以在缺少新文献证据和新语义合同的情况下诚实登记为“未实现 SOTA 功能”的项目。
这不等于研究工作结束，也不等于系统对任意 C/LLVM 程序完备。剩余最高优先级工作属于效果验证、前端扩展
或开放研究问题，不能通过继续添加启发式名称替代。

## 2. 已关闭的原缺口

| F455 后要求 | F456 实现 | 本地证据 |
| --- | --- | --- |
| 稳定对象身份 | root/derived reference、proxy object、term/state 全部 canonical SHA-256 | snapshot/checkpoint identity tests |
| lazy field read/write | byte lane ITE read、conditional alias write、64-bit reference field | 27/27 + 256 alias assignments |
| alias quotient | equality union、disequality conflict、null/type pruning | adversarial model/branch tests |
| free/bounds/init 一致性 | conditional live clear、structural bounds、byte init tag | load/store/free negative tests |
| 只在真实 CFG branch 分叉 | heap ops `cfg_forks=0`，`branch_alias` 建 child | swap 2、sum 1、list max10 12 paths |
| 形式 oracle 与公开风格 case | exhaustive concrete equivalence + POSE motivating examples | 9/9 sealed oracle |
| continuation 接线 | symbolic-store marker、parent lineage、digest-checked restore | coupled 231 + 282 subtests |

## 3. 仍需做但不登记为新运行时 SOTA 的工作

### P0：长时等 CPU 公开目标评价

F455 需要真实模型的 online/shadow/fallback 多 seed 对照；F456 需要可公开的 C heap harness 与
bounded lazy-initialization baseline。预注册版本、目标、seed、CPU-hours 与 failure policy，至少报告 6h/24h
coverage AUC、time-to-target、solver/model cost、峰值状态/term、无收益目标和置信区间。论文结果不得转移为
本项目结果。

### P1：自动 LLVM/C heap 前端

当前 F456 API 需要调用方提供 type layout、roots 和 access。递归 debug type/IR 恢复、union/type punning、
custom allocator、任意 pointer arithmetic 和 ABI wrapper 是前端覆盖扩展；完成后应保持 F456 域不变，并
以 differential replay 验证映射，不应重新发明 heap 语义。

### P1：大型 ITE heap solver 工程

当前 QF_BV 导出正确但没有证明大型真实目标上的求解效率。可研究 term hash-consing 跨 worker 共享、
ITE simplification、array encoding、incremental solver context 与 separation-logic summary；任何优化必须由
原始公式等价 oracle 和真实 solver 成本验证。

### P2：并发 initial heap

把 initial heap 与 ConDPOR/原子内存模型组合，需要定义 allocation/free、alias quotient 与 schedule event
的线性化语义。当前单线程 F456 不应被表述为该开放问题的实现。

## 4. 声明边界

[POSE arXiv v2](https://arxiv.org/abs/2407.16827) 的公开 trace/solver/time 结论属于作者的 Java bytecode
prototype。F456 本地证明的是有界 C 适配的机制正确性、公开风格路径数与 Python 成本。没有公共目标长时
数据时，不声明 coverage、bug yield、solver speedup 或完整论文复现。

审计日期：2026-08-26。下一阶段应先执行 P0 R-track，而不是继续把尚未验证收益的新 heuristic 登记为
“SOTA 已实现”。
