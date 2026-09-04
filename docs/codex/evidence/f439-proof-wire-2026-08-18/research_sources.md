# F439 一手研究与工具来源

核对日期：2026-08-18。论文结论用于界定协议目标；本项目结果只由本目录证据支持。

1. **Real-time Proof Checking for Distributed Incremental SAT Solving**, TACAS 2026.
   DOI: <https://doi.org/10.1007/978-3-032-22752-2_18>
   用于核对增量交互、实时独立检查和 LIDRUP/ImpCheck 的信任边界。
2. **A Natively Parallel Proof Framework for Clause-Sharing SAT Solving**, SAT 2026.
   DOI: <https://doi.org/10.4230/LIPIcs.SAT.2026.17>
   用于核对 PalRUP 并行 proof files、去中心化检查和全局 confirmation 的完成定义。
3. **Problem Partitioning via Proof Prefixes**, SAT 2025.
   DOI: <https://doi.org/10.4230/LIPIcs.SAT.2025.3>
   用于界定下一阶段 proof-prefix 完备、不交叠和可组合验证能力。
4. **Certifying Incremental SAT Solving**, LPAR 2024.
   DOI: <https://doi.org/10.29007/pdcc>
   用于核对 incremental additions/deletions、assumption scope 和证明格式语义。
5. **lidrup-check 0.0.7 官方源码**，固定 commit
   `3ae8c23cd978c313ee14472327bf0f9560601015`。
   <https://github.com/dominikschreiber/lidrup-check>
6. **PalRUP-Check 官方源码**，固定 SAT 2026 artifact commit
   `d9382fb4b0acf094034ee91e2ed0a22b1b479c1d`。
   <https://github.com/rubenGoetz/PalRUP-Check>

固定工具由仓库中的安装脚本从指定 commit 构建，并在运行前同时检查 `source-commit` 文件和可执行文件
SHA-256。官方工具的论文规模结果不作为本项目的性能数据。
