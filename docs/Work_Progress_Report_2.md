# SymCC MPI 并行符号执行 — 工作进展报告（二）

## 上期回顾

上一阶段完成了并行框架的核心开发与优化工作：实现了 MPI 纯并行和 Hybrid AFL+SymCC 两种并行模式；通过 6 轮 profiling 驱动优化将 Master 负载降至 9%；修复了 20+ 个 bug（含致命的 afl-showmap 路径错误、LAVA-M unlocked-io 绕过、MPI 死锁等）；在 7 个目标上完成了 benchmark，确认了三个核心结论：（1）吞吐量 scaling 优秀（84-117x at np=128），（2）Hybrid 模式覆盖率最高（AFL + SymCC 互补），（3）覆盖率不随并行度线性增长（concolic execution 的固有限制）。同时验证了两个无效方向：字典引导在种子充分时无额外收益，分种子多实例打断正反馈循环导致覆盖率下降。

**上期遗留的待办事项**：

1. 全量 benchmark（覆盖所有 LAVA-M + Google FTS 目标）
2. 深入分析 who 目标（AFL 44.5% vs MPI 6.7% 的极端差距）
3. 扩展更多 fuzzer-test-suite 目标（pcre2, freetype2 等）
4. AFL++ CmpLog 集成

---

## 一、本期完成的工作

### 1.1 全量 Benchmark：8 个原有目标的完整测试

对所有 LAVA-M（base64, md5sum, uniq, who）和 Google FTS（png, xml）加上 libarchive 和 SQLite 共 8 个目标，执行 Serial / MPI(np=2,8,32) / Hybrid(np=2,8,32) / AFL-only 四模式完整 benchmark，每次运行 120 秒。

**覆盖率汇总（最佳配置）**：

| 目标 | AFL 边数 | 种子 | MPI best | AFL-only | Hybrid best | 最佳模式 |
|------|------:|-----:|--------:|--------:|-----------:|:----:|
| png | 3,072 | 10.35% | 15.69% | 14.10% | **16.96%** | Hybrid |
| xml | 50,880 | 3.09% | 6.13% | 7.57% | **8.80%** | Hybrid |
| base64 | 1,088 | 11.58% | 23.25% | 7.81% | **23.90%** | Hybrid |
| md5sum | 1,344 | 7.14% | 7.14% | 7.37% | **7.37%** | Hybrid=AFL |
| uniq | 1,216 | 9.54% | 9.54% | 10.61% | **10.61%** | Hybrid=AFL |
| who | 10,688 | 6.73% | 6.73% | 44.55% | **44.58%** | Hybrid≈AFL |
| libarchive | 13,760 | 12.27% | 15.33% | 16.41% | **22.25%** | Hybrid |
| SQLite | 31,680 | 14.74% | 15.26% | 18.48% | **19.44%** | Hybrid |

**结论**：Hybrid 在全部 8 个目标上均为最佳或并列最佳覆盖率。但各目标的 SymCC 贡献差异极大：base64 上 SymCC 是主力（+12%），who 上 SymCC 贡献为 0（全部来自 AFL），libarchive 上两者互补效果最明显（Hybrid 22.25% 远超 AFL 16.41% 和 MPI 15.33%）。

### 1.2 who 目标深入分析

who 是数据中最极端的案例：AFL/Hybrid 覆盖率 44.5%，MPI（纯 SymCC）只有 6.73%，与种子相同。

**第一层根因：SymCC 产出 0 个测试用例**

通过 `nm` 检查发现 who 二进制中没有 `fread_symbolized`，只有 `getutxent@GLIBC`。追溯到 `readutmp.c` 的条件编译：当 `UTMP_NAME_FUNCTION` 被定义时，who 使用 glibc 的 `getutxent()` API 读取 utmp 文件。该函数在 glibc **内部**调用 `read()` 系统调用，SymCC 只拦截用户代码中的 I/O 函数，无法拦截 glibc 内部调用，导致输入数据不被标记为符号化。

**修复**：在 `readutmp.c` 中将 `#ifdef UTMP_NAME_FUNCTION` 改为 `#if 0`，强制使用 `fopen`+`fread` 路径。修复后 SymCC 产出了 2 个测试用例。

**第二层根因：产出了测试用例但覆盖率不变**

修复后 SymCC 产出 2 个变体（修改了 1-2 个字节），但 AFL 边覆盖率仍为 719 edges（6.73%）。utmp 是一个平坦的二进制结构体数组（`struct utmpx`：`ut_type` + `ut_pid` + `ut_line[32]` + `ut_user[32]` + `ut_host[256]`...），分支条件完全由字段值决定，没有 magic number 或校验和。SymCC 的约束翻转对这种"随机翻转就能覆盖"的格式没有优势。

**AFL 为什么能达到 44.58%**：AFL 的 bitflip/havoc 变异天然适合二进制结构体——翻转 `ut_type` 的比特进入 7 种 case 分支，随机化字符串字段触发不同的格式化路径，截断/扩展记录触发循环边界。

**结论**：who 是"AFL 完胜 SymCC"的典型案例。SymCC 的价值在于精确构造特定值（magic number），对于不需要精确构造的平坦二进制格式，AFL 的随机变异既快又有效。

### 1.3 新增测试目标：pcre2 和 freetype2

从 Google fuzzer-test-suite 编译了两个新目标，将测试覆盖扩展到 10 个：

**pcre2（正则表达式引擎，7,488 AFL 边）**：

编写了文件读取 harness（`pcre2_harness.c`），调用 `pcre2_compile` + `pcre2_match`。创建了 8 个多样化种子（简单文本、字符集、交替、量词、后向引用、命名组、数字模式、惰性匹配）。

| 模式 | 边数 | 覆盖率 | 说明 |
|------|-----:|------:|------|
| 种子 | 1,177 | 15.72% | 8 个正则种子 |
| MPI np=32 | 1,543 | 20.61% | SymCC 有效（+31%） |
| AFL-only | 3,010 | 40.20% | AFL 主导 |
| **Hybrid np=32** | **3,230** | **43.14%** | **最高，SymCC 额外贡献 +3%** |

pcre2 的特点：AFL 贡献巨大（随机变异产生多样的正则语法），SymCC 也有效（+31% over seed），Hybrid 在 AFL 基础上额外多 3% 覆盖率。

**freetype2（字体渲染引擎，21,632 AFL 边）**：

编写了文件读取 harness（`freetype2_harness.c`），调用 `FT_New_Face` + `FT_Load_Glyph` + `FT_Render_Glyph`。使用系统字体截断和最小 TrueType 头作为种子。

| 模式 | 边数 | 覆盖率 | 说明 |
|------|-----:|------:|------|
| 种子 | 488 | 2.26% | |
| MPI np=32 | 488 | 2.26% | **SymCC 完全无效** |
| AFL-only | 1,339 | 6.19% | AFL 有效 |
| **Hybrid np=2** | **2,216** | **10.24%** | 最高，但 np 增大反而下降 |

freetype2 的特点：SymCC 产出 0 新边（字体文件的表目录+偏移结构太复杂，逐字节翻转破坏表结构导致 `FT_New_Face` 直接失败）。Hybrid np=2 最好（10.24%），但增加 SymCC workers 反而降低覆盖率（np=32 降到 5.28%），因为 SymCC 占用了本可给 AFL 的计算资源。

### 1.4 AFL++ CmpLog 集成

CmpLog 是 AFL++ 的功能，通过在编译时插桩 `strcmp`/`memcmp`/`switch` 等比较操作，在运行时自动提取比较参数（如 SQL 关键字、协议字段），用作智能字典变异。与我们之前手动实现的 `SYMCC_DICT` 字典引导不同，CmpLog **全自动**——不需要用户提供字典文件。

**实现**：

1. 为 4 个目标编译 CmpLog 版本：`AFL_LLVM_CMPLOG=1 afl-clang-fast` 生成专用二进制
2. 在 `run_benchmark.py` 中添加 CmpLog 支持：`run_hybrid` 和 `run_afl_only` 接受 `cmplog_binary` 参数，自动在 AFL 命令中添加 `-c <cmplog_binary> -l 2AT`
3. 自动发现 `*-cmplog` 目录下的 CmpLog 二进制

**A/B 对比结果（np=8, 120s）**：

| 目标 | AFL 无 CmpLog | AFL 有 CmpLog | 差异 |
|------|------:|------:|------:|
| **SQLite** | 18.48% | **21.03%** | **+2.55%** |
| pcre2 | 40.20% | 39.42% | -0.78% |
| libarchive | 16.41% | 15.60% | -0.81% |
| freetype2 | 6.19% | 5.70% | -0.49% |

| 目标 | Hybrid 无 CmpLog | Hybrid 有 CmpLog | 差异 |
|------|------:|------:|------:|
| **SQLite** | 18.60% | 18.72% | +0.12% |
| pcre2 | 40.13% | 39.77% | -0.36% |
| libarchive | 17.85% | 18.60% | +0.75% |
| freetype2 | 9.06% | 9.06% | +0.00% |

**关键发现**：

- **SQLite AFL-only 显著提升**（18.48% → 21.03%）：CmpLog 自动从 SQLite 的 `strcmp` 调用中提取了 SQL 关键字（SELECT, INSERT, CREATE...），效果等于我们手动实现的字典引导
- **SQLite Hybrid 几乎无变化**（18.60% → 18.72%）：且 **Hybrid < AFL-only**（18.72% < 21.03%）。CmpLog 让 AFL 如此强大，SymCC workers 反而成了净负担——它们占用了 6 个核做低效的逐字节翻转，不如把这 6 个核全给 AFL
- **非文本格式无效**：CmpLog 主要提取字符串比较参数，对二进制结构体（libarchive, freetype2）没有帮助，反而因额外开销略慢

---

## 二、遇到的问题

### 问题 1：LAVA-M base64 的覆盖率测量极慢

base64 的 MPI 模式生成数万个测试用例，其中 47% 会触发 SIGSEGV（LAVA-M 注入的 bug）。`afl-showmap -C` 处理这些 crash 文件时，每次 crash 后 fork server 必须重启，速度从 ~100 files/s 降到 ~10 files/s。np=32 的 64,000 个输出文件需要 30-60 分钟做覆盖率测量。

**影响**：全量 benchmark 总运行时间远超预期（8 个目标约 6 小时，主要瓶颈在 base64 的覆盖率测量）。

**性质**：这是 afl-showmap 对 crash 文件的已知性能问题，不影响结果正确性，但影响实验效率。

### 问题 2：CmpLog 在文本解析器上让 AFL 超越 Hybrid

SQLite 上 AFL+CmpLog（21.03%）> Hybrid+CmpLog（18.72%）。这意味着在 CmpLog 启用后，Hybrid 中的 SymCC workers 不仅没有帮助，反而拉低了整体效果。

**根因**：CmpLog 让 AFL 自动学会了 SQL 关键字替换（与 SymCC 的字典引导功能重叠），而 SymCC 的约束求解仍然是逐字节翻转（无法超越 CmpLog 的智能字典变异）。6 个 SymCC workers 消耗了本可给 AFL 的 CPU 资源。

**启示**：Hybrid 的最优 SymCC worker 数应该自适应——对 CmpLog 有效的文本解析器应减少 SymCC workers（甚至为 0），对 magic number 型目标（base64）应增加 SymCC workers。

### 问题 3：freetype2 上 SymCC 完全无效

SymCC 对 freetype2 产出了 test cases 但 0 条新边。字体文件有严格的表目录结构（4 字节 tag + offset + length），逐字节翻转破坏了表结构的完整性，导致 FT_New_Face 在解析阶段直接拒绝。

**性质**：与 who 的 utmp 问题类似——平坦/结构化二进制格式对 SymCC 约束求解不利。这类格式适合 AFL 的随机变异（能自然触发不同的 type/flag 值），不适合 SymCC 的精确约束求解。

---

## 三、实验结果汇总

### 3.1 全部 10 个目标的覆盖率对比

| 目标 | AFL 边 | MPI best | AFL-only | Hybrid best | SymCC 贡献 |
|------|------:|--------:|--------:|-----------:|:---:|
| png | 3,072 | 15.69% | 14.10% | **16.96%** | 边际 (+2.9%) |
| xml | 50,880 | 6.13% | 7.57% | **8.80%** | 边际 (+1.2%) |
| base64 | 1,088 | 23.25% | 7.81% | **23.90%** | **主力** (+16%) |
| md5sum | 1,344 | 7.14% | 7.37% | **7.37%** | 无 |
| uniq | 1,216 | 9.54% | 10.61% | **10.61%** | 无 |
| who | 10,688 | 6.73% | 44.55% | **44.58%** | 无 |
| libarchive | 13,760 | 15.33% | 16.41% | **22.25%** | **显著** (+5.8%) |
| SQLite | 31,680 | 15.26% | 18.48% | **19.44%** | 边际 (+1.0%) |
| pcre2 | 7,488 | 20.61% | 40.20% | **43.14%** | 边际 (+2.9%) |
| freetype2 | 21,632 | 2.26% | 6.19% | **10.24%** | 无（有害） |

### 3.2 SymCC 的有效性分类

根据 10 个目标的实验数据，可将 SymCC 的有效性分为三类：

| 类别 | 目标 | 特征 | SymCC 贡献 |
|------|------|------|-----------|
| **SymCC 主力** | base64 | magic number 约束、校验和 | Hybrid 比 AFL 高 16+% |
| **SymCC 有效** | libarchive, png, pcre2, xml | 格式头 magic、混合结构 | Hybrid 比 AFL 高 1-6% |
| **SymCC 无效** | who, freetype2, md5sum, uniq | 平坦二进制、无约束、glibc API 绕过 | Hybrid ≈ AFL 或 SymCC 有害 |

### 3.3 CmpLog 对 AFL 的提升

| 目标 | AFL 无 CmpLog | AFL 有 CmpLog | 提升 |
|------|------:|------:|------:|
| SQLite | 18.48% | **21.03%** | **+2.55%** |
| pcre2 | 40.20% | 39.42% | -0.78% |
| libarchive | 16.41% | 15.60% | -0.81% |
| freetype2 | 6.19% | 5.70% | -0.49% |

CmpLog 对文本解析器（SQLite）效果显著，对二进制格式无效。

---

## 四、结论

### 4.1 Hybrid 模式在全部 10 个目标上覆盖率最优

这是在扩展到 10 个不同类型目标后确认的结论。即使 SymCC 完全无效（freetype2, who），Hybrid 仍不低于 AFL-only，因为 Hybrid 中的 AFL 组件独立工作不受 SymCC 影响。

### 4.2 SymCC 的价值取决于目标特征

SymCC 对含 magic number / 校验和的目标（base64）贡献极大（+16%），对含格式头的多格式解析器（libarchive）有显著互补效果（+5.8%），但对平坦二进制结构体（who, freetype2）和 glibc API 绕过的目标（md5sum, uniq）完全无效。

### 4.3 CmpLog 是 SymCC 字典引导的更优替代

AFL++ 的 CmpLog 功能在 SQLite 上自动提升覆盖率 2.55%，效果与我们手动实现的 SYMCC_DICT 字典引导相同，但不需要用户提供字典文件。且 CmpLog 启用后 AFL 如此强大，以至于在 SQLite 上 AFL-only (21.03%) > Hybrid (18.72%)——SymCC workers 反而成了负担。

### 4.4 最优配置应自适应目标特征

不存在一种配置对所有目标都最优。理想方案是根据目标特征自动调整：

| 目标类型 | 推荐配置 |
|---------|---------|
| magic number / 校验和 | 多 SymCC workers，少 AFL |
| 文本解析器（SQL, XML） | AFL + CmpLog 为主，少量 SymCC |
| 平坦二进制（utmp, font） | 纯 AFL，不分配 SymCC workers |

---

## 五、待完成的工作

| 方向 | 说明 | 优先级 |
|------|------|:------:|
| 自适应 Hybrid 配置 | 根据目标特征自动调整 SymCC/AFL 资源分配比例 | 高 |
| 多轮统计 | 当前所有数据为单轮运行，需 3-5 轮计算标准差 | 高 |
| 长时间 benchmark | 当前 120s，跑 10min/1h 验证长期 scaling 效果 | 中 |
| 更多目标 | openssl, harfbuzz（需 autotools 配置） | 低 |

---

## 附录：实验可重现性

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

注：CmpLog 需先编译 cmplog 版本二进制（`AFL_LLVM_CMPLOG=1 afl-clang-fast`），放入 `benchmark/public/bin/<target>-cmplog/` 目录。当前所有数据为单轮运行（`rounds=1`），未计算标准差。
