# F427 QF_BV UNSAT 证明回执证据

本目录封存 F427 的原始测试、真实 cvc5/Ethos oracle、工具链身份、源码合同、图示和声明边界。
所有普通文件均由 `SHA256SUMS.txt` 完整枚举；交付校验器还会重新执行真实 oracle，并验证两次
封存运行的语义字段一致。

## 证据结论

- capability-closed Python：1,167 passed，260 subtests passed，零 skip/xfail/xpass/deselection，
  node-ID 清单精确一致；
- F427 定向：14 passed 加 7 subtests；QF_BV/QueryStore/F426/F427 关联：63 passed 加 32 subtests；
- LLVM 17：309 passed、2 expected unsupported；LLVM 18：310 passed、1 expected unsupported；
- 两次真实 oracle 均得到 1 次生成并检查的 UNSAT、5/5 跨 worker 复用、1/1 reference 篡改拒绝、
  1/1 CAS 篡改拒绝、0 误授权；
- 固定工具链为 cvc5 1.3.4 `f3b21c4`、Ethos 与 51 个 CPC signature 文件。

## 文件说明

| 文件 | 内容 |
| --- | --- |
| `oracle-run-1.json`, `oracle-run-2.json` | 独立进程的真实 cvc5/Ethos 运行及机制耗时 |
| `targeted-python.xml`, `related-python.xml` | 定向与关联 pytest JUnit |
| `full-python-gate.json` | 能力、测试结果和 node-ID 身份门禁 |
| `targeted-lit-llvm17.json`, `targeted-lit-llvm18.json` | 双版本 F427 定向 lit |
| `llvm17-full.json`, `llvm18-full.json` | 双版本完整 lit 原始 JSON |
| `full-suite-summary.json` | 从原始门禁提取的汇总 |
| `environment.txt` | 主机、语言、LLVM 和 proof toolchain 版本 |
| `source-contract.txt` | F427 权威源码和测试文件 SHA-256 |
| `source-research.txt` | 直接技术来源及检索日期 |
| `review-findings.txt` | 四轮 review 的问题、修复与复验 |
| `claim-boundary.txt` | 可以与不可以从本证据推出的结论 |
| `static-checks.txt` | Ruff、py_compile、shell 与差异格式门禁 |
| `oracle-reproducibility.txt` | 两次真实 oracle 的复现命令和稳定字段 |
| `f427_qfbv_proof_receipt.svg`, `.png` | 可独立检查回执的执行闭环图 |

## 复现

```bash
benchmark/install_cvc5_cpc_ethos_1_3_4.sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q \
  test/test_qfbv_proof_receipt.py -ra
python3 benchmark/check_qfbv_proof_receipt_oracles.py --repetitions 5
```

完整命令、协议和结果解释见
[`Proof_Carrying_QFBV_UNSAT_Receipts_F427_2026-08-17.md`](../../research-progress/Proof_Carrying_QFBV_UNSAT_Receipts_F427_2026-08-17.md)。

