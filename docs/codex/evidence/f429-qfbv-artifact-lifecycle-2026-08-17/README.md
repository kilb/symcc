# F429 QF_BV 制品生命周期与依赖感知 GC 证据

本目录封存 F429 的原始测试、两次独立 cvc5/Ethos oracle、有限图 oracle、双 LLVM
完整门禁、源码合同、审查记录、机制图和声明边界。`SHA256SUMS.txt` 枚举除自身外的全部
普通文件；交付校验器还会重算清单、原始计数和稳定语义字段，并在新临时目录执行真实 oracle。

## 证据结论

- capability-closed Python：1,197 passed、273 subtests passed，零
  skip/xfail/xpass/deselection/collection error，node-ID 为 1,197/1,197 精确一致；
- F429 定向：22 passed；F426--F429 四类 store 关联：52 passed、20 subtests passed；
- LLVM 17：311 passed、2 expected unsupported；LLVM 18：312 passed、
  1 expected unsupported；双版本 F429 定向 lit 均为 1/1；
- 两轮独立 oracle 各检查 64 个 DAG、1,024 个节点，误删、漏删和依赖顺序错误均为 0；
- 真实链包含 2 个 context、1 个 proof、1 个 receipt 和 1 个 lemma。活动租约保护 5/5，
  释放后删除 5/5 和 12,169 B，旧 generation 被拒绝，publish-before-index orphan 被回收；
- 两次 oracle 的授权与回收字段相同，仅 `elapsed_us` 不同。

以上结论只证明生命周期协议、图闭包、fencing、崩溃恢复和回归完整性。464,704 us 与
436,942 us 包含 proof/lemma 构造、同步、mark 和 deletion，不是 solver、coverage、吞吐、
time-to-bug 或公开 benchmark speedup。

## 文件说明

| 文件 | 内容 |
| --- | --- |
| `oracle-run-1.json`, `oracle-run-2.json` | 双轮有限图与真实 cvc5/Ethos 生命周期 oracle |
| `targeted-python.xml`, `related-python.xml` | 定向与关联 pytest JUnit 原始结果 |
| `full-python-gate.json` | capability、outcome 与 canonical node-ID 门禁 |
| `targeted-lit-llvm17.json`, `targeted-lit-llvm18.json` | 双 LLVM 定向 lit 原始结果 |
| `llvm17-full.json`, `llvm18-full.json` | 双 LLVM 完整 lit 原始结果 |
| `full-suite-summary.json` | 从上述原始结果提取的保守汇总 |
| `environment.txt` | Python、LLVM、cvc5、Ethos 与主机身份 |
| `source-contract.txt` | F429 权威源码、测试与 oracle SHA-256 |
| `source-research.txt` | 一手技术来源、迁移范围与检索日期 |
| `review-findings.txt` | 六轮反例驱动 review、修复与复验 |
| `claim-boundary.txt` | 本证据支持与不支持的结论 |
| `static-checks.txt` | Ruff、py_compile、index 与 diff 门禁 |
| `oracle-reproducibility.txt` | oracle 命令、稳定字段与非稳定计时字段 |
| `f429_qfbv_artifact_lifecycle.svg`, `.png` | 生命周期与依赖感知 GC 示意图 |

## 复现

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -W error \
  test/test_qfbv_artifact_lifecycle.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -W error \
  test/test_qfbv_artifact_lifecycle.py test/test_qfbv_lemma_exchange.py \
  test/test_qfbv_proof_receipt.py test/test_cross_worker_context.py
python3 benchmark/check_qfbv_artifact_lifecycle_oracles.py \
  --graphs 64 --output /tmp/f429-oracle.json
python3 util/python_test_gate.py --output /tmp/f429-python-gate.json \
  --require-nodeid-manifest test/pytest-nodeids.json -- -q -W error
lit -sv --path=/usr/lib/llvm-17/bin build-llvm17/test
lit -sv --path=/usr/lib/llvm-18/bin build/test
```

协议、执行次序、资源边界和后续研究见
[`Lease_Fenced_QFBV_Artifact_Lifecycle_F429_2026-08-17.md`](../../research-progress/Lease_Fenced_QFBV_Artifact_Lifecycle_F429_2026-08-17.md)。
