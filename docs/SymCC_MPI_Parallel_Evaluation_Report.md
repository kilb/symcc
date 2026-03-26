# SymCC MPI 并行 Concolic Execution 性能评估与优化

## 项目背景

SymCC 是一种基于编译器插桩的符号执行工具，通过 LLVM pass 在编译期注入符号执行逻辑，运行时使用 QSYM 后端进行约束求解。本项目中的 `mpi_concolic_execution.py` 和 `mpi_fuzzing_helper.py` 实现了基于 MPI 的并行 concolic execution 框架，采用 Master-Worker 架构分发符号执行任务。

**项目目标**：系统评估该并行框架在不同并行度（np=2 至 190）下的吞吐量扩展性和覆盖率有效性，发现并修复代码缺陷，并通过集成 AFL++ fuzzer 探索覆盖率提升的可能性。

**硬件环境**：AMD Threadripper PRO 9995WX (96C/192T)，250GB DDR5，Ubuntu 24.04

**测试目标选择依据**：选用 Google Fuzzer Test Suite (FTS) 中的 PNG (libpng 1.2.56) 和 XML (libxml2 2.9.2) 两个目标。Google FTS 是学术界和工业界广泛使用的 fuzzing 基准集；PNG 代表二进制格式，XML 代表文本格式，覆盖两种主要的输入格式类型。此外还尝试了 LAVA-M 基准集（base64、md5sum、uniq、who），四个目标均编译成功（含 glibc 兼容性 patch），但 SymCC 运行时未产生输出，原因尚未确诊（详见第六节）。Google FTS 中 FreeType、re2、SQLite 等其他目标的编译脚本尚未实现，并非编译不通过。

**覆盖率度量标准**：本报告中所有覆盖率数据均为 **lcov branch coverage（分支覆盖率）**。测量方法为：将测试用例喂给 `gcc --coverage` 编译的 Coverage 二进制执行，生成 `.gcda` profile 数据，再通过 `lcov --capture` 采集 harness + 库源码目录的覆盖率并以 `lcov --summary` 提取分支覆盖率百分比。Coverage 构建不参与实际的 fuzzing 或符号执行，仅用于事后度量——SymCC 二进制和 AFL 二进制均不产生 profiling 数据，因此必须维护一份独立的 Coverage 构建。

**吞吐量定义**：本报告中的"吞吐量 (tc/s)"指 SymCC（或 AFL+SymCC）在单位时间内生成的**测试用例总数** ÷ wall time，包含所有 worker 输出，无论是否去重唯一。

**采样策略**：MPI 高并行度下 5 分钟可生成数百万测试用例，全部喂给 Coverage 二进制耗时过长。`measure_coverage()` 设定 `max_cases=200000` 上限：超过 20 万个测试用例时采用分层采样（按文件名排序均匀取样，补充桶边界样本）。此策略引入约 ±1.5% 的覆盖率噪声。

---

## 一、基准测试基础设施的构建

### 1.1 编译系统开发（compile_public_benchmarks.sh，+450 行）

测试目标的编译涉及三种不同的构建模式，需统一为自动化流程。

**PNG 目标（libpng 1.2.56）的编译**：libpng 依赖 zlib，需先编译 zlib 再链接 libpng，最后将 harness 与静态库链接。三种构建模式均需走此流程：

- **SymCC 插桩构建**：使用 `symcc` 作为编译器，生成包含 `__sym_ctor` 符号的二进制，用于符号执行
- **Coverage 构建**：使用 `gcc --coverage` 编译 harness **及底层库（libpng/libxml2）**，生成 `.gcno` profile 数据文件，用于覆盖率测量。库的构建目录路径记录在 `.covlibdirs` 元数据文件中，供 lcov 采集使用
- **AFL 插桩构建**：使用 `afl-clang-fast` 编译，生成包含 AFL edge bitmap 插桩的二进制

**构建目录设计缺陷及规避方案**：当前编译脚本中三种构建模式共享同一份库源码目录（`$work_dir/libpng-1.2.56`），每次 `./configure && make` 都会覆盖源码目录中的中间文件。更合理的做法是将 tarball 解压三份至不同目录（如 `libpng-symcc/`、`libpng-afl/`、`libpng-cov/`）独立构建。当前方案的规避措施为严格控制构建顺序——SymCC 目标 → AFL 目标 → Coverage 目标（必须最后），因为 AFL 构建的 `make distclean` 会删除 Coverage 构建产生的 `.gcno` 文件。此问题在 XML 目标上被发现：成功编译后覆盖率突然归零，经对比 `.gcno` 文件时间戳确认为构建顺序导致。

**XML 目标（libxml2 2.9.2）的编译**：libxml2 的 `./configure` 选项较多，需禁用 Python 绑定、ICU 支持等不必要的依赖，同时保留解析器核心功能。harness 源文件从 Coverage 构建目录中复用，确保三种构建使用同一份 harness 代码。

**LAVA-M 目标**：从 GNU coreutils 8.24 源码编译 base64、md5sum、uniq、who 四个程序。glibc 2.28+ 重新定义了某些内部符号导致编译报错，需额外打 glibc 兼容性 patch。下载源码包遇到原始链接失效问题，实现了多级 fallback 下载逻辑（Gitee 镜像 → 多个 Wayback Machine 时间戳 → 原始链接）。四个目标均编译成功，但运行时存在问题（详见第六节）。

### 1.2 基准测试框架开发（run_benchmark.py，+750 行）

从零实现了一个完整的基准测试编排框架，支持三种执行模式和自动化的覆盖率测量与报告生成。

**Serial 模式**：调用原有的 `pure_concolic_execution.sh` 脚本，作为基线对照。

**MPI 模式**：通过 `mpirun` 启动 `mpi_concolic_execution.py`，支持配置 np、timeout、per-exec-timeout 等参数。测试完成后自动收集生成的测试用例并统计去重唯一数。

**Hybrid 模式**（后期新增，约 150 行）：同时编排 AFL fuzzer 和 MPI SymCC 两个独立进程树：

1. 启动 `afl-fuzz -M fuzzer01` 作为后台进程，设置 `AFL_NO_UI=1` 禁用交互界面
2. 轮询等待 AFL 产生 `fuzzer_stats` 文件（最多 30 秒），确认 AFL 完成初始化
3. 启动 `mpirun ... mpi_fuzzing_helper.py`，指向 AFL 的输出目录，使 SymCC 处理 AFL 生成的输入
4. 等待总超时到期，通过两阶段策略终止进程：先发送 SIGTERM 等待 10 秒（允许 MPI 进程执行优雅退出逻辑——向 worker 发送 TAG_STOP、flush 统计文件、释放 MPI 资源），超时后再发送 SIGKILL 强制终止。使用 `os.killpg` 进程组级信号确保子进程不成为孤儿进程
5. 收集结果：AFL 队列 + SymCC 队列 + SymCC 全量输出 → 合并到 combined_dir（添加 `afl_`、`symcc_`、`symcc_all_` 前缀避免文件名冲突）
6. 解析 AFL 的 `fuzzer_stats` 提取 bitmap_cvg 等统计信息

### 1.3 覆盖率测量系统的修复过程

覆盖率测量经历了三次递进修复：

**第一次（gcov 单文件）**——所有并行度下覆盖率完全相同（77.3%）。原因：初始的 Coverage 构建仅对 harness 文件（`png_read_fuzzer.c`，约 30 行）使用了 `--coverage`，底层库（libpng/libxml2）仍以普通 gcc 编译，未产生 `.gcno` 文件。gcov 仅能统计存在 `.gcno` 的源文件覆盖率，而 harness 逻辑简单（打开文件 → 调用解析函数 → 退出），任何有效输入都走相同的 harness 分支。真正的代码路径差异体现在库内部（libpng 数千行、libxml2 数万行），gcov 完全不可见。

**修复方案**：实现 `_measure_with_lcov()` 函数，将 libpng/libxml2 也以 `gcc --coverage` 重新编译，使用 `lcov --capture` 同时采集 harness 目录和库构建目录的覆盖率数据。每次测量前调用 `_clean_gcda_files()` 清零计数器确保独立性。

**第二次（MPI 调用点遗漏）**——串行基线显示正确的 lcov 结果，但 MPI 模式仍输出旧 gcov 数据。原因：代码修改时仅匹配到串行调用点，MPI 调用点因上下文不同未被替换。此缺陷隐藏了一轮完整的 benchmark 运行周期，直到对比两种模式结果的不合理差异时才发现。

**第三次（lcov 兼容性）**——lcov 新版本将 `lcov_branch_coverage=1` 标记为弃用，改为 `branch_coverage=1`。全局替换修复。

### 1.4 种子库的构建

修复覆盖率测量后，各并行度下覆盖率仍基本一致。分析发现生成的测试用例文件大小仅 2-3 种，与种子文件大小完全吻合。

**根因**：SymCC 的 concolic execution 沿执行路径翻转单个分支条件，生成的变异体与输入仅差 1-3 字节。若所有种子结构相同（如均为最简单的 PNG），所有变异体均在同一结构空间的"邻域"内，无法触达需要不同输入结构才能执行的代码路径。

**解决方案**——手工构建多样化种子集：

- PNG（9 个种子）：使用 Python PIL 库创建 RGBA 8bit、灰度 8bit、灰度+alpha、调色板模式、16bit 深度、32x32 尺寸、带 tEXt 元数据、隔行扫描（Adam7）等不同参数组合的 PNG 文件，每个种子对应 libpng 中不同的解码路径
- XML（15 个种子）：手写 15 个 XML 文件覆盖解析器的不同特性——基础标签、CDATA 段、命名空间、20 层深嵌套、多属性、注释与处理指令、混合内容、自闭标签、字符实体、UTF-8 多字节、长文本（500 字符）、DTD 声明、数值内容

---

## 二、并行框架代码审查与 Bug 修复

对 `mpi_concolic_execution.py`（~600 行）和 `mpi_fuzzing_helper.py`（~600 行）进行了完整的代码审查，发现并修复了 7 个 bug，涉及竞态条件、资源泄漏、逻辑缺失和安全隐患。

### 2.1 TOCTOU 竞态条件修复（mpi_concolic_execution.py）

**问题**：`import_inputs()` 和 worker 写入路径中使用 `if not os.path.exists(dest): open(dest, "wb").write(...)` 模式。在多 worker 并行场景下，两个 worker 可能同时通过 `exists` 检查后同时写入同一文件，导致数据截断或文件损坏。

**修复**：提取 `_atomic_write(dest, content)` 辅助函数——先写入同目录的临时文件，再通过 `os.rename()` 原子替换。`rename` 在同一文件系统上为原子操作，即使多个 worker 同时写入，最终文件内容始终完整。该辅助函数在 `import_inputs()` 和 worker 两处调用点复用。

需要说明的是，`_atomic_write` 仅增加一次 `os.rename()` 调用，在 Linux 同一文件系统上为微秒级 inode 操作，对整体吞吐量影响可忽略。当前文件名即 SHA-256 hash（content-addressable storage 设计），相同内容必然同名，天然实现去重。若改为每个 worker 写不同文件名以避免竞争，将丧失去重能力，shared_dir 存储量膨胀 N 倍。

### 2.2 环境变量字典引用修改（mpi_concolic_execution.py）

**问题**：`run_symcc()` 函数中 `env = base_env` 仅创建引用而非拷贝，后续 `env["SYMCC_OUTPUT_DIR"] = ...` 修改会污染调用方传入的字典。在循环调用场景下，后续调用将看到前次调用留下的脏数据。

**修复**：改为 `env = dict(base_env)` 浅拷贝。

### 2.3 hang 保存逻辑缺失（mpi_fuzzing_helper.py）

**问题**：代码中构造了 `hang_name`（包含 worker rank、时间戳和 hash 前缀），但随后缺少实际写入文件的语句，`queue_id` 也未递增。所有检测到的 hang 输入被静默丢弃。

**修复**：添加 `shutil.copy2(input_path, os.path.join(hangs_dir, hang_name))` 保存文件，并在之后 `queue_id += 1` 保持 ID 连续。

### 2.4 优雅退出机制缺失（mpi_fuzzing_helper.py）

**问题**：master 函数主循环为 `while True` 无条件循环，无退出机制。外部发送 SIGTERM 时 master 直接被杀死，worker 永远收不到 TAG_STOP 消息，导致 worker 进程挂起，MPI 最终超时强制终止整个任务，丢失最后一批未写入的结果。

**修复**：
1. 在 master 函数开头注册 SIGTERM/SIGINT 信号处理器，设置 `shutdown_requested = True`
2. 主循环条件改为 `while not shutdown_requested`
3. 退出循环后向所有 worker 发送 TAG_STOP 消息
4. 调用 `stats.log()` 写入最终统计并关闭 `stats_file`

### 2.5 stats_file 资源泄漏（mpi_fuzzing_helper.py）

**问题**：`stats_file = open(...)` 打开后从未关闭，长时间运行中缓冲数据可能未 flush 到磁盘。

**修复**：在优雅退出逻辑尾部，先调用 `stats.log()` 记录最终状态，再 `stats_file.close()`。

### 2.6 worker 修改全局 os.environ（mpi_fuzzing_helper.py）

**问题**：worker 通过 `os.environ["SYMCC_AFL_COVERAGE_MAP"] = ...` 设置环境变量。`os.environ` 为进程全局对象，虽然当前 MPI 架构下每个 worker 为独立进程，但若未来改为线程模型或在同一进程中多次调用 worker 函数，将产生环境变量污染。

**修复**：构建独立的 `worker_env = os.environ.copy()` 字典，在其中设置所需变量，传递给 `subprocess.run(env=worker_env)`。

### 2.7 MPI.Finalize 前缺少 Barrier（mpi_fuzzing_helper.py）

**问题**：`main()` 函数在 master/worker 返回后直接调用 `MPI.Finalize()`，若某 rank 先完成而其他 rank 仍有未完成的非阻塞通信，可能导致未定义行为。

**修复**：在 `MPI.Finalize()` 前插入 `comm.Barrier()`，确保所有 rank 同步后再清理 MPI 资源。

### 2.8 清除未使用的 import

删除了 `import re`（第 35 行），该模块在文件中从未使用。

---

## 三、AFL++ Hybrid Fuzzing 的集成

### 3.1 动机

纯 concolic execution 的基准测试结果显示覆盖率在 np=2 时即饱和（PNG 16.7%、XML 7.4%），增加到 190 个进程也无实质提升。这是 concolic execution 的固有局限——仅能翻转单个分支条件，产生与输入仅差 1-3 字节的"邻域"变异。要突破这一天花板，需引入能产生结构性不同输入的工具。

AFL++ 是业界领先的覆盖率引导 fuzzer，通过字节级随机变异（bit flip、字典插入、拼接等）快速生成大量结构不同的输入。将 AFL 的广度探索与 SymCC 的精确求解结合，是学术界公认的有效策略。

### 3.2 AFL++ 编译流程实现

在 `compile_public_benchmarks.sh` 中新增 `build_google_fts_afl()` 函数（约 80 行），使用 `afl-clang-fast` 编译 libpng 和 libxml2。harness 代码从 Coverage 构建目录中复用，确保三种构建使用同一份 harness。新增 `--with-afl` 命令行标志控制是否执行 AFL 构建。

### 3.3 `--save-all` 机制——解决覆盖率测量盲区

此为 Hybrid 集成中发现的最关键问题。

初次 Hybrid 测试后，覆盖率结果与预期不符——Hybrid 模式的分支覆盖率（PNG 15.3%）反而低于纯 MPI concolic（16.7%）。

**排查过程**：AFL 正常运行并生成约 170 个队列条目；MPI workers 正常启动并处理了 AFL 队列中的输入；但 master 输出统计显示 `symcc_interesting=0`。

**问题定位**：`mpi_fuzzing_helper.py` 的 master 使用 `afl-showmap` 判断 SymCC 输出是否"interesting"。AFL 的 edge bitmap 使用 `(prev_block XOR cur_block) % MAP_SIZE` 映射到 64KB bitmap，记录 hit count bucket。SymCC 生成的变异通常仅差 1-3 字节，经过的 edge 集合与原输入几乎相同——连 hit count bucket 都未发生变化，因此 afl-showmap 判定全部为"不 interesting"。

因此 master 丢弃了全部 SymCC 输出，Hybrid 模式的覆盖率测量仅包含 AFL 自身输出。

**解决方案**：在 `mpi_fuzzing_helper.py` 中实现 `--save-all DIR` 参数——master 在执行 afl-showmap 筛选的同时，将所有 SymCC 输出以 SHA-256 hash 为文件名保存到指定目录。AFL 队列仍使用 afl-showmap 筛选（保证 AFL 反馈循环正常工作），覆盖率测量时则使用全量输出。

修复后重测结果：
- PNG Hybrid: 15.3% → **16.9%**
- XML Hybrid: 6.2% → **10.7%**

### 3.4 当前 Hybrid 实现的已知缺陷

**SymCC 输出未回灌至 AFL**：当前实现中，afl-showmap 判定不 interesting 的 SymCC 输出虽通过 `--save-all` 保存用于覆盖率测量，但并未写入 AFL 的 queue 目录。AFL 实际上完全不知道 SymCC 生成了哪些输出——双方的反馈循环处于断开状态。改进方案为：即使 afl-showmap 判定不 interesting，也选择性地将部分 SymCC 输出回灌给 AFL，使 AFL 在此基础上进行进一步变异。

**缺少 AFL-only 基线**：当前实验未单独运行 AFL-only 对照组，无法分离 AFL 和 SymCC 各自对覆盖率的贡献（详见第四节 4.3 说明）。

---

## 四、基准测试结果

### 4.1 吞吐量扩展性

吞吐量为所有 worker 在单位时间内生成的测试用例总数（含重复）。

| 目标 | np=2 | np=4 | np=8 | np=16 | np=32 | np=64 | np=128 | np=190 |
|------|------|------|------|-------|-------|-------|--------|--------|
| PNG (tc/s) | 209 | 564 | 1,238 | 2,606 | 4,736 | 8,183 | 8,175 | 7,804 |
| XML (tc/s) | 318 | 1,091 | 2,467 | 5,012 | 9,287 | 16,338 | 19,780 | 18,983 |

- PNG: np=2 → np=32 实现 22.7x 加速（理想值 31x），并行效率 73%
- XML: np=2 → np=32 实现 29.2x 加速（理想值 31x），并行效率 94%
- np=64 以后吞吐量增速明显放缓，np=190 甚至略低于 np=128。确切原因尚待进一步分析——需对 SymCC 执行时间、MPI 消息收发时间、master 间 hash 同步时间、worker 空闲等待时间分别计时，方能定位瓶颈所在。当前代码仅统计了总 wall time 和每次 SymCC 执行的 elapsed，未对 MPI 通信部分单独计时

### 4.2 纯 Concolic 覆盖率

所有覆盖率数据均为 lcov branch coverage，采用 200K 分层采样。

| 目标 | 种子基线 | np=2 | np=4 | np=8 | np=16 | np=32 | np=64 | np=128 | np=190 |
|------|---------|------|------|------|-------|-------|-------|--------|--------|
| PNG 分支 | 12.1% | 16.7% | 16.8% | 16.9% | 17.2% | 17.9% | 17.4% | 17.0% | 17.5% |
| XML 分支 | 4.0% | 7.4% | 8.0% | 8.1% | 7.7% | 8.1% | 7.6% | 7.7% | 7.2% |

覆盖率在 np=2 时即完成主要跳升（PNG +38%，XML +85%），之后 np=2 至 190 范围内的波动幅度（PNG 16.7%~17.9%，XML 7.2%~8.1%）均在 ±1.5% 采样噪声范围内，**不应解读为覆盖率随并行度的真实变化**。需通过 3-5 轮独立运行取中位数方能区分真实差异与统计噪声。

### 4.3 Hybrid AFL+SymCC 覆盖率

**重要说明**：当前实验**缺少 AFL-only 对照组**。按照混合测试领域的标准评估方法，应设三组对比：AFL alone（基线）、SymCC alone（纯 concolic）、AFL+SymCC（Hybrid），提升幅度应为 `(Hybrid - AFL alone) / AFL alone`。由于 AFL-only 基线缺失，下表仅列出纯 concolic 和 Hybrid 的实测覆盖率数据，暂不计算提升比例。

| 目标 | 模式 | np=2 | np=8 | np=32 | np=128 |
|------|------|------|------|-------|--------|
| PNG 分支 | 纯 MPI | 16.7% | 16.8% | 17.0% | 17.6% |
| PNG 分支 | Hybrid | 16.9% | 16.5% | 16.8% | 16.3% |
| XML 分支 | 纯 MPI | 7.5% | 8.0% | 8.0% | 7.7% |
| XML 分支 | **Hybrid** | **10.7%** | **10.8%** | **10.8%** | **10.6%** |

> 注：Hybrid 测试尚缺 np=4、np=16、np=64 数据，待后续补测以与纯 MPI 测试保持一致。

XML 在 Hybrid 模式下覆盖率显著高于纯 concolic（10.7% vs 7.5-8.0%）。PNG 则基本持平。

**格式容错性差异**是两者表现不同的根本原因：

- **XML（文本格式，对变异高度容错）**：AFL 随机翻转字节后 libxml2 的 `XML_PARSE_RECOVER` 模式仍可恢复解析；AFL 可插入 `<`、`>`、`</`、`="` 等 token 生成结构多样的新标签。实测 AFL 在 5 分钟内生成约 3,500 个 XML queue 条目
- **PNG（二进制格式，校验机制严格）**：PNG 文件中的 chunk type（4 字节）和 CRC（4 字节）构成严格校验——翻转 CRC 中任何 1 bit 即导致 `png_error()` 终止解析，翻转 type 中 1 bit 产生未知 chunk type。实测 AFL 在 5 分钟内仅生成约 170 个 PNG queue 条目

即：AFL 能为 XML 提供大量结构不同的有效输入（约 3,500 个），SymCC 在此基础上探索更多解析路径；但对 PNG 仅能提供极少量有效变异（约 170 个），结构多样性不足。

### 4.4 Multi-Master 去重效率

np>46 时系统启用 multi-master 架构，master 间通过异步 `isend`/`iprobe` 同步已知 hash（同步间隔 2 秒）。在该延迟窗口内，多个 master 组独立处理相同输入并产生相同输出，导致重复探索。

具体表现（PNG 数据）：

| np | 架构模式 | 总生成数 | 去重唯一数 | 唯一率 |
|----|---------|---------|-----------|--------|
| 32 | 单 master | 1,268,051 | 858,946 | 67.7% |
| 64 | 多 master (2 个) | 2,190,734 | 819,990 | 37.4% |
| 128 | 多 master (3 个) | 2,191,383 | 565,907 | 25.8% |
| 190 | 多 master (5 个) | 2,085,161 | 388,590 | 18.6% |

从 np=32 到 np=190：总生成数增长 64%，而唯一数下降 55%。master 数量越多，同步延迟窗口内的重复探索越严重。

---

## 五、关键结论

**1. MPI 并行框架的吞吐量扩展性良好**——在 32 workers 范围内接近线性（XML 并行效率达 94%），验证了 Master-Worker 架构、hash 去重和非阻塞通信的设计正确性。np=64 以后增速放缓，具体瓶颈尚待通过分阶段计时分析确认。

**2. 纯 concolic execution 的覆盖率存在固有天花板**——SymCC 沿执行路径翻转单个分支条件，生成的变异体与输入仅差 1-3 字节。百万级 hash 不同的测试用例可能触发的分支集合相同（如修改 PNG IDAT chunk 中 1 字节解压数据，libpng 的 IHDR→IDAT→解压→读行 解析流程不变）。更多 worker 加速了当前路径邻域的穷举，但无法跨越到需要全新输入结构（如新的 PNG chunk type，需 4 字节标记 + 正确 CRC）才能触发的代码路径。

**3. Hybrid AFL+SymCC 对变异容错度高的格式有效**——XML 覆盖率从 7.5-8.0% 提升至 10.7%，因为 AFL 的随机变异能产生大量结构合法的 XML 输入（约 3,500 个 queue 条目），为 SymCC 提供了结构多样的起点。PNG 格式校验严格，AFL 的有效变异极少（约 170 个 queue 条目），Hybrid 无显著提升。关键区别不在于"文本 vs 二进制"，而在于目标格式对随机变异的容错程度。

**4. AFL edge bitmap 覆盖率与 lcov 分支覆盖率为不同维度的度量**——SymCC 的输出可提升 lcov 分支覆盖率但不触发 AFL bitmap 新覆盖（连 hit count bucket 都未变化）。当前实现中 SymCC 输出未回灌至 AFL 队列，双方反馈循环断开，是需要修复的设计缺陷。

**5. Multi-master 异步同步存在延迟窗口**——导致高并行度下去重效率下降（唯一率从 np=32 的 67.7% 降至 np=190 的 18.6%），是架构层面需要优化的点。

---

## 六、待解决的问题

### 6.1 LAVA-M 目标零输出（中优先级）

base64、md5sum、uniq、who 四个 LAVA-M 二进制均包含 SymCC 插桩符号（如 base64 有 72 个 `__sym_ctor`），编译成功，但执行时不生成任何输出。可能原因包括：
- stdin/file 输入模式不匹配（LAVA-M 程序从 stdin 读取，SymCC 可能需要 `SYMCC_INPUT_FILE` 指向实际文件）
- QSYM 后端对 glibc 工具程序中特定系统调用的兼容性问题
- 运行时环境配置缺失

需手动运行单个输入并检查 SymCC 日志以定位根因。

### 6.2 统计显著性不足（高优先级）

当前每种配置仅运行 1 轮，200K 采样引入约 ±1.5% 覆盖率噪声。不同并行度间的覆盖率差异（如 PNG np=32 的 17.9% 与 np=128 的 17.0%）可能完全由采样随机性导致。需 3-5 轮独立运行取中位数，方能判定差异是否具有统计显著性。

### 6.3 AFL-only 基线缺失（高优先级）

Hybrid 测试缺少 AFL-only 对照组，无法分离 AFL 和 SymCC 各自的覆盖率贡献。需在相同条件下（相同种子、300s 运行时间）单独运行 `afl-fuzz`，用 lcov 测量 AFL queue 的覆盖率，作为 Hybrid 评估的基准线。

### 6.4 Hybrid 并行度数据不完整（中优先级）

Hybrid 测试仅覆盖 np=2, 8, 32, 128，缺少 np=4, 16, 64 数据，与纯 MPI 测试的并行度梯度不一致。需补测以确保对比的完整性。

### 6.5 测试时间过短（中优先级）

当前每轮仅 300 秒（5 分钟），学术论文中典型评测为 24 小时。5 分钟可能不足以让 concolic execution 充分探索深层路径——QSYM 后端的约束求解单次可能耗时 30+ 秒，每个 worker 在 5 分钟内仅能处理约 10 个输入。

### 6.6 吞吐量饱和原因未确认（低优先级）

np=64 以后吞吐量增速放缓的确切原因尚未定位。需对 SymCC 执行时间、MPI 消息收发时间、master 间同步时间、worker 空闲时间分别计时，方能确认瓶颈所在。

---

## 七、后续计划

| 优先级 | 任务 | 预期收益 |
|--------|------|---------|
| 高 | 补充 AFL-only 基线测试 | 建立 Hybrid 评估的正确基准 |
| 高 | 多轮统计测试（3-5 轮/配置） | 消除采样噪声，得到可靠的覆盖率对比数据 |
| 高 | 补测 Hybrid np=4, 16, 64 | 统一并行度梯度，确保对比完整性 |
| 高 | 长时间执行测试（1-24 小时） | 观察覆盖率曲线是否在更长时间尺度上继续增长 |
| 中 | 修复 SymCC→AFL 反馈回路 | 将 SymCC 输出选择性回灌至 AFL 队列 |
| 中 | LAVA-M 调试（手动运行 + 日志分析） | 增加测试目标多样性 |
| 中 | 吞吐量饱和分阶段计时 | 定位高并行度下的性能瓶颈 |
| 中 | Multi-master 同步优化 | 减少高并行度下的重复探索 |
| 低 | 编译系统改进（独立构建目录） | 消除三种构建间的相互干扰 |
| 低 | 增加更多目标（FreeType、re2、SQLite） | 验证结论的通用性 |
| 低 | 代码整理提交 & PR | 正式贡献到项目 |

---

## 八、代码变更总览

共修改 6 个文件，新增约 1,400 行代码：

| 文件 | 改动量 | 主要内容 |
|------|--------|---------|
| `.gitignore` | +2 | 忽略 .env 和构建产物 |
| `benchmark/compile_public_benchmarks.sh` | +450 | 三模式编译（SymCC/Coverage/AFL）、LAVA-M 编译、构建顺序控制 |
| `benchmark/run_benchmark.py` | +750 | lcov 覆盖率测量、public 目标发现、serial/mpi/hybrid 三模式编排、报告生成 |
| `benchmark/setup_public_benchmarks.sh` | +59 | 多级 fallback 下载、glibc 兼容性 patch |
| `util/mpi_concolic_execution.py` | +82 | 原子写入、env 浅拷贝、MPI Barrier |
| `util/mpi_fuzzing_helper.py` | +90 | hang 保存、优雅退出、worker_env 隔离、--save-all 机制 |
