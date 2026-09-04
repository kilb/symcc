# Codex 文档迁移后深度审阅记录（2026-07-30）

## 1. 审阅目标与范围

本轮只修改 `docs/codex/`，不改动 `docs/` 根目录及其他协作者的交付区。审阅对象包括：

- 项目主报告、架构问答、技术全景、当前实现评估和开发追踪；
- 28 页 PPTX 及其 Markdown 源；
- 21 张报告图、25 张 QA 图和 7 张一手证据截图；
- 两组实验原始证据、统计汇总、manifest 与测试记录；
- HTML 浏览入口、完整报告 HTML、ZIP 和 SHA-256 清单。

审阅原则是让每条重要结论都能回答四个问题：它来自代码、测试、实验还是论文；口径是否
一致；证据等级是否足够；图形是否会诱导出比数据更强的结论。

## 2. 主要发现与修订

| 类别 | 审阅发现 | 修订 |
|---|---|---|
| 迁移路径 | `render_project_report.py` 把 `docs/codex/diagrams` 的上两级误当仓库根，重绘时会读取错误目录 | 仓库根改为 `HERE.parents[2]`；功能档案固定读取 Codex 副本 |
| 代码规模 | 主报告仍写 `runtime/src 6.5k`、`docs 44.1k` | 按当前明确口径更新为项目 runtime 5.7k、`docs/` 顶层 43.8k |
| 文件计数 | 绘图脚本把 `test/README` 符号链接计作普通文件 | 排除符号链接，统一为 231 个一级普通文件 |
| 测试快照 | 技术全景仍使用 206/206、205+1；历史证据为 207/207 | 当前门禁更新为 LLVM 18 209/209、LLVM 17 208+1、Python 475/475；历史快照保留并显式标注 |
| 证据等级 | 证据等级图把 13 个不同 seed 的 LAVA-M 消融放在 B 级 | 改为 C 级机制描述；B 级只保留同配置多轮的 SymSan 六目标 |
| 复用表述 | `runtime-full` 打开 poly/prefix 路径，原文容易被理解为已观察到 context reuse 收益 | 补充 `poly_cache_hits=0`、`cross_prefix_hits=0`、prefix context `0/0`，并禁止单独归因 |
| 数字一致性 | 在线 bitmap 差值在不同位置精度不一致 | 按原始均值差 0.2265 统一四舍五入为 +0.227 pp |
| SOTA 边界 | “polyhedral context”“state merge”过度压缩论文方法 | 改为 Pangolin 的 polyhedral path abstraction/受约束采样，以及 Hydra 的 targeted control-flow transformation/failure-preserving 边界 |
| 迁移链接 | QA、图集 README 和证据命令仍引用 `docs/diagrams`、`docs/evidence` | 统一改为 `docs/codex/...`，仓库共享资料仍保留 `../` 引用 |
| 交付一致性 | Markdown 修订后 HTML、PPTX 内嵌图仍是旧版本 | 重新生成嵌入资源的 HTML，并替换 PPTX 的 24 张内嵌图及相关正文、讲者备注 |

## 3. 内容正确性复核

### 3.1 架构与执行流程

执行顺序已在主报告与 QA 中交叉核对：AFL 实例先启动，MPI worker 以 `TAG_READY` 拉取工作；
master 扫描、打分并分发内容对象；worker 运行 concolic 子进程、做字节摘要去重和
`afl-showmap` 重放；B2 进行 worker 本地预过滤；master 的 B1 进行会话级最终裁决。
B1/B2 属于 AFL edge ID 空间，B3 属于 QSYM 的 `site_id + taken` 哈希空间，二者不可互换。
B5 是 AFL 共享 map 中的数据进展 feature，B6 是结构化提议的新颖性空间。

### 3.2 约束与求解

文档保留三种编译形态的区别：宽整数比较适合 `fastSolveConcat`，逐字节短路链适合
multi-solve，保留的 `memcmp/strcmp` 调用依赖 libc wrapper/string 语义。LAF-Intel、
CmpLog、CTX/NGRAM 属于 AFL 变体，不应写成改变 SymCC 目标约束形态的开关。

求解流程不是“全部乐观”也不是固定串行栈：默认严格路径切片求解；只有严格
UNSAT/unknown 后才进入普通 optimistic fallback；含 ITE 的目标可先尝试 Backsolver。
丢弃前缀扩大可满足空间，却削弱目标分支可达性，因此只能生成 proposal，必须经过完整表达式
验证、真实目标重放和 AFL bitmap triage。

### 3.3 学术映射

“借鉴”与“完整复现”继续分开记录：

- [Pangolin](https://doi.org/10.1109/SP40000.2020.00063) 的原始方法包含声音的
  polyhedral path abstraction、受约束采样和增量求解；当前项目是有界实现子集；
- [CoFuzz](https://doi.org/10.1109/ICSE48619.2023.00045) 的统一评估是 15 个真实程序、
  24 h、5 次运行，并用双 CGF 实例对齐 hybrid 的两核预算；本项目没有复现其完整实验；
- [GenSym](https://doi.org/10.1109/ICSE48619.2023.00116) 的 continuation 编译思想只在
  当前明确列出的 LLVM 子集内实现；
- [Hydra](https://doi.org/10.1145/3798202) 采用 failure-preserving 而非完整
  semantics-preserving 目标；当前变换默认关闭并要求 baseline replay/manifest；
- Gordian、NeuroSCA 在当前仓库中只是 verifier-gated proposal 扩展点，不计作已实现 solver
  backend。

## 4. 实验结论复核

当前可汇报的 A 级工程统计是：同机、同当前二进制和初始语料，每组 20 个独立样本；
`4 AFL + 3 SymCC + basic profiles` 相对 `1 AFL + 6 SymCC + profiles off` 的 15 秒离线
候选并集边覆盖均值差为：

- libarchive：+0.953 pp，95% CI `[+0.582,+1.321]`，Holm p=0.00168；
- SQLite：+1.949 pp，95% CI `[+1.466,+2.441]`，Holm p=0.00032。

它是完整组合配置差异，不是单项技术贡献，也不是 AFL 在线 accepted corpus 的净收益。

LAVA-M `base64` 是 C 级机制描述。`runtime-full` 相对 strict-first baseline 的完整路径
Z3 调用减少 56.6%、候选增加 33.7%，但 solver query 增加 190.5%、solver time 增加
82.6%、墙钟约 53.3 倍、candidate/s 下降 97.5%，listed bug 并集仍为 41/44。三类上下文
复用命中均为 0，所以不能把变化归因于 context reuse，也不能写成提速或 bug 数提升。

## 5. 图形质量复核

图集保持白底、深色正文和蓝/青/橙/红/紫的语义配色；相同颜色在不同图中继续表达相同角色。
本轮重点优化：

- worker 执行图从狭长单列改成三行时序，保留 1–10 的严格次序与两条交付分支；
- `negatePath` 图改成分层决策树，strict、Backsolver、optimistic 与 replay 路径可直接追踪；
- 证据等级图将 LAVA-M 移到 C 级；
- LAVA-M 图增加三类复用零命中和“不可单独归因”的醒目标注；
- 工程规模图改为动态统计正确仓库根，排除 `test/README` 符号链接；
- 模块图更新 runtime/docs 规模，Pangolin/Hydra 图更新论文术语；
- HTML 报告改为 1180 px 阅读宽度，图片可使用完整正文宽度，并增加移动端和打印样式。

全部 46 张 SVG 均可解析，46 张 PNG 均通过签名与尺寸检查；代表性结构图、统计图和 HTML
首屏已做人工视觉检查，未发现裁切、文本重叠或空白渲染。

## 6. 当前验证

| 验证项 | 结果 |
|---|---|
| LLVM 18 lit | 209/209 passed，134.25 s |
| LLVM 17 lit | 208 passed + 1 unsupported，133.42 s |
| Python unittest | 475/475 passed，82.962 s |
| 图集 | 21 报告图 + 25 QA 图；SVG/PNG 均通过 |
| PPTX | ZIP 完整；28 slide XML、27 notes XML、24 内嵌 PNG |
| 功能与配置计数 | F00–F296 连续，共 297；375 个 `SYMCC_*` 名称 |

当前命令记录见
[`current_document_review_verification.md`](evidence/current-eval-2026-07-30/current_document_review_verification.md)；
交付机械检查可运行：

```bash
python3 docs/codex/verify_delivery.py
```

## 7. 仍需保留的研究边界

本轮审阅提高的是材料的准确性、完整性、可读性与可复算性，没有把现有工程证据升级为论文级
确认实验。当前仍缺少等 CPU-second、随机区组、多目标、长时、每格至少 20 次的 sealed
confirmatory campaign；也缺少 AFL 在线 foreign sync 闭环的净收益测量。报告中仍不得声称
“297 项技术分别带来固定百分比”“已达到或超过 SOTA”或“LAVA-M bug 数已因新求解技术提高”。
