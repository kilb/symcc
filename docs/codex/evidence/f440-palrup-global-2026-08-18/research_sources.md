# F440 一级研究与实现来源

## 1. PalRUP 论文

Ruben Götz、Benjamin Dörr、Dominik Schreiber，*A Natively Parallel Proof Framework for Clause-Sharing
SAT Solving*，SAT 2026，LIPIcs 17。

- DOI：<https://doi.org/10.4230/LIPIcs.SAT.2026.17>
- 采用：并行 proof fragments、局部检查、通信重分发、最终确认和持久并行检查的完成定义。
- 边界：论文报告的 3072-core 结果不是本项目数据，不进入 F440 性能结论。

## 2. PalRUP-Check 官方 artifact

- 仓库：<https://github.com/rubenGoetz/PalRUP-Check>
- 固定 commit：`d9382fb4b0acf094034ee91e2ed0a22b1b479c1d`
- 核对文件：`README.md`、`test/test_full_run.c`、`scripts/pal/pal.sh`、三个 executable 的构建目标及 `r3unsat_200` fixture。
- 采用：直接运行 `palrup_local_check`、`palrup_redistribute`、`palrup_confirm`，不在项目中重写 proof-checking semantics。

固定 commit 的 README 将 redistribute process 数简写为 `ceil(sqrt(N))`，但官方集成测试把
`comm_size` 设置为 `ceil(sqrt(N))^2`，`pal.sh` 也为方阵所有 cells（含 padding）创建 import。F440 以
可运行源码和官方 fixture 共同验证的 `w^2` 合同为准，并在报告中公开记录该歧义。

官方安装工具 SHA-256：

- local：`5359913eb4b8430a766ce383e0da1cb69238aa9f1c36de15afc6b3a616c182ee`
- redistribute：`9005b1290069681b50d4fd4564239a1a799287b01d199162ba67c3147a2d4494`
- confirm：`4a7501b3cde76d2e40cfe9e417275dd3f1e8f9d9b78f87e8575a755ac889cc72`

## 3. ImpCheck 分布式增量证明检查

*Real-time Proof Checking for Distributed Incremental SAT Solving*，TACAS 2026。

- 论文：<https://satres.kikit.kit.edu/papers/2026-tacas-distrincproof.pdf>
- 采用：solver/checker 解耦和独立确认的设计原则。
- 差异：F440 是完整 PalRUP proof 产生后的三阶段 checker，不声称实现 ImpCheck 的实时 LIDRUP 消息流；
  项目求解中实时 checked stream 属于 F433。

## 4. 本项目结论边界

一级来源支撑“PalRUP 官方检查机制及其任务/目录合同”。本证据只增加单机共享文件系统上的机制复现和
工程化授权边界。原生 SymCC proof generation、跨节点 transport/recovery、公开 benchmark speedup、
coverage 与 defect yield 均需单独实现和 R 级实验。
