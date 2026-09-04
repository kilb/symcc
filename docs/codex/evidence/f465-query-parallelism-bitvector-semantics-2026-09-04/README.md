# F465验证证据

本目录保存第五轮深度审查的机器可读门禁结果。证据级别为I/T/E-mechanism；它验证实现和反例，
不构成公共目标coverage、solver speedup或defect-yield结论。

## 冻结结果

- `full-python-gate.json`：1671/1671 node ID，1671 passed加638 subtests，330.19秒；
  skip、xfail、xpass、deselect、missing和unexpected均为0。
- 文件SHA-256：`cfac39513bc2d860687db6d0a318fdd917616b2fd6993e6137f4f45d7404d731`。
- LLVM 18/QSYM：350项中349 passed、1 unsupported，328.85秒。
- LLVM 17/QSYM：排除provenance的349项中347 passed、2 unsupported，290.97秒；
  `test_research_protocol.py`隔离1 passed，52.07秒。
- QueryStore：41 passed加50 subtests；AFL编排：77 passed加42 subtests；QF_BV冻结
  conformance artifact：5 passed；data coverage原生测试：5 passed。

## 复现命令

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 \
python3 util/python_test_gate.py \
  --output full-python-gate.json \
  --min-collected 1597 \
  --max-skips 0 --max-xfails 0 --max-xpasses 0 --max-deselected 0 \
  --max-missing-nodeids 0 --max-unexpected-nodeids 0 \
  --require-nodeid-manifest test/pytest-nodeids.json \
  --require-command cc --require-command z3 --require-command cvc5 \
  --require-command bitwuzla --require-command afl-clang-fast \
  --require-command afl-showmap --require-command mpiexec \
  --require-command openssl --require-command opt --require-command llvm-diff \
  --require-module mpi4py --require-module tree_sitter \
  --require-module tree_sitter_json --require-module lark \
  --require-module parglare --require-library z3 \
  -- -q -W error -p no:cacheprovider

python3 /usr/lib/llvm-18/build/utils/lit/lit.py -sv build/test -j 32
python3 /usr/lib/llvm-17/build/utils/lit/lit.py -sv \
  --filter-out test_research_protocol.py build-llvm17/test -j 32
python3 /usr/lib/llvm-17/build/utils/lit/lit.py -sv \
  --filter test_research_protocol.py build-llvm17/test -j 1
```

LLVM 17的provenance项读取工作树身份，因此与会产生临时产物的主套件隔离执行。第一次LLVM 18
低并发试跑被主动终止，不计入冻结结果；表中只记录完整结束且退出码为0的运行。

## 文档验证残余

当前source-delivery gate在提交后的clean `HEAD`上通过：714个普通文件、1个runtime gitlink，
tree SHA-256为`98b78af0290d9188f199c91c9945c10d8d6624bd36a45b870d30efb001114333`。
单体`docs/codex/verify_delivery.py`中的链接、SVG和PNG检查通过，但总结果仍有15项失败：固定的
Configuration变量数/test文件数/F360规则已过时，F400--F456的10个历史source contract仍拿
冻结摘要比较当前演进源码，另有F456 source manifest和旧顶层交付包摘要不匹配。这里不重签历史
证据，也不把该脚本报告成通过；后续应把“历史artifact自洽”与“当前HEAD源码一致”拆成两个门禁。
