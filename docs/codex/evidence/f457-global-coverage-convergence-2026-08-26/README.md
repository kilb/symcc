# F457 验证证据

本目录保存 2026-08-26 在 `/home/ubuntu/code/symcc` 对最终实现执行的原始验证输出。

## 文件

- `targeted-pytest.txt`：四个直接相关模块，316 passed、104 subtests；
- `full-pytest.txt`：项目默认完整 `pytest` 门禁，最终计数见文件尾；
- `static-checks.txt`：Python 编译、严格错误类 Ruff、diff whitespace、SVG/XML 与本地文档链接检查；
- `environment.txt`：解释器、pytest、Ruff、平台和提交/工作区身份；
- `SHA256SUMS.txt`：本目录证据摘要。

## 命令

```bash
python -m pytest -q \
  test/test_mpi_lifecycle.py \
  test/test_distributed_state.py \
  test/test_afl_profile_orchestration.py \
  test/test_adaptive_components.py

python -m pytest -q

python -m py_compile \
  util/mpi_fuzzing_helper.py util/distributed_state.py \
  util/adaptive_components.py benchmark/run_benchmark.py

python -m ruff check --select E9,F63,F7,F82 \
  util/mpi_fuzzing_helper.py util/distributed_state.py \
  util/adaptive_components.py benchmark/run_benchmark.py
```

## 声明边界

这些制品证明本地协议和回归状态，不是公开 benchmark 的覆盖率或速度实验。工作区包含本轮以前的大量未提交研究实现，`git` 身份只用于说明测试对象，不能被解释为单一提交的干净复现环境。

