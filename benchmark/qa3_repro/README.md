# QA3 实测复现脚本

[`docs/Architecture_QA3.md`](../../docs/Architecture_QA3.md) 中**表内列出的已脚本化实测**
由这里的脚本产出。§4.3–§4.6 的合成 magic/长度/checksum 实验目前只有文中命令和结果，
尚无独立 driver；历史 `/tmp` 语料与已提交 CSV 也不由本目录重新生成。

所有脚本默认把工作目录放在 `${WORK:-/tmp/symcc_qa3}`，不污染仓库。

## 前置条件

- 已构建的 SymCC：`build/symcc`、`build/SymCCRuntime-prefix/src/SymCCRuntime-build/libsymcc-rt.so`
- AFL++（`afl-fuzz`、`afl-showmap`、`afl-clang-fast` 在 `PATH` 里）
- 已构建的公开目标：`benchmark/public/bin/`（见 `benchmark/compile_public_benchmarks.sh`）
- `mpirun` + `mpi4py`（仅 `funnel.sh` 需要）

## 脚本

| 脚本 | 对应章节 | 产出 |
|---|---|---|
| `measure_strategy.sh` | §3.3 求解策略占比 | 各目标的 nominal / optimistic / fast / group 产出计数 + telemetry |
| `landing.py` | §3.4 | 各策略产出相对种子的"落地率" |
| `landing2.py` | §3.4 | 各策略的**独有新边**与"边/产出"，即互补性 |
| `iterate.py` | §3.5 | 单一策略下的嵌套深度迭代（前沿 FIFO） |
| `iterate2.py` | §3.5 | **FIFO vs 覆盖率制导**对照——本文最关键的一组对照 |
| `qa3_common.py` | §3.4--§3.5 | 严格 showmap/telemetry 解析、流式兼容性探针、交错复测和稳定性策略 |
| `../run_qa3_coverage_campaign.py` | §3.4 | 一次 campaign 同源生成 landing、互补性、逐轮边集、哈希和证据清单 |
| `funnel.sh` | §1.6 | 端到端漏斗（generated → reported → accepted）+ worker 相位计时 + master PROF |
| `persab.sh` | §6.6 | 持久模式 A/B：同一二进制带/不带 `@@` 的 execs/s 与 bitmap 覆盖率 |
| `trace_base64.sh` | §4.9 | 真实 LAVA-M base64 的两类种子 DSE 遥测、策略产出与内容去重 |
| `inject_test.sh` | §1.10 | 受控对照：运行中的 afl-fuzz 会不会捡走直接丢进它自己 `queue/` 的文件（结论：不会） |

## 用法

```bash
cd <symcc 根目录>

# §3.3 求解策略占比（默认配置）——对每个目标各跑一次
W=/tmp/symcc_qa3/strategy && mkdir -p $W
benchmark/qa3_repro/measure_strategy.sh $W xml \
    benchmark/public/bin/google-fts/xml_read_fuzzer \
    benchmark/public/seeds/google-fts/xml_read_fuzzer
# 打开开关看占比怎么变：把 SYMCC_FAST_SOLVE=1 / SYMCC_OPTIMISTIC_FIRST=1 /
# SYMCC_MULTI_SOLVE=1 作为第 5 个参数传进去

# §3.4 推荐：一次 campaign 同源计算落地率和边贡献互补性
python3 benchmark/run_qa3_coverage_campaign.py \
    --afl-binary benchmark/public/bin/google-fts-afl/xml_read_fuzzer \
    --corpus $W/xml/out \
    --seed benchmark/public/seeds/google-fts/xml_read_fuzzer/seed_01.xml \
    --output /tmp/qa3-coverage-evidence \
    --rounds 3 --stability-policy strict --replica-delay-ms 1000

# 快速终端摘要；landing.py 与 landing2.py 各自执行独立 campaign，不能跨脚本
# 拼接为同源归因结论。正式证据优先使用上面的统一 driver。
python3 benchmark/qa3_repro/landing2.py \
    benchmark/public/bin/google-fts-afl/xml_read_fuzzer \
    $W/xml/out \
    benchmark/public/seeds/google-fts/xml_read_fuzzer/seed_01.xml

# §3.5 嵌套深度：FIFO vs 覆盖率制导（需先生成嵌套目标，见下）
python3 benchmark/qa3_repro/iterate2.py ./nested32 ./nested32_afl 32 60 fifo 64
python3 benchmark/qa3_repro/iterate2.py ./nested32 ./nested32_afl 32 60 cov  64

# §1.6 端到端漏斗 + 相位计时（约 4 分钟）
WARMUP=25 DUR=180 benchmark/qa3_repro/funnel.sh

# §6.6 持久模式 A/B（约 4 分钟）
DUR=60 benchmark/qa3_repro/persab.sh

# §4.9 真实目标的两类种子追踪（产物写入唯一的 /tmp 子目录）
benchmark/qa3_repro/trace_base64.sh

# §1.10 直注是否有效的受控对照（约 3 分钟）
benchmark/qa3_repro/inject_test.sh
```

### 生成 §3.5 用的嵌套目标

```bash
python3 - <<'PY'
for d in (4, 8, 16, 32):
    conds = "\n".join(f"  if (b[{i}] != (char)({0x41+i})) return {i};" for i in range(d))
    open(f"nested{d}.c", "w").write(f"""#include <stdio.h>
#include <stdlib.h>
int main(int argc, char **argv) {{
  char b[64] = {{0}};
  FILE *f = fopen(argv[1], "rb");
  if (!f) return 100;
  if (fread(b, 1, {d}, f) != {d}) {{ fclose(f); return 101; }}
  fclose(f);
{conds}
  printf("DEPTH{d} SOLVED\\n");
  return 200;
}}
""")
PY
for d in 4 8 16 32; do
  build/symcc      -O0 -o nested$d      nested$d.c
  afl-clang-fast   -O0 -o nested${d}_afl nested$d.c
done
```

## 注意事项

- **持久模式目标不能带 `@@`。** 先用 `grep -a '##SIG_AFL_PERSISTENT##' <bin>` 判断；带错了
  AFL 会跑得飞快但 bitmap 覆盖率接近 0（`persab.sh` 就是量化这一点的）。
- `funnel.sh` 需要 `SYMCC_WORKER_PROFILE=1`（脚本内已设）才会写 `redun_*.csv` /
  `phase_timing_rank*.csv`。
- 当前 `_snap` 已改为读取 worker 的 AFL coverage 副本，并按 `(edge_id,count)` 的
  `edge_id` 比较；旧实现读 QSYM 图且会触发 `tuple >= int`。复现实验必须记录所用 commit/
  dirty diff，不能把修复前后的 `redun_*.csv` 混合。
- coverage 脚本默认对每个输入执行 3 轮循环位移交错复测，轮间隔 1 秒，并采用
  `strict`：任一 edge-ID 漂移都退出 2。`intersection` 是需要显式选择的保守欠近似，
  `union` 只表示“曾观察到”，不能当作稳定覆盖。
- campaign 第一次测量会比较 AFL++ 流式 `-S` 与隔离 one-shot 的状态和完整 edge-ID
  集合；不一致时整个 campaign 统一回退 one-shot，禁止混合 oracle 语义。
- `run_qa3_coverage_campaign.py` 自动保存逐轮原始边集、输入/目标/showmap SHA-256、
  汇总 JSON 和目录清单。它仍不会记录主仓库/子模块 commit、dirty diff、CPU 绑定以及
  LLVM/AFL++/Z3 版本，正式封存需在外层实验 manifest 中补齐。
- 结果随目标、种子、机器负载变化。历史文档里的单次数字**不是等 CPU 多轮确认性结果**；
  F306 之后的正式 coverage 归因必须保留统一 driver 的 raw campaign，评级规则见
  `docs/README.md`。
