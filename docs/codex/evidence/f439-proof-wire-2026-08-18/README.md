# F439 LIDRUP / PalRUP Proof-Wire 互操作证据

- 证据日期：2026-08-18
- 功能：F439
- 证据等级：I/T/E-local
- 结论边界：LIDRUP 已接入生产 UNSAT 授权链；PalRUP 只证明 fragment 语法互操作，不证明多 worker 全局 UNSAT

## 证据组成

| 文件 | 证明内容 |
| --- | --- |
| `official_oracle.json` | 固定版本 LIDRUP checker 与 PalRUP converter 的非跳过官方工具结果 |
| `focused_tests.txt` | proof-wire 专项测试，含格式、篡改、checker、sidecar、生产接线和 PalRUP codec |
| `associated_tests.txt` | incremental proof、proof-wire、QueryStore 关联回归 |
| `full_python_gate.json` | 完整 capability、结果计数和 canonical node-ID inventory |
| `full_python_tests.txt` | 1320 个 Python 测试的原始门禁摘要 |
| `llvm17_lit.txt`、`llvm18_lit.txt` | 两个 LLVM build tree 的完整串行 lit 输出 |
| `static_checks.txt` | 编译、lint、installer shell、索引和 diff 静态门禁 |
| `source_manifest.txt` | F439 权威源码、测试、文档和图的 SHA-256 合同 |
| `review_findings.txt` | 五轮 review、发现、修复与剩余风险 |
| `research_sources.md` | 一手论文、工具与固定源码版本 |
| `full_suite_summary.json` | 所有门禁的机器可读汇总及严格 claim boundary |
| `delivery_verifier.txt` | `docs/codex/verify_delivery.py` 的完整交付验证输出 |
| `f439_proof_wire_interoperability.*` | 与主文档逐字节一致的架构图 |

`SHA256SUMS.txt` 覆盖目录内除其自身外的每个常规文件。交付验证器会重算该清单、oracle canonical digest、
固定工具身份、测试计数、图像身份、文档合同和生产接线，不接受仅凭 README 自证。
