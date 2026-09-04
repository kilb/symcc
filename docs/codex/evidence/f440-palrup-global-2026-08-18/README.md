# F440 PalRUP 全局确认流水线证据

- 日期：2026-08-18
- 功能：F440
- 等级：I/T/E-local
- 结论：固定 SAT 2026 官方 checker 的 `N local -> ceil(sqrt(N))^2 redistribute -> N confirm` 机制已形成失败闭锁的库、CLI 与全局回执，并由官方 12-fragment fixture 独立重跑验证
- 边界：SymCC 尚不原生生成完整 PalRUP fragments；没有跨节点恢复、solver speedup、coverage、defect-yield 或多节点 scalability 结论

## 文件说明

| 文件 | 内容 |
| --- | --- |
| `official_oracle.json` | 固定 commit、三工具 content SHA 和官方 `r3unsat_200` 的完整两次 pipeline 结果 |
| `focused_tests.txt` | F440 专项正向/故障注入测试 |
| `associated_tests.txt` | F439 proof-wire、F440 pipeline、Python gate 与 discovery contract 关联门禁 |
| `inventory_rebuild.txt` | canonical pytest node-ID 清单重建结果 |
| `full_python_gate.json` | capability-closed 完整 Python 门禁的机器可读结果 |
| `full_python_tests.txt` | 完整 Python 门禁原始控制台输出 |
| `llvm17_lit.txt` / `llvm18_lit.txt` | 两个 build tree 串行完整 lit 输出 |
| `full_suite_summary.json` | 所有门禁、官方 oracle 与严格边界的汇总 |
| `static_checks.txt` | 语法、lint、安装脚本、索引、文档和 diff 静态门禁 |
| `review_findings.txt` | 五轮 review 的发现、修复和复验 |
| `research_sources.md` | 论文、官方源码及采用合同 |
| `source_manifest.txt` | F440 权威源码、测试、文档和图的 SHA-256 合同 |
| `f440_palrup_global_confirmation.svg/.png` | 人工布局的全局确认机制图与渲染图 |

`SHA256SUMS.txt` 覆盖本目录除自身和 `delivery_verifier.txt` 外的全部权威证据。`delivery_verifier.txt`
由它所记录的 verifier 自身产生，为避免局部清单自引用循环，由顶层 `docs/codex/SHA256SUMS.txt` 封印。

## 关键实测

- 专项：6 passed + 7 subtests；
- 关联：30 passed + 19 subtests；
- Python：1326 passed + 310 subtests，node-ID 1326/1326；
- LLVM 17：330 discovered，328 passed，2 expected unsupported；
- LLVM 18：330 discovered，329 passed，1 expected unsupported；
- 官方 PalRUP：12 fragments / 8,452,282 bytes，12 + 16 + 12 tasks，12/12 confirmed，witness rank 3，40 stage artifacts / 8,644 bytes；
- 所有门禁 exit 0，Python 无 skip、xfail、xpass、deselect、collection error 或 inventory drift。

## 复现入口

```bash
bash benchmark/install_palrup_check_sat2026.sh
python3 benchmark/check_qfbv_palrup_global_oracles.py \
  --source-root /path/to/PalRUP-Check-at-d9382fb4 \
  --output /tmp/f440-palrup-global-oracle.json
```

完整命令合同见 `docs/Testing.txt`、`docs/Configuration.txt` 与
`docs/codex/research-progress/PalRUP_Global_Confirmation_Pipeline_F440_2026-08-18.md`。
