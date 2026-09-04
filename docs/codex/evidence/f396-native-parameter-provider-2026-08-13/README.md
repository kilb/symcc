# F396 Native Parameter-Provider Evidence

本目录封存 F396 executable parameter discovery、provider-atomic registry、scope
routing 和 schema-bound state 的可复核证据。

## 结论口径

- `symcc-query-solver` 在 LLVM 18/17 build 中均输出同一
  `symcc-parameter-provider-v1`：23 项 query-service 参数；
- coordinator-only registry 是 40 项；原生合并后 53 项，其中 28 task、23
  query-service、2 coordinator-campaign；
- 100 次独立 provider 启动全部成功，provider digest 和 registry hash 各只有一个，
  errors/conflicts 为 0；中位 1887.812 us，P95 2331.673 us，最大 2518.865 us；
- Python 定向 15 passed；LLVM 17/18 原生 provider 定向各 1/1；
- capability-closed Python gate 为 960 passed + 235 subtests，960/960 node IDs；
- LLVM 18 完整 lit 双复跑均为 257 passed + 1 个既有 unsupported；
- 这只证明机制正确性、稳定性和本机一次性启动成本，不证明 ParaSuit 论文完整
  value-space/silhouette 算法、coverage、bug-yield 或符号执行 speedup。

## 文件

| 文件 | 内容 |
| --- | --- |
| `provider-llvm18.json` / `provider-llvm17.json` | 两套真实 solver binary 的原生参数合同 |
| `mechanism-benchmark.json` | 10 warmup + 100 独立发现的 scope、hash 和 latency |
| `targeted-python.txt` | 15 项协议/反例/state/lifecycle 单元测试 |
| `targeted-lit-llvm18.txt` / `targeted-lit-llvm17.txt` | 两代 LLVM 真实 binary 协议与 registry 对拍 |
| `full-python-gate.json` | capability、完整 pytest 结果和 canonical node-ID gate |
| `full-lit-first.txt` / `full-lit-second.txt` | 两轮完整 LLVM 18 lit 原始输出 |
| `pytest-inventory.txt` | 960 项 canonical collection 输出 |
| `static-checks.txt` | Ruff、format、py_compile、whitespace 结果与 clang-format 可用性 |
| `environment.txt` | 测试主机和工具版本 |
| `source-contract.txt` | 直接实现/测试/benchmark 文件 SHA-256 |
| `upstream-revision.txt` | ParaSuit 官方页面、仓库和审计 revision |
| `SHA256SUMS.txt` | 本目录其余文件的本地完整性清单 |

## 独立复核

```bash
sha256sum -c SHA256SUMS.txt

python3 benchmark/benchmark_self_config_provider.py \
  --provider build/SymCCRuntime-prefix/src/SymCCRuntime-build/src/backends/qsym/symcc-query-solver \
  --iterations 100 --warmup 10

lit -v --filter self_config_native_provider build/test
lit -v --filter self_config_native_provider build-llvm17/test
python3 -m pytest -q test/test_self_config.py
```

完整回归命令及 capability 集合由 `full-python-gate.json`、`full-lit-first.txt` 和
`full-lit-second.txt` 原样保存。
