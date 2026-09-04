# F428 可验证 QF_BV learned lemma 交换证据

本目录封存 F428 的原始测试、两次独立 cvc5/Ethos oracle、有限域语义 oracle、工具链身份、
源码合同、审查记录、图示和声明边界。`SHA256SUMS.txt` 完整枚举目录内除自身外的全部普通文件；
交付校验器同时复核清单、原始测试规模、稳定语义字段，并以新临时目录重新执行真实 oracle。

## 证据结论

- capability-closed Python：1,174 passed，273 subtests passed，零
  skip/xfail/xpass/deselection，node-ID 清单 1,174/1,174 精确一致；
- F428 定向：7 passed 加 13 subtests；QF_BV/QueryStore/F426--F428 关联：
  70 passed 加 45 subtests；
- LLVM 17：310 passed、2 expected unsupported；LLVM 18：311 passed、
  1 expected unsupported；双版本 F428 定向 lit 均为 1/1；
- 两次真实 oracle 均产生 1 个经 CPC/Ethos 验证的候选、发布 1 个 record，并由 5/5
  fresh consumers 注入；sibling、错误 lemma、record 篡改和 proof 篡改均被拒绝，误授权为 0；
- 独立有限域 oracle 穷举 65,536 个 `(x,y)` 赋值：有效 lemma 反例为 0，故意错误 lemma
  反例为 1；
- F426 首次并发 SQLite 初始化竞态修复后，4 worker 同启压力复验为 200/200。

以上结论只证明协议语义、机制可执行性和回归完整性。当前一次性候选提取会增加 cvc5 replay 与
CPC/Ethos 检查成本；本证据不构成覆盖率、吞吐、time-to-bug 或公开 benchmark 性能提升结论。

## 文件说明

| 文件 | 内容 |
| --- | --- |
| `oracle-run-1.json`, `oracle-run-2.json` | 两次独立真实 cvc5/Ethos 与有限域 oracle |
| `targeted-python.xml`, `related-python.xml` | 定向与关联 pytest JUnit 原始结果 |
| `full-python-gate.json` | 能力、测试结果和 canonical node-ID 门禁 |
| `targeted-lit-llvm17.json`, `targeted-lit-llvm18.json` | 双 LLVM 版本定向 lit 原始结果 |
| `llvm17-full.json`, `llvm18-full.json` | 双 LLVM 版本完整 lit 原始结果 |
| `full-suite-summary.json` | 从原始门禁提取的保守汇总 |
| `context-initialization-stress.txt` | F426 并发初始化修复的 200 轮复验 |
| `environment.txt` | Python、LLVM、cvc5、Ethos 与主机身份 |
| `source-contract.txt` | F428 权威源码、测试与 oracle SHA-256 |
| `source-research.txt` | 一手技术来源、迁移边界及检索日期 |
| `review-findings.txt` | 五轮 review 的问题、修复与复验 |
| `claim-boundary.txt` | 本证据支持与不支持的结论 |
| `static-checks.txt` | Ruff、py_compile、index 和 diff 门禁 |
| `oracle-reproducibility.txt` | 真实 oracle 的命令、稳定字段与非稳定计时字段 |
| `f428_qfbv_verified_lemma_exchange.svg`, `.png` | 可验证 lemma 交换闭环示意图 |

## 复现

```bash
benchmark/install_cvc5_cpc_ethos_1_3_4.sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q \
  test/test_qfbv_lemma_exchange.py -ra
python3 benchmark/check_qfbv_lemma_exchange_oracles.py --repetitions 5
python3 util/python_test_gate.py --output /tmp/f428-python-gate.json \
  --require-nodeid-manifest test/pytest-nodeids.json -- -q -W error
```

协议、执行次序、资源边界、数据解释和后续研究见
[`Verified_QFBV_Lemma_Exchange_F428_2026-08-17.md`](../../research-progress/Verified_QFBV_Lemma_Exchange_F428_2026-08-17.md)。
