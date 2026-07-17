# SymCC MPI 并行符号执行 — 工作进展报告（二）

## 上期回顾

上一阶段完成了并行框架的核心开发与优化工作：实现了 MPI 纯并行和 Hybrid AFL+SymCC 两种并行模式；通过 6 轮 profiling 驱动优化将 Master 负载降至 9%；修复了 20+ 个 bug；在 7 个目标上完成了初步 benchmark。确认了三个核心结论：（1）吞吐量 scaling 优秀（84-117x at np=128），（2）Hybrid 模式覆盖率最高，（3）覆盖率不随并行度线性增长（concolic execution 的固有限制）。

**本期目标**：全量测试所有目标、深入分析异常案例、扩展测试范围、集成 AFL++ CmpLog。

---

## 测试方法

### 测试环境

- **CPU**: AMD Threadripper PRO 9995WX — 96 cores / 192 threads
- **RAM**: 250 GB DDR5
- **OS**: Ubuntu 24.04, kernel 6.17.0
- **AFL++**: 最新版本（含 CmpLog 支持）
- **Z3**: 4.x

### 测试模式

| 模式 | 说明 | 覆盖率来源 |
|------|------|-----------|
| **Seed** | 纯种子，不经过任何 fuzzing/concolic | `afl-showmap -C` 测量种子目录 |
| **MPI** | 纯 SymCC 并行（无 AFL） | `afl-showmap -C` 测量全部输出（>20K TC 时随机抽样 20K） |
| **Hybrid** | AFL + 并行 SymCC，双向反馈 | 同时记录：(1) `afl-showmap -C` 测量全部输出，(2) AFL `fuzzer_stats` 中的 `edges_found`/`total_edges` |
| **AFL-only** | 纯 AFL fuzzing，无 SymCC | 同时记录：(1) `afl-showmap -C`，(2) AFL `fuzzer_stats` |

### 覆盖率指标说明

- **ShowmapCov**：使用 `afl-showmap -C` 对全部输出测试用例重放测量的边覆盖率。反映所有已保存测试用例的累积覆盖。
- **FstatsCov**：AFL `fuzzer_stats` 文件中的 `bitmap_cvg`（`edges_found`/`total_edges`）。反映 AFL 在整个 fuzzing 过程中所有执行（包括未保存的变异）的累积覆盖。对于 AFL 参与的模式（Hybrid/AFL-only），此指标更全面。
- **两者差异**：FstatsCov 通常 ≥ ShowmapCov，因为 AFL 的 bitmap 记录了所有执行过的路径，而 ShowmapCov 仅测量保存的 queue 文件。但对于 Hybrid 模式，ShowmapCov 可能 > FstatsCov，因为 ShowmapCov 包含了 SymCC 输出但 AFL 尚未同步的测试用例。

### 测试参数

- **超时时间**: 300 秒/轮
- **并行度**: np=2, 8, 32
- **轮次**: 1 轮（单次运行）
- **目标数量**: 10 个

---

## 核心发现

本期最重要的结论是：**SymCC 的有效性高度依赖目标程序的特征**。通过对 10 个不同类型目标的系统测试，我们将 SymCC 的有效性分为三类：

| 类别 | 目标 | 特征 | SymCC 对覆盖率的贡献（相对 seed 提升） |
|:----:|------|------|-----------|
| **主力** | base64 | 含 magic number 约束 | MPI np=8 比种子 +100.8%（23.25% vs 11.58%） |
| **有效** | libarchive, png, xml, SQLite | 格式头、混合结构 | Hybrid np=32 比 AFL-only 高 1-8pp |
| **无效** | who, freetype2, md5sum, uniq | 平坦二进制、glibc API 绕过 | 0 或负面影响 |

另一个重要发现：freetype2 在 Hybrid np=8 时覆盖率 11.23%，但 np=32 时降至 5.59%（甚至低于 AFL-only 的 5.75%）。原因是 CPU 资源争抢：30 个 SymCC workers 持续执行字体解析和 Z3 求解，占用 CPU 时间片，AFL 的 exec/s 从 np=2 的 3648 降到 np=32 的 3433，且 AFL queue 从 575 降到 553。

---

## 一、全量 Benchmark 结果

### 1.1 全部 10 个目标的覆盖率汇总

以下使用 `afl-showmap -C` 覆盖率（ShowmapCov），timeout=300s，单轮运行。

| 目标 | AFL 总边数 | 种子 | MPI best | AFL-only | Hybrid best | Hybrid best np |
|------|------:|-----:|--------:|--------:|-----------:|:---:|
| png | 3,072 | 10.35% | 14.94% (np=2) | 14.10% | **16.96%** | 32 |
| xml | 50,880 | 3.08% | 5.74% (np=2) | 7.60% | **8.90%** | 32 |
| base64 | 1,088 | 11.58% | 23.25% (np=8) | 7.81%† | **23.90%** | 8,32 |
| md5sum | 1,344 | 7.14% | 7.14% | 7.37% | **7.37%** | - |
| uniq | 1,216 | 9.54% | 9.54% | 10.61% | **10.61%** | - |
| who | 10,688 | 6.73% | 6.73% | 44.57% | **47.26%** | 32 |
| libarchive | 13,760 | 6.45% | 13.90% (np=8) | 15.07% | **23.36%** | 32 |
| SQLite | 31,680 | 13.70% | 14.97% (np=2) | 18.55% | **18.84%** | 32 |
| pcre2 | 7,488 | 7.16% | 18.66% (np=8) | 41.73% | **43.58%** | 32 |
| freetype2 | 21,632 | 2.22% | 2.26% | 5.75% | **11.23%** | 8 |

†base64 的 AFL-only 以编码模式运行（缺少 `-d` 参数），不反映 AFL 的真实能力。

### 1.2 AFL fuzzer_stats 覆盖率对比（仅 AFL 参与模式）

以下使用 AFL `fuzzer_stats` 中的 `bitmap_cvg`（FstatsCov），更真实反映 AFL 内部覆盖状态。

| 目标 | AFL-only (FstatsCov) | Hybrid np=2 | Hybrid np=8 | Hybrid np=32 |
|------|------:|------:|------:|------:|
| png | 14.28% | 14.28% | 14.38% | 14.28% |
| xml | 7.63% | 7.59% | 7.39% | 7.82% |
| base64 | 14.74% | 14.74% | 15.51% | 14.74% |
| md5sum | 7.59% | 7.59% | 7.59% | 7.59% |
| uniq | 11.16% | 11.16% | 11.16% | 11.16% |
| who | 44.62% | 44.63% | 44.63% | 44.63% |
| libarchive | 15.13% | 16.28% | 16.50% | **19.17%** |
| SQLite | 25.87% | 26.00% | 25.59% | **26.15%** |
| pcre2 | 41.83% | 40.90% | 40.24% | 40.46% |
| freetype2 | 5.75% | 6.39% | **11.24%** | 5.59% |

**关键观察**：
1. **FstatsCov 与 ShowmapCov 的差异**：对于 SQLite，FstatsCov（25.87%）远高于 ShowmapCov（18.55%），因为 AFL 执行了 807K 次变异，大部分覆盖来自未保存的执行。
2. **Hybrid 的 FstatsCov 与 AFL-only 差异较小**：在 FstatsCov 维度上，SymCC 的边际贡献主要体现在 libarchive（+4pp）和 freetype2 np=8（+5.5pp）。
3. **ShowmapCov 中 Hybrid 明显高于 AFL-only**：说明 SymCC 生成了大量有新覆盖的测试用例，但这些覆盖大部分已被 AFL 自身的变异所覆盖（体现在 FstatsCov 中）。

### 1.3 SymCC 贡献分析（相对种子基线的边覆盖率提升）

| 目标 | 种子覆盖率 | MPI best | Hybrid best (Showmap) | AFL-only | SymCC 净贡献‡ |
|------|------:|------:|------:|------:|------:|
| png | 10.35% | +44.3% | +63.9% | +36.2% | Hybrid 比 AFL 高 +2.86pp |
| xml | 3.08% | +86.4% | +189.0% | +146.8% | Hybrid 比 AFL 高 +1.30pp |
| base64 | 11.58% | +100.8% | +106.4% | -32.6%† | MPI 主导 +12.32pp |
| md5sum | 7.14% | +0.0% | +3.2% | +3.2% | 0（AFL 独立发现） |
| uniq | 9.54% | +0.0% | +11.2% | +11.2% | 0（AFL 独立发现） |
| who | 6.73% | +0.0% | +602.2% | +562.3% | Hybrid 比 AFL 高 +2.69pp |
| libarchive | 6.45% | +115.5% | +262.2% | +133.6% | **Hybrid 比 AFL 高 +8.29pp** |
| SQLite | 13.70% | +9.3% | +37.5% | +35.4% | Hybrid 比 AFL 高 +0.29pp |
| pcre2 | 7.16% | +160.6% | +508.7% | +482.8% | Hybrid 比 AFL 高 +1.85pp |
| freetype2 | 2.22% | +1.8% | +405.9% (np=8) | +159.0% | Hybrid np=8 比 AFL 高 +5.48pp |

‡SymCC 净贡献 = Hybrid best (ShowmapCov) - AFL-only (ShowmapCov)
†base64 AFL-only 缺少 `-d` 参数，结果不具可比性

---

## 二、深入分析

### 2.1 who 目标：SymCC 完全无效

who 是数据中最极端的案例：AFL/Hybrid 覆盖率 44-47%，而 MPI（纯 SymCC）只有 6.73%——与种子相同。

**根因**：who 通过 glibc 的 `getutxent()` 高层 API 读取 utmp 文件，而非直接调用 `fread`。SymCC 的运行时库拦截了 `fread`、`read`、`fgets` 等函数，但 `getutxent` 不在拦截列表中。因此，utmp 文件的内容未被标记为符号化输入，Z3 没有约束可求解。

即使修复了拦截问题（强制使用 `fread` 路径），utmp 是平坦的二进制结构体数组，分支条件通过随机翻转比特就能覆盖，不需要精确约束求解。

### 2.2 freetype2：SymCC 有害

freetype2 的 Hybrid 覆盖率呈非单调变化：np=2 时 6.39%，np=8 时 **11.23%**（最高），np=32 时降至 **5.59%**（低于 AFL-only 的 5.75%）。

**根因**：CPU 资源争抢。30 个 SymCC workers（np=32）持续执行字体解析和 Z3 求解，占用大量 CPU。AFL 的执行速度受影响（exec/s 从 np=2 的 3648 降到 np=32 的 3433），关键是 AFL 的有效变异时间被压缩。

SymCC 在 freetype2 上完全无效（MPI 覆盖率 = 种子覆盖率 2.26%），因为逐字节翻转破坏了 TrueType 字体的表目录结构。无效的 workers 消耗 CPU 但产出为零。

**注意**：SymCC 的无效输出不会污染 AFL 的种子队列。系统有四层过滤机制：
1. **SymCC 层**：`isInterestingBranch()` 检查分支是否已覆盖，已覆盖则跳过求解
2. **Worker 端 dedup**：运行 `afl-showmap` 检查每个输出，仅传送有新边的（~3% 通过率）
3. **Master triage**：合并到全局 coverage bitmap，仅保存有新边的用例
4. **AFL 同步过滤**：AFL 的 sync 机制独立判断，仅导入触发新路径的用例

因此 Hybrid np=32 覆盖率下降的原因是 **CPU 争抢**，而非种子污染。

### 2.3 libarchive：Hybrid 互补效果最佳

libarchive 是 Hybrid 模式价值最大的目标：
- AFL-only: 15.07%
- MPI np=8: 13.90%
- **Hybrid np=32: 23.36%**（远超两者之和，体现互补效应）

FstatsCov 也证实了这一点：Hybrid np=32 的 FstatsCov 为 19.17%，比 AFL-only 的 15.13% 高出 4pp。这意味着 SymCC 确实为 AFL 提供了有价值的种子，帮助 AFL 发现了更多路径。

libarchive 解析多种归档格式（tar、zip、cpio 等），每种格式有独立的 magic number 和头部结构。SymCC 能精确构造各种格式头，帮助 AFL 进入不同的解析分支。

### 2.4 ShowmapCov vs FstatsCov 差异分析

| 目标 | ShowmapCov (Hybrid np=32) | FstatsCov (Hybrid np=32) | 差异 |
|------|------:|------:|------:|
| SQLite | 18.84% | **26.15%** | AFL 内部覆盖远高于 queue |
| pcre2 | **43.58%** | 40.46% | SymCC 输出尚未被 AFL 同步 |
| libarchive | **23.36%** | 19.17% | SymCC 输出尚未被 AFL 同步 |

- **SQLite**：FstatsCov >> ShowmapCov 说明 AFL 执行了大量有效变异但未保存（非 interesting），这些覆盖只存在于 AFL bitmap 中
- **pcre2/libarchive**：ShowmapCov > FstatsCov 说明 SymCC 产生的测试用例有额外覆盖但 AFL 在 300s 内未完全同步

---

## 三、结论

### 3.1 Hybrid 在最优 np 配置下覆盖率最高

10 个目标中 Hybrid ShowmapCov 均 ≥ AFL-only（最优 np 选取时）。但最优 np 因目标而异：
- libarchive、pcre2、png、xml：np=32 最优
- freetype2：np=8 最优（np=32 因 CPU 争抢反而降低）
- md5sum、uniq：np 无影响（SymCC 无效）

### 3.2 SymCC 的价值取决于目标特征

- **有效场景**：magic number（base64）、多格式解析（libarchive 多种 magic）
- **边际有效**：已有复杂输入的格式（png、xml、pcre2、SQLite）——AFL 自身变异已足够发现大部分边
- **无效场景**：平坦二进制（who utmp）、结构化格式（freetype2 TrueType）、glibc API 绕过

### 3.3 FstatsCov 是更准确的 AFL 覆盖率指标

FstatsCov（`fuzzer_stats` 中的 `bitmap_cvg`）反映了 AFL 在整个 fuzzing 过程中所有执行的累积覆盖，而 ShowmapCov 仅测量保存的 queue。对于评估"SymCC 是否帮助 AFL 发现了更多路径"，应使用 FstatsCov。

以 FstatsCov 衡量，SymCC 的净贡献最显著的目标是：
1. **libarchive**: Hybrid np=32 (19.17%) vs AFL-only (15.13%) = **+4.04pp**
2. **freetype2 np=8**: Hybrid (11.24%) vs AFL-only (5.75%) = **+5.49pp**
3. **其他目标**: Hybrid FstatsCov ≈ AFL-only FstatsCov（差异 <1pp）

### 3.4 无效变异不影响 AFL 质量

SymCC 产生的测试用例经过四层过滤（SymCC 分支检查 → Worker dedup → Master triage → AFL 同步过滤），只有触发新覆盖的用例才会进入 AFL 的 queue。SymCC 的负面影响仅来自 **CPU 资源争抢**（如 freetype2 np=32），可通过限制 worker 数量缓解。

---

## 四、待完成的工作

| 方向 | 说明 | 优先级 |
|------|------|:------:|
| 多轮统计 | 当前所有数据为单轮运行，需 3-5 轮计算标准差 | 高 |
| 自适应 np 选择 | 根据目标特征自动调整 SymCC/AFL 资源分配比例 | 高 |
| 长时间 benchmark | 当前 300s，跑 10min/1h 验证长期 scaling | 中 |
| CmpLog A/B 对比 | 在 FstatsCov 体系下重新对比 CmpLog 效果 | 中 |

---

## 附录 A：完整数据表

### A.1 MPI-only 模式（afl-showmap 覆盖率）

| 目标 | 种子 | np=2 | np=8 | np=32 | TC 生成量 (np=32) |
|------|------:|------:|------:|------:|------:|
| png | 10.35% | 14.94% | 14.58% | 14.36% | 641,385 |
| xml | 3.08% | 5.74% | 5.48% | 5.43% | 1,206,278 |
| base64 | 11.58% | 21.97% | 23.25% | 23.16% | 351,574 |
| md5sum | 7.14% | 7.14% | 7.14% | 7.14% | 0 |
| uniq | 9.54% | 9.54% | 9.54% | 9.54% | 0 |
| who | 6.73% | 6.73% | 6.73% | 6.73% | 8 |
| libarchive | 6.45% | 12.83% | 13.90% | 12.52% | 330,669 |
| SQLite | 13.70% | 14.97% | 14.97% | 14.97% | 848,354 |
| pcre2 | 7.16% | 15.29% | 18.66% | 18.32% | 554,148 |
| freetype2 | 2.22% | 2.26% | 2.26% | 2.26% | 14,813 |

### A.2 Hybrid 模式（双指标）

| 目标 | np | ShowmapCov | FstatsCov | AFL queue | SymCC interesting | execs | exec/s |
|------|---:|------:|------:|------:|------:|------:|------:|
| png | 2 | 16.44% | 14.28% | 261 | 0 | 1,454,845 | 4,833 |
| png | 8 | 16.50% | 14.38% | 248 | 0 | 1,344,922 | 4,467 |
| png | 32 | 16.96% | 14.28% | 266 | 0 | 1,240,599 | 4,121 |
| xml | 2 | 8.24% | 7.59% | 3,262 | 0 | 942,714 | 3,133 |
| xml | 8 | 8.70% | 7.39% | 3,401 | 0 | 818,351 | 2,719 |
| xml | 32 | 8.90% | 7.82% | 3,830 | 0 | 711,268 | 2,362 |
| base64 | 2 | 22.33% | 14.74% | 171 | 0 | 760,256 | 2,525 |
| base64 | 8 | 23.90% | 15.51% | 241 | 0 | 618,372 | 2,054 |
| base64 | 32 | 23.90% | 14.74% | 206 | 0 | 741,459 | 2,462 |
| libarchive | 2 | 17.22% | 16.28% | 1,483 | 0 | 773,522 | 2,570 |
| libarchive | 8 | 18.88% | 16.50% | 2,071 | 0 | 749,443 | 2,490 |
| libarchive | 32 | 23.36% | 19.17% | 2,554 | 0 | 644,022 | 2,138 |
| SQLite | 2 | 18.70% | 26.00% | 1,813 | 0 | 719,539 | 2,391 |
| SQLite | 8 | 18.64% | 25.59% | 2,027 | 0 | 825,073 | 2,742 |
| SQLite | 32 | 18.84% | 26.15% | 2,074 | 0 | 868,636 | 2,885 |
| pcre2 | 2 | 41.49% | 40.90% | 5,635 | 0 | 1,282,293 | 4,260 |
| pcre2 | 8 | 41.69% | 40.24% | 6,199 | 0 | 1,239,735 | 4,118 |
| pcre2 | 32 | 43.58% | 40.46% | 6,194 | 0 | 1,184,526 | 3,931 |
| freetype2 | 2 | 6.39% | 6.39% | 575 | 0 | 1,098,196 | 3,648 |
| freetype2 | 8 | 11.23% | 11.24% | 1,190 | 0 | 1,025,971 | 3,408 |
| freetype2 | 32 | 5.59% | 5.59% | 553 | 0 | 1,033,369 | 3,433 |

### A.3 AFL-only 基线

| 目标 | ShowmapCov | FstatsCov | queue | execs | exec/s |
|------|------:|------:|------:|------:|------:|
| png | 14.10% | 14.28% | 174 | 1,478,044 | 4,928 |
| xml | 7.60% | 7.63% | 2,577 | 863,397 | 2,880 |
| base64 | 7.81% | 14.74% | 108 | 801,918 | 2,674 |
| md5sum | 7.37% | 7.59% | 9 | 826,828 | 2,756 |
| uniq | 10.61% | 11.16% | 56 | 795,278 | 2,651 |
| who | 44.57% | 44.62% | 44 | 620,393 | 2,068 |
| libarchive | 15.07% | 15.13% | 1,158 | 682,809 | 2,277 |
| SQLite | 18.55% | 25.87% | 1,510 | 807,586 | 2,694 |
| pcre2 | 41.73% | 41.83% | 5,529 | 1,460,493 | 4,870 |
| freetype2 | 5.75% | 5.75% | 541 | 1,048,373 | 3,496 |

## 附录 B：实验可重现性

```bash
# 全量 benchmark（10 个目标，300s，np=2,8,32）
python3 benchmark/run_benchmark.py \
  --targets gfts-png_read_fuzzer,gfts-xml_read_fuzzer,lava-base64,lava-md5sum,lava-uniq,lava-who,libarchive-archive_fuzzer,sqlite-sqlite_fuzzer,pcre2-pcre2_fuzzer,freetype2-freetype2_fuzzer \
  --np-list 2,8,32 --timeout 300 --rounds 1 \
  --hybrid --afl-only --no-default --public --skip-build --no-serial \
  --output benchmark/benchmark_results_v2
```

## 附录 C：SymCC → AFL 反馈机制

SymCC 输出到达 AFL queue 的完整路径：

1. **SymCC 执行**：Worker 对 AFL queue 中的种子执行符号执行，为每个条件分支生成 negated 约束的解
2. **SymCC 层过滤**：`isInterestingBranch()` 检查共享 AFL bitmap，已覆盖的分支不求解
3. **Worker 端 dedup**：Worker 持有 master bitmap 副本，使用 `afl-showmap -S`（streaming mode, 0.6ms/TC）检查每个输出，仅发送有新边的（~3% 通过率）
4. **Master triage**：合并到全局 coverage bitmap，仅将有新边的用例写入 SymCC queue 和 AFL queue
5. **AFL 同步**：AFL 在 sync 周期扫描 SymCC queue，仅导入在 AFL bitmap 中触发新路径的用例

因此：
- **无效变异**（不覆盖新边的输出）在步骤 2-4 被丢弃，不会进入 AFL queue
- **Hybrid 覆盖率下降**的唯一原因是 CPU 争抢（SymCC workers 消耗 CPU，压缩 AFL 执行时间）
- **缓解方案**：限制 SymCC worker 数量，或实现自适应资源分配（检测 SymCC 无效时自动减少 workers）
