# PPT 材料补充 — 详细技术与数据

> 本文档是 `PPT_Material_Complete.md` 的补充，包含完整的代码实现细节、编译系统、种子配置、完整实验原始数据等。两份文档合并即为完整的 PPT 素材。

---

## 补充一：编译系统详细信息

### compile_public_benchmarks.sh（1,108 行）

#### LAVA-M 编译（build_lava, 176 行）

**关键补丁**：

1. **unlocked-io.h 补丁**（最关键的修复）：
```c
// 原始 gnulib 的 unlocked-io.h 将标准 I/O 重定义为 *_unlocked 变体：
// #define fread  fread_unlocked
// #define fwrite fwrite_unlocked
// #define getc   getc_unlocked

// SymCC 仅拦截标准 fread/fwrite/getc，不拦截 *_unlocked
// → 所有输入绕过符号化 → 0 个测试用例输出

// 补丁：反向映射，禁用 *_unlocked 优化
#define fread_unlocked  fread
#define fwrite_unlocked fwrite
#define getc_unlocked   getc
#define fgetc_unlocked  fgetc
#define fgets_unlocked  fgets
#define fputs_unlocked  fputs
#define fputc_unlocked  fputc
#define putc_unlocked   putc
#define putchar_unlocked putchar
#define clearerr_unlocked clearerr
#define feof_unlocked   feof
#define ferror_unlocked ferror
#define fileno_unlocked fileno
```

2. **glibc 2.28 兼容补丁**：`_IO_ftrylockfile` → `flockfile`，`_IO_IN_BACKUP` 定义，`sys/sysmacros.h` 包含

3. **O_SEARCH 补丁**：不支持的系统上回退到 `O_RDONLY`

4. **`.args` 机制**：base64 需要 `-d` 参数才能进入解码路径（否则只覆盖编码的查表路径）

#### libpng CRC 绕过补丁

```c
// 在 pngrutil.c 中：
if (need_crc && !getenv("SYMCC_SKIP_CRC")) {
    // 原始 CRC 校验代码
} else {
    // 跳过 CRC → SymCC 变异的 chunk 数据可通过校验
}
```

SymCC 修改 PNG chunk 数据字节后，CRC 不匹配会导致 `png_crc_error` → 程序跳到错误处理 → 无法探索数据解析路径。绕过后 SymCC 可探索 CRC 后的数据处理分支（定性改善，无精确量化数据）。

#### 增强 Harness

**PNG 增强 harness**（添加颜色转换 API）：
```c
// 原始 harness 仅调用 4 个 API：
png_create_read_struct()
png_read_info()
png_set_interlace_handling()
png_read_row()

// 增强后添加：
png_set_expand()           // palette→RGB, gray 1/2/4→8, tRNS→alpha
png_set_gray_to_rgb()      // grayscale→RGB
png_set_strip_16()         // 16-bit→8-bit
png_set_add_alpha()        // 添加 alpha 通道
png_set_gamma()            // gamma 校正
png_read_update_info()     // 应用所有变换
```
效果：触发 pngrtran.c 中大量颜色转换分支（~4,100 行代码），lcov 分支覆盖从 12.1% → 24.5%。

**XML 增强 harness**（添加 XPath/DTD/XInclude）：
```c
// 原始 harness 仅调用 1 个 API：
xmlReadMemory(data, sz, "input.xml", NULL,
    XML_PARSE_NONET | XML_PARSE_RECOVER | XML_PARSE_NOERROR);

// 增强后：
int flags = XML_PARSE_RECOVER | XML_PARSE_NONET
          | XML_PARSE_DTDLOAD | XML_PARSE_DTDVALID
          | XML_PARSE_NOENT | XML_PARSE_XINCLUDE;
xmlDocPtr doc = xmlReadMemory(data, sz, "input.xml", NULL, flags);
if (doc) {
    // XPath 查询
    xmlXPathContextPtr ctx = xmlXPathNewContext(doc);
    xmlXPathEvalExpression("//node", ctx);
    // DTD 验证
    xmlValidateDocument(valid_ctx, doc);
    // XInclude 处理
    xmlXIncludeProcess(doc);
}
```
效果：触发 DTD 验证 (+7,052 行)、Entity 处理、XInclude (+1,787 行)、XPath 引擎 (+15,377 行)。lcov 分支覆盖从 4.0% → 13.0%。

---

## 补充二：种子文件完整清单

### 按目标分类（共 113 个种子文件）

| 目标 | 种子数 | 大小范围 | 内容说明 |
|------|------:|---------|---------|
| base64 | 13 | 0-669B | 不同长度的 base64 编码字符串（空、短、长、含填充） |
| base64_harness | 13 | 同上 | base64 相同种子集 |
| md5sum | 1 | 915B | 一个文本文件 |
| uniq | 1 | 6.9KB | 含重复行的文本 |
| who | 1 | 768B | utmp 二进制结构体 |
| png | 13 | 69-113B | Python 生成的最小有效 PNG（RGB/RGBA/灰度/16位/调色板/灰度+alpha/隔行） |
| xml | 15 | 52-534B | 手工编写（基本/实体/HTML/DTD/XPath/命名空间/CDATA/多实体/深嵌套） |
| SQLite | 25 | 9-201B | 6 类 SQL 语句（DDL: create_table/create_index/create_view/create_trigger/alter/drop; DML: insert/select/update/delete; Query: join/subquery/union/group_by/cte; Control: transaction/pragma/explain/analyze/vacuum/reindex; Expr: expressions/functions/blob_funcs; Multi: multi_stmt） |
| libarchive | 13 | 0-10KB | 各格式归档（tar/tar.gz/tar.bz2/tar.xz/7z/ar/bz2/cab/gzip/rar/xz/zip/空文件） |
| pcre2 | 8 | 6-17B | 正则模式（alternation/charset/digit/lazy/lookbehind/named_group/quantifier/simple） |
| freetype2 | 2 | 268B-4KB | TTF 字体（最小有效 TTF, DejaVuSans.ttf） |
| parallel_scaling | 1 | 10B | 合成 10 字节输入 |

---

## 补充三：完整实验原始数据

### v2 全量 benchmark 完整数据（10 目标 × 8 模式，81 行）

> 来源：`benchmark_results_v2/benchmark_data.csv`（300s, 单轮）

**gfts-png_read_fuzzer**:

| 模式 | np | 生成量 | ShowmapCov | edges | FstatsCov | AFL execs | AFL exec/s |
|------|---:|------:|------:|------:|------:|------:|------:|
| seed | 0 | 11 | 10.35% | 318/3072 | — | — | — |
| mpi | 2 | 23,174 | 14.94% | 459/3072 | — | — | — |
| mpi | 8 | 161,565 | 14.58% | 448/3072 | — | — | — |
| mpi | 32 | 641,385 | 14.36% | 441/3072 | — | — | — |
| hybrid | 2 | 344 | 16.44% | 505/3072 | 14.28% | 1,454,845 | 4,833 |
| hybrid | 8 | 604 | 16.50% | 507/3072 | 14.38% | 1,344,922 | 4,467 |
| hybrid | 32 | 1,453 | 16.96% | 521/3072 | 14.28% | 1,240,599 | 4,121 |
| afl-only | 1 | 174 | 14.10% | 433/3072 | 14.28% | 1,478,044 | 4,928 |

**gfts-xml_read_fuzzer**:

| 模式 | np | 生成量 | ShowmapCov | edges | FstatsCov | AFL execs | AFL exec/s |
|------|---:|------:|------:|------:|------:|------:|------:|
| seed | 0 | 20 | 3.08% | 1568/50880 | — | — | — |
| mpi | 2 | 36,145 | 5.74% | 2921/50880 | — | — | — |
| mpi | 8 | 297,259 | 5.48% | 2786/50880 | — | — | — |
| mpi | 32 | 1,206,278 | 5.43% | 2764/50880 | — | — | — |
| hybrid | 2 | 3,853 | 8.24% | 4191/50880 | 7.59% | 942,714 | 3,133 |
| hybrid | 8 | 7,724 | 8.70% | 4425/50880 | 7.39% | 818,351 | 2,719 |
| hybrid | 32 | 24,362 | 8.90% | 4526/50880 | 7.82% | 711,268 | 2,362 |
| afl-only | 1 | 2,577 | 7.60% | 3865/50880 | 7.63% | 863,397 | 2,880 |

**lava-base64**:

| 模式 | np | 生成量 | ShowmapCov | edges | FstatsCov | AFL execs | AFL exec/s |
|------|---:|------:|------:|------:|------:|------:|------:|
| seed | 0 | 13 | 11.58% | 126/1088 | — | — | — |
| mpi | 2 | 16,809 | 21.97% | 239/1088 | — | — | — |
| mpi | 8 | 107,994 | 23.25% | 253/1088 | — | — | — |
| mpi | 32 | 351,574 | 23.16% | 252/1088 | — | — | — |
| hybrid | 2 | 263 | 22.33% | 243/1088 | 14.74% | 760,256 | 2,525 |
| hybrid | 8 | 686 | 23.90% | 260/1088 | 15.51% | 618,372 | 2,054 |
| hybrid | 32 | 2,222 | 23.90% | 260/1088 | 14.74% | 741,459 | 2,462 |
| afl-only | 1 | 108 | 7.81%† | 85/1088 | 14.74% | 801,918 | 2,674 |

†缺少 `-d` 参数

**lava-md5sum**:

| 模式 | np | 生成量 | ShowmapCov | edges |
|------|---:|------:|------:|------:|
| seed | 0 | 1 | 7.14% | 96/1344 |
| mpi | 2/8/32 | 0 | 7.14% | 96/1344 |
| hybrid | 2/8/32 | 9 | 7.37% | 99/1344 |
| afl-only | 1 | 9 | 7.37% | 99/1344 |

**lava-uniq**:

| 模式 | np | 生成量 | ShowmapCov | edges |
|------|---:|------:|------:|------:|
| seed | 0 | 1 | 9.54% | 116/1216 |
| mpi | 2/8/32 | 0 | 9.54% | 116/1216 |
| hybrid | 2/8/32 | 61-66 | 10.61% | 129/1216 |
| afl-only | 1 | 56 | 10.61% | 129/1216 |

**lava-who**:

| 模式 | np | 生成量 | ShowmapCov | edges | FstatsCov |
|------|---:|------:|------:|------:|------:|
| seed | 0 | 1 | 6.73% | 719/10688 | — |
| mpi | 2/8/32 | 8 | 6.73% | 719/10688 | — |
| hybrid | 2 | 523 | 46.71% | 4992/10688 | 44.63% |
| hybrid | 8 | 1,457 | 47.25% | 5050/10688 | 44.63% |
| hybrid | 32 | 7,150 | 47.26% | 5051/10688 | 44.63% |
| afl-only | 1 | 44 | 44.57% | 4764/10688 | 44.62% |

**libarchive-archive_fuzzer**:

| 模式 | np | 生成量 | ShowmapCov | edges | FstatsCov | AFL execs | AFL exec/s |
|------|---:|------:|------:|------:|------:|------:|------:|
| seed | 0 | 13 | 6.45% | 888/13760 | — | — | — |
| mpi | 2 | 7,941 | 12.83% | 1766/13760 | — | — | — |
| mpi | 8 | 75,851 | 13.90% | 1912/13760 | — | — | — |
| mpi | 32 | 330,669 | 12.52% | 1723/13760 | — | — | — |
| hybrid | 2 | 1,644 | 17.22% | 2370/13760 | 16.28% | 773,522 | 2,570 |
| hybrid | 8 | 4,522 | 18.88% | 2598/13760 | 16.50% | 749,443 | 2,490 |
| hybrid | 32 | 20,707 | 23.36% | 3215/13760 | 19.17% | 644,022 | 2,138 |
| afl-only | 1 | 1,158 | 15.07% | 2073/13760 | 15.13% | 682,809 | 2,277 |

**sqlite-sqlite_fuzzer**:

| 模式 | np | 生成量 | ShowmapCov | edges | FstatsCov | AFL execs | AFL exec/s |
|------|---:|------:|------:|------:|------:|------:|------:|
| seed | 0 | 25 | 13.70% | 4339/31680 | — | — | — |
| mpi | 2 | 31,747 | 14.97% | 4743/31680 | — | — | — |
| mpi | 8 | 212,880 | 14.97% | 4742/31680 | — | — | — |
| mpi | 32 | 848,354 | 14.97% | 4743/31680 | — | — | — |
| hybrid | 2 | 2,180 | 18.70% | 5924/31680 | 26.00% | 719,539 | 2,391 |
| hybrid | 8 | 4,087 | 18.64% | 5905/31680 | 25.59% | 825,073 | 2,743 |
| hybrid | 32 | 9,216 | 18.84% | 5970/31680 | 26.15% | 868,636 | 2,885 |
| afl-only | 1 | 1,510 | 18.55% | 5876/31680 | 25.87% | 807,586 | 2,694 |

**pcre2-pcre2_fuzzer**:

| 模式 | np | 生成量 | ShowmapCov | edges | FstatsCov | AFL execs | AFL exec/s |
|------|---:|------:|------:|------:|------:|------:|------:|
| seed | 0 | 8 | 7.16% | 536/7488 | — | — | — |
| mpi | 2 | 11,755 | 15.29% | 1145/7488 | — | — | — |
| mpi | 8 | 140,206 | 18.66% | 1397/7488 | — | — | — |
| mpi | 32 | 554,148 | 18.32% | 1372/7488 | — | — | — |
| hybrid | 2 | 6,122 | 41.49% | 3107/7488 | 40.90% | 1,282,293 | 4,260 |
| hybrid | 8 | 10,028 | 41.69% | 3122/7488 | 40.24% | 1,239,735 | 4,118 |
| hybrid | 32 | 20,907 | 43.58% | 3263/7488 | 40.46% | 1,184,526 | 3,931 |
| afl-only | 1 | 5,529 | 41.73% | 3125/7488 | 41.83% | 1,460,493 | 4,870 |

**freetype2-freetype2_fuzzer**:

| 模式 | np | 生成量 | ShowmapCov | edges | FstatsCov | AFL execs | AFL exec/s |
|------|---:|------:|------:|------:|------:|------:|------:|
| seed | 0 | 2 | 2.22% | 481/21632 | — | — | — |
| mpi | 2 | 689 | 2.26% | 488/21632 | — | — | — |
| mpi | 8 | 5,364 | 2.26% | 488/21632 | — | — | — |
| mpi | 32 | 14,813 | 2.26% | 488/21632 | — | — | — |
| hybrid | 2 | 578 | 6.39% | 1382/21632 | 6.39% | 1,098,196 | 3,648 |
| hybrid | 8 | 1,200 | 11.23% | 2430/21632 | 11.24% | 1,025,971 | 3,408 |
| hybrid | 32 | 602 | 5.59% | 1210/21632 | 5.59% | 1,033,369 | 3,433 |
| afl-only | 1 | 541 | 5.75% | 1244/21632 | 5.75% | 1,048,373 | 3,496 |

### 吞吐量 Scaling 完整数据

> 来源：`benchmark_results_scaling/benchmark_data.csv`（120s, MPI-only）

**xml_read_fuzzer**:

| np | 生成量 | unique | tc/s | ShowmapCov |
|---:|------:|------:|------:|------:|
| serial | 0 | 0 | 0.00 | 3.09% |
| 2 | 13,202 | 9,011 | 128.1 | 5.69% |
| 8 | 165,692 | 91,101 | 1,603.6 | 5.88% |
| 32 | 791,204 | 407,450 | 7,579.1 | 6.21% |
| 128 | 1,562,696 | 319,778 | 14,917.3 | 6.25% |
| 190 | 1,441,075 | 225,754 | 13,812.9 | 6.05% |

**base64 (LAVA-M)**:

| np | 生成量 | unique | tc/s | ShowmapCov |
|---:|------:|------:|------:|------:|
| serial | 0 | 0 | 0.00 | 11.58% |
| 2 | 10,764 | 5,333 | 104.5 | 21.97% |
| 8 | 67,287 | 29,523 | 652.6 | 23.25% |
| 32 | 229,471 | 65,350 | 2,222.6 | 23.25% |
| 128 | 570,569 | 52,496 | 5,523.1 | 23.25% |
| 190 | 668,799 | 50,736 | 6,471.4 | 23.25% |

**base64_harness（自定义）**:

| np | 生成量 | unique | tc/s | ShowmapCov | speedup | efficiency |
|---:|------:|------:|------:|------:|------:|------:|
| serial | 2,822 | 2,822 | 23.5 | 49.48% | 1.0x | 100% |
| 2 | 17,683 | 11,069 | 171.6 | 49.48% | 7.3x | 100% |
| 8 | 106,009 | 54,825 | 1,027.3 | 50.52% | 43.7x | 85.5% |
| 32 | 426,764 | 187,158 | 4,117.2 | 51.04% | 175.1x | 77.4% |
| 128 | 1,278,389 | 249,356 | 12,249.2 | 51.04% | 520.9x | 57.1% |
| 190 | 1,352,476 | 138,935 | 13,023.5 | 51.04% | 553.8x | 41.0% |

### 深度集成对比实验完整数据

> 来源：6 个 benchmark_results_* 目录（300s, Hybrid np=8, 单轮）

**libarchive 全版本对比**:

| 版本 | Hybrid ShowmapCov | AFL-only ShowmapCov | H-A 差值 | Hybrid FstatsCov | AFL-only FstatsCov | H-A 差值 | 生成量 |
|------|------:|------:|------:|------:|------:|------:|------:|
| baseline | 23.19% | 16.49% | +6.70 | 17.94% | 16.56% | +1.38 | 7,274 |
| multsolve v1 | 20.33% | 16.93% | +3.40 | 17.88% | 17.00% | +0.88 | 5,107 |
| multsolve v2 | 22.87% | 16.63% | +6.24 | 20.02% | 16.70% | +3.32 | 7,118 |
| multsolve v3 | 20.45% | 17.04% | +3.41 | 16.88% | 17.11% | -0.23 | 5,494 |
| enhanced | **26.70%** | 17.12% | **+9.58** | **21.75%** | 17.19% | **+4.56** | 7,142 |
| fastsol | 22.57% | 16.91% | +5.66 | 18.43% | 16.98% | +1.45 | 7,026 |

**SQLite 全版本对比**:

| 版本 | Hybrid ShowmapCov | AFL-only ShowmapCov | H-A 差值 | Hybrid FstatsCov | AFL-only FstatsCov | H-A 差值 | 生成量 |
|------|------:|------:|------:|------:|------:|------:|------:|
| baseline | 21.14% | 19.71% | +1.43 | 28.83% | 27.35% | +1.48 | 4,183 |
| multsolve v1 | 20.31% | 20.83% | -0.52 | 27.91% | 28.40% | -0.49 | 4,206 |
| multsolve v2 | 19.69% | 19.24% | +0.45 | 26.68% | 26.85% | -0.17 | 4,542 |
| multsolve v3 | 20.46% | 20.15% | +0.31 | 27.86% | 27.65% | +0.21 | 4,315 |
| enhanced | 20.65% | 20.87% | -0.22 | 28.29% | 28.82% | -0.53 | 4,369 |
| fastsol | **21.21%** | 20.64% | **+0.57** | **29.58%** | 28.62% | **+0.96** | 3,791 |

### AFL-only 基线波动数据

**libarchive AFL-only ShowmapCov**（6 次独立实验）：
16.49%, 16.93%, 16.63%, 17.04%, 17.12%, 16.91%
- 均值：16.85%
- 标准差：±0.23pp

**SQLite AFL-only ShowmapCov**（6 次独立实验）：
19.71%, 20.83%, 19.24%, 20.15%, 20.87%, 20.64%
- 均值：20.24%
- 标准差：±0.63pp

---

## 补充四：Benchmark 工具链详细设计

### run_benchmark.py（2,443 行）

#### 命令行参数

```
--symcc PATH       SymCC 编译器路径
--np-list LIST     并行度列表（默认 1,2,4,8）
--targets LIST     目标名列表
--rounds N         每配置轮次（默认 3）
--timeout T        单次超时秒数（默认 60）
--output DIR       输出目录
--skip-build       跳过编译
--simulation       用 gcc 替代 SymCC（仅测试框架）
--public           添加公开测试集目标
--no-default       跳过内置目标
--no-coverage      跳过覆盖率测量
--hybrid           运行 Hybrid AFL+SymCC 模式
--afl-only         运行 AFL-only 基线
--no-serial        跳过串行基线
--no-mpi           跳过 MPI-only 并行
--timeseries N     每 N 秒采样覆盖率
```

#### 执行流程

```
1. 编译目标（--skip-build 跳过）
2. 发现目标二进制（SymCC/AFL/CmpLog/Coverage 变体）
3. 对每个目标：
   a. 测量种子覆盖率（afl-showmap -C）
   b. 串行基线（--no-serial 跳过）
   c. MPI 并行 np=2,8,32（--no-mpi 跳过）
   d. Hybrid AFL+SymCC np=2,8,32（--hybrid 启用）
   e. AFL-only 基线（--afl-only 启用）
4. 生成报告（CSV + JSON + TXT）
```

#### 覆盖率测量的 TC 采样机制

当测试用例数超过 20,000 时，`measure_coverage_afl()` 使用随机采样：
```python
if total_cases > max_cases:
    sample_files = random.sample(test_files, max_cases)
    # 复制采样文件到临时目录（afl-showmap 不支持 symlink）
    for f in sample_files:
        shutil.copy2(src, os.path.join(sample_dir, f))
    actual_dir = sample_dir
```
这解决了 MPI np=32 产出 64 万+ TC 时 afl-showmap 耗时数小时的问题。

#### Hybrid 模式的 AFL 启动与协调

```python
def run_hybrid(symcc_binary, afl_binary, ...):
    # 1. 启动 AFL（后台进程）
    afl_cmd = ["afl-fuzz", "-M", "fuzzer01", "-i", seed_dir, "-o", afl_out_dir, "-m", "none"]
    if cmplog_binary:
        afl_cmd.extend(["-c", cmplog_binary, "-l", "2AT"])
    afl_proc = subprocess.Popen(afl_cmd, start_new_session=True)

    # 2. 等待 AFL 就绪（轮询 fuzzer_stats，最多 60s）
    while not os.path.isfile(stats_path) and wait < 60:
        time.sleep(1)

    # 3. 启动 MPI SymCC Workers
    mpi_cmd = ["mpirun", "-np", str(symcc_np), "python3", mpi_script,
               "-a", "fuzzer01", "-o", afl_out_dir, "-n", "symcc01",
               "--save-all", symcc_all_dir, "--", symcc_binary]
    mpi_proc = subprocess.Popen(mpi_cmd, start_new_session=True)

    # 4. 等待超时
    time.sleep(timeout)

    # 5. 终止：先 SIGTERM AFL，再 SIGKILL MPI
    os.killpg(afl_proc.pid, signal.SIGTERM)
    os.killpg(mpi_proc.pid, signal.SIGKILL)

    # 6. 解析 fuzzer_stats
    # edges_found, total_edges, bitmap_cvg, execs_done, execs_per_sec
```

### profile_bottleneck.py（726 行）

测量 Master 各阶段耗时：
- **scan**：AFL queue 扫描时间
- **dispatch**：MPI send 分发时间
- **recv**：MPI recv 接收时间
- **triage**：batch_triage 处理时间
- **idle**：Master 等待时间

输出：`profile_report.txt`（人类可读）+ `profile_data.csv`（机器可读）

### run_multi_instance.py（407 行）

将种子按功能分组，每组独立运行 Hybrid 实例：
```python
# SQLite 25 种子 → 6 组
SEED_GROUPS = {
    "ddl": [create_table, create_index, ...],
    "dml": [insert, select, update, delete],
    "query": [join, subquery, union, ...],
    "control": [transaction, pragma, ...],
    "expr": [expressions, functions, ...],
    "multi": [multi_stmt.sql],
}
# 每组 np=32 → 6×32=192 核
```
结果：吞吐量 +2.8x（19,242 vs 6,969 tc/s），覆盖率 -0.14%（15.15% vs 15.29%）。

---

## 补充五：solver.cpp 关键常量与算法参数

| 常量 | 值 | 用途 |
|------|---:|------|
| `kSolverTimeout` | 10,000 ms | Z3 求解超时（默认 10 秒） |
| `kMaxGroupSize` | 12 | 联合求解最大分支数 |
| `kMinGroupSize` | 2 | 触发联合求解的最小分支数 |
| `kNearbyWindowBytes` | 64 | 邻近字段检测的字节窗口 |
| `kMaxDictVariants` | 20 | 字典引导最大变体数 |

### fastSolve 10 种运算符的值计算规则

| 运算符 | 条件 | 目标值计算 | 失败条件 |
|--------|------|-----------|---------|
| Equal (taken=true → negate) | byte != C | `C ^ 0x01`（若等于 C 则 `C ^ 0xFF`） | — |
| Equal (taken=false → satisfy) | byte == C | `C` | — |
| Distinct (taken=true → negate) | byte == C | `C` | — |
| Ugt | byte > C | `C + 1` | C = 0xFF |
| Uge | byte >= C | `C` | — |
| Ult | byte < C | `C - 1` | C = 0 |
| Ule | byte <= C | `C` | — |
| Sgt | (signed) byte > C | `(int8_t)C + 1` | C = 127 |
| Sge | (signed) byte >= C | `C` | — |
| Slt | (signed) byte < C | `(int8_t)C - 1` | C = -128 |
| Sle | (signed) byte <= C | `C` | — |

---

## 补充六：Git 提交历史（最近 30 条）

| 序号 | Hash | 消息 |
|------|------|------|
| 1 | 969b333 | Fix incorrect explanation: Hybrid degradation is CPU contention, not seed pollution |
| 2 | c6bd052 | Restructure report for readability: move code to appendix, lead with findings |
| 3 | 433aa25 | Fix 14 issues from report review |
| 4 | 6075fcc | Add work progress report #2: full benchmark, who analysis, cmplog |
| 5 | 771e485 | Add AFL++ CmpLog integration: auto-extracted comparison dictionaries |
| 6 | 9b1ef74 | Add pcre2 and freetype2 benchmarks: 10 targets total |
| 7 | 59197d0 | Add full benchmark results: 8 targets × 4 modes × np=2,8,32 |
| 8 | ed9dffc | Fix 17 issues from detailed report review |
| 9 | 772a8d7 | Add multi-instance parallel section to work progress report |
| 10 | 7173033 | Revise work progress report based on expert review |
| 11 | 7beda5e | Add work progress report with findings and conclusions |
| 12 | fa430f0 | Add dictionary-guided constraint solving to SymCC (SYMCC_DICT) |
| 13 | 1f55cde | Add multi-instance scheduler: seed partitioning does NOT improve coverage |
| 14 | e93b7d1 | Add comprehensive work summary report |
| 15 | 201517d | Add SQLite benchmark: 200K LOC, 31680 edges, 84x throughput scaling |
| 16 | c4cd486 | Add libarchive benchmark: parallel scaling with 120s shows +19% gain |
| 17 | 090480c | Add synthetic parallel scaling benchmark: coverage grows with np |
| 18 | ab8eb85 | Add hybrid scaling benchmark results: np=2,8,32,128,190 |
| 19 | 17d9d8c | Add scaling benchmark results: np=2,8,32,128,190 (MPI complete) |
| 20 | 9559730 | Add base64 harness with 3 coverage optimizations: 49.48% coverage |
| 21 | e81b4b9 | Fix LAVA-M base64 coverage: add -d flag via .args config, 7%→24% |
| 22 | 78c8dfe | Fix LAVA-M hybrid/AFL-only: discover_public_afl_targets was hardcoded |
| 23 | 40a0854 | Fix AFL-only stats: report execs_done and execs_per_sec from fuzzer_stats |
| 24 | d41c63a | Add parallel architecture technical report |
| 25 | 276cf01 | Worker-side coverage dedup: recv 18.7s→2.3s, MPI data 34MB→3.6MB |
| 26 | 5855b7c | Fix #1 master bottleneck: AFL queue scan 39s→3.7s (10x speedup) |
| 27 | c979241 | Use afl-showmap streaming mode for 3-4x hybrid throughput improvement |
| 28 | 24dfe20 | Add parallel bottleneck profiling tool with scaling analysis |
| 29 | 558383e | Fix LAVA-M zero test case generation: patch unlocked-io.h for SymCC |
| 30 | 3f78b10 | Fix bugs from code review: protocol, deadlocks, resource leaks |

**项目总计 522 个 commit**。
