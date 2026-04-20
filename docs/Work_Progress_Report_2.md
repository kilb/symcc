# SymCC MPI 并行符号执行 — 工作进展报告（二）

## 上期回顾

上一阶段完成了并行框架的核心开发与优化工作：实现了 MPI 纯并行和 Hybrid AFL+SymCC 两种并行模式；通过 6 轮 profiling 驱动优化将 Master 负载降至 9%；修复了 20+ 个 bug；在 7 个目标上完成了初步 benchmark。确认了三个核心结论：（1）吞吐量 scaling 优秀（84-117x at np=128），（2）Hybrid 模式覆盖率最高，（3）覆盖率不随并行度线性增长（concolic execution 的固有限制）。

**本期目标**：全量测试所有目标、深入分析异常案例、扩展测试范围、集成 AFL++ CmpLog。

---

## 核心发现

本期最重要的结论是：**SymCC 的有效性高度依赖目标程序的特征**。通过对 10 个不同类型目标的系统测试，我们将 SymCC 的有效性分为三类：

| 类别 | 目标 | 特征 | SymCC 对覆盖率的贡献 |
|:----:|------|------|-----------|
| **主力** | base64 | 含 magic number 约束 | MPI 比种子高 +11.7% |
| **有效** | libarchive, png, pcre2, xml, SQLite | 格式头、混合结构 | Hybrid 比 AFL 高 1-6% |
| **无效** | who, freetype2, md5sum, uniq | 平坦二进制、glibc API 绕过 | 0 或负面影响 |

另一个重要发现：集成 AFL++ 的 CmpLog 功能后，AFL 在 SQLite 上的覆盖率（21.03%）**超过了** Hybrid 模式（18.72%）。这意味着对于文本解析器，CmpLog 的自动字典提取比 SymCC 的约束求解更有效，SymCC workers 反而成为负担。

---

## 一、本期完成的工作

### 1.1 全量 Benchmark：10 个目标的完整测试

将测试覆盖从 7 个目标扩展到 10 个（新增 pcre2 正则引擎和 freetype2 字体渲染引擎），对每个目标执行 Serial / MPI / Hybrid / AFL-only 四模式测试，每次运行 120 秒。本期共新增约 500 行代码（harness、种子生成、CmpLog 集成）。

**全部 10 个目标的覆盖率汇总**：

| 目标 | AFL 边数 | 种子 | MPI best | AFL-only | Hybrid best | SymCC 贡献 |
|------|------:|-----:|--------:|--------:|-----------:|:---:|
| png | 3,072 | 10.35% | 15.69% | 14.10% | **16.96%** | +2.9% |
| xml | 50,880 | 3.09% | 6.13% | 7.57% | **8.80%** | +1.2% |
| base64 | 1,088 | 11.58% | 23.25% | 7.81%† | **23.90%** | **+11.7%** |
| md5sum | 1,344 | 7.14% | 7.14% | 7.37% | **7.37%** | 无 |
| uniq | 1,216 | 9.54% | 9.54% | 10.61% | **10.61%** | 无 |
| who | 10,688 | 6.73% | 6.73% | 44.55% | **44.58%** | 无 |
| libarchive | 13,760 | 12.27% | 15.33% | 16.41% | **22.25%** | **+5.8%** |
| SQLite | 31,680 | 14.74% | 15.26% | 18.48% | **19.44%** | +1.0% |
| pcre2 | 7,488 | 15.72% | 20.61% | 40.20% | **43.14%** | +2.9% |
| freetype2 | 21,632 | 2.26% | 2.26% | 6.19% | **10.24%** | 无（有害）|

†base64 的 AFL-only 以编码模式运行（缺少 `-d` 参数，只覆盖了编码的查表路径），不反映 AFL 的真实能力。SymCC 贡献以 MPI-only vs 种子衡量。

**Hybrid 在最优配置下覆盖率均 ≥ AFL-only 和 MPI-only**。但 SymCC 的贡献在不同目标间差异极大：base64 上 SymCC 是主力（+11.7%），who 上 SymCC 贡献为 0，libarchive 上互补效果最明显（Hybrid 22.25% 远超 AFL 16.41% 和 MPI 15.33%）。

### 1.2 who 目标深入分析

who 是数据中最极端的案例：AFL/Hybrid 覆盖率 44.5%，而 MPI（纯 SymCC）只有 6.73%——与种子相同，说明 SymCC 没有产生任何有效输出。

**根因排查**：who 通过 glibc 的 `getutxent()` 高层 API 读取 utmp 文件，而非直接调用 `fread`。SymCC 的运行时库维护了一个 I/O 函数拦截列表（包含 `fread`、`read`、`fgets` 等），但 `getutxent` 不在此列表中。因此，utmp 文件的内容未被标记为符号化输入，Z3 没有约束可求解。

我们通过修改编译配置强制 who 使用 `fread` 路径后，SymCC 能够产出测试用例，但覆盖率仍不变——因为 utmp 是平坦的二进制结构体数组，分支条件（如 `ut_type` 的 7 种取值）通过随机翻转比特就能覆盖，不需要 SymCC 的精确约束求解。AFL 的 bitflip/havoc 变异天然适合这种格式。

**结论**：who 揭示了 SymCC 无效的两个独立原因：（1）glibc 高层 API 绕过了符号化拦截（工程问题，可修复），（2）平坦二进制格式对约束求解没有需求（固有限制）。这个发现具有普遍意义：任何通过 glibc 高层 API（如 `getaddrinfo`、`getpwnam`、`glob` 等）读取输入的程序都可能存在同样的绕过问题。

### 1.3 新增目标：pcre2 和 freetype2

**pcre2（正则表达式引擎）**：编写了 harness 和 8 个多样化正则种子。SymCC 有效（MPI 比种子 +31%），AFL 贡献更大（40.20%），Hybrid 最高（43.14%）。pcre2 展示了"AFL 主导、SymCC 边际贡献"的典型模式。

**freetype2（字体渲染引擎）**：SymCC 完全无效（0 新边），因为逐字节翻转破坏了 TrueType 字体的表目录结构。更值得注意的是，Hybrid 中增加 SymCC workers 反而降低覆盖率：np=2 时 10.24%，np=32 时降到 5.28%。原因是 **CPU 资源争抢**：30 个 SymCC workers 持续执行字体解析和 Z3 求解，占用大量 CPU，AFL 分到的时间片减少，导致 AFL 的 generated 从 np=2 的 1,193 降到 np=32 的 451（甚至低于 AFL-only 的 551）。SymCC workers 在此目标上做的是无用功（0 新边），却实实在在消耗了本可让 AFL 跑得更快的 CPU 资源。

### 1.4 AFL++ CmpLog 集成

集成了 AFL++ 的 CmpLog 功能。CmpLog 在编译时插桩比较操作，运行时自动提取比较参数做字典变异——与我们之前手动实现的 `SYMCC_DICT` 字典引导目标类似，但全自动且更有效。

**A/B 对比结果（120s）**：

| 目标 | AFL 无 CmpLog | AFL 有 CmpLog | 差异 |
|------|------:|------:|------:|
| **SQLite** | 18.48% | **21.03%** | **+2.55%** |
| pcre2 | 40.20% | 39.42% | -0.78% |
| libarchive | 16.41% | 15.60% | -0.81% |
| freetype2 | 6.19% | 5.70% | -0.49% |

CmpLog 对 SQLite 显著有效（+2.55%），因为它自动从 `strcmp` 调用中提取了 SQL 关键字。但对二进制格式无效（无字符串比较可提取）。

**颠覆性发现**：启用 CmpLog 后，SQLite 上 AFL-only（21.03%）> Hybrid（18.72%）。CmpLog 的动态字典提取与 SymCC 的约束求解在功能上重叠，但 CmpLog 在运行时捕获程序遇到的实际比较值，比 SymCC 的逐字节翻转更有效。同时，SymCC workers 与 AFL 争抢 CPU 资源，降低了 AFL 的执行速度。

---

## 二、遇到的问题

| # | 问题 | 影响 | 性质 |
|---|------|------|------|
| 1 | base64 覆盖率测量极慢（47% 输出触发 crash，afl-showmap fork server 频繁重启） | benchmark 6 小时（预期 2 小时） | 工程问题，不影响正确性 |
| 2 | CmpLog 在 SQLite 上让 AFL 超越 Hybrid（21.03% > 18.72%） | SymCC workers 成为净负担 | 需要自适应配置 |
| 3 | freetype2 上 SymCC 完全无效，增加 workers 覆盖率反降 | Hybrid np=32（5.28%）< AFL-only（6.19%） | 目标特征决定的固有限制 |
| 4 | who 的 `getutxent` 绕过 SymCC 拦截 | MPI 覆盖率等于种子 | 拦截列表不完整，可修复但治标不治本 |

---

## 三、结论

### 3.1 Hybrid 在最优配置下覆盖率最高，但需要正确的 np 选择

10 个目标中 Hybrid 均可达到最高覆盖率，前提是选择合适的 np 值。当 SymCC 无效时（freetype2），np 过大反而有害。

### 3.2 SymCC 的价值取决于目标是否有"需要精确构造的约束"

- **有效场景**：magic number（base64 LAVA bug）、格式头识别（libarchive 多格式 magic）
- **无效场景**：平坦二进制（who utmp）、结构化格式（freetype2 TrueType）、glibc API 绕过

### 3.3 CmpLog 是比 SymCC 字典引导更优的文本解析器优化方案

两者的关键区别：SYMCC_DICT 是**静态**字典（用户提供、编译时固定），在种子充分时无额外收益；CmpLog 是**动态**字典（运行时从程序的比较操作中提取），即使种子充分仍有效。对文本解析器（SQL、正则等），CmpLog + AFL 的组合可能比 Hybrid 更优。

### 3.4 最优配置应自适应目标特征

| 目标类型 | 推荐配置 | 依据 |
|---------|---------|------|
| magic number / 校验和 | 多 SymCC workers | base64: SymCC +11.7%, AFL 无法求解 magic |
| 文本解析器 | AFL + CmpLog 为主 | SQLite: AFL+CmpLog 21.03% > Hybrid 18.72% |
| 多格式解析器 | Hybrid（均衡分配） | libarchive: Hybrid 22.25% >> AFL 16.41% + MPI 15.33% |
| 平坦二进制 | 纯 AFL，零 SymCC | who: AFL 44.55%, SymCC 贡献 0; freetype2: SymCC 有害 |

---

## 四、待完成的工作

| 方向 | 说明 | 优先级 |
|------|------|:------:|
| 自适应 Hybrid 配置 | 根据目标特征自动调整 SymCC/AFL 资源分配比例 | 高 |
| 多轮统计 | 当前所有数据为单轮运行，需 3-5 轮计算标准差 | 高 |
| 长时间 benchmark | 当前 120s，跑 10min/1h 验证长期 scaling 效果 | 中 |
| 更多目标 | openssl, harfbuzz（需 autotools 配置） | 低 |

---

## 附录 A：who 分析技术细节

who 的 `readutmp.c` 有两个条件编译版本：

- `#ifdef UTMP_NAME_FUNCTION`：使用 `setutxent()`/`getutxent()`/`endutxent()` glibc API（当前编译走此路径）
- `#else`：使用 `fopen()`+`fread()` 直接读文件

SymCC 的运行时库（`LibcWrappers.cpp`）拦截了 `fread`、`read`、`fgets`、`fgetc` 等函数，但 `getutxent` 不在列表中。修复方法：将 `#ifdef UTMP_NAME_FUNCTION` 改为 `#if 0`，强制走 `fread` 路径。

修复后 who 的 `nm` 输出中出现了 `fread_symbolized`，SymCC 产出 2 个测试用例，但覆盖率不变（仍为 719 edges / 6.73%）。

## 附录 B：CmpLog 实现细节

CmpLog 二进制编译方式：
```bash
AFL_LLVM_CMPLOG=1 afl-clang-fast -O2 -o target_cmplog target.c lib.a
```

在 `run_benchmark.py` 中的集成（`run_hybrid` 和 `run_afl_only`）：
```python
if cmplog_binary:
    afl_cmd.extend(["-c", cmplog_binary, "-l", "2AT"])
```

CmpLog 二进制自动从 `benchmark/public/bin/<target>-cmplog/` 目录发现。

## 附录 C：实验可重现性

```bash
# 全量 benchmark（8 个原有目标）
python3 benchmark/run_benchmark.py \
  --targets gfts-png_read_fuzzer,gfts-xml_read_fuzzer,lava-base64,lava-md5sum,lava-uniq,lava-who,libarchive-archive_fuzzer,sqlite-sqlite_fuzzer \
  --np-list 2,8,32 --timeout 120 --rounds 1 \
  --hybrid --afl-only --no-default --public --skip-build \
  --output benchmark/benchmark_results_full

# 新增目标（pcre2, freetype2）
python3 benchmark/run_benchmark.py \
  --targets pcre2-pcre2_fuzzer,freetype2-freetype2_fuzzer \
  --np-list 2,8,32 --timeout 120 --rounds 1 \
  --hybrid --afl-only --no-default --public --skip-build \
  --output benchmark/benchmark_results_new_targets

# CmpLog A/B 对比
python3 benchmark/run_benchmark.py \
  --targets sqlite-sqlite_fuzzer,pcre2-pcre2_fuzzer,libarchive-archive_fuzzer,freetype2-freetype2_fuzzer \
  --np-list 8 --timeout 120 --rounds 1 \
  --hybrid --afl-only --no-default --public --skip-build \
  --output benchmark/benchmark_results_cmplog
```

注：CmpLog A/B 对比中「无 CmpLog」基线来自 `benchmark_results_full` 和 `benchmark_results_new_targets` 实验（运行条件相同但非同次运行）。当前所有数据为单轮运行（`rounds=1`），未计算标准差。
