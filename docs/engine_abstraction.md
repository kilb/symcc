# Concolic 引擎抽象(SymCC / SymSan 可切换)

分支 `engine-configurable-symsan` 引入一个 concolic 引擎抽象层,让并行编排层可在
**SymCC**(默认,稳定)与 **R-Fuzz/SymSan**(实验性)之间切换。

## 为什么能抽象:编排层已把引擎当黑盒
`util/mpi_fuzzing_helper.py` 的 worker 只做:**喂一个种子文件 → 运行 concolic 二进制 → 从一个输出目录
扫出编号的新测试用例**。两个引擎都能满足"跑一次 → 把解写进一个目录"的契约,因此编排层(MPI 调度、
showmap 去重、位图合并、K-Scheduler、冗余统计)**~90% 可原样复用**(见 `SymSan_迁移评估.md`)。

## 用法
```
# 默认 SymCC(不变)
python3 benchmark/run_benchmark.py --targets ... --hybrid
# 切 SymSan(实验性,需先构建 + 重编目标,见下)
SYMSAN_FGTEST=/path/to/fgtest \
python3 benchmark/run_benchmark.py --engine symsan --targets ... --hybrid
```
`--engine` → `SYMCC_ENGINE` 环境变量 → 经 `os.environ.copy()` 透传给各 MPI worker。

## 实现
- **`util/concolic_engine.py`**:`ConcolicEngine` 接口 + `SymCCEngine` / `SymSanEngine`,`get_engine()` 工厂。
  每个引擎的 `wrap_run(target_cmd, input, out_dir, env, use_stdin, timeout)` 返回 `(cmd, env, feed_stdin)`。
- **`run_symcc_worker`** 改为调用 `get_engine().wrap_run(...)` 决定实际命令 + 环境(不再硬编码 SymCC)。
  SymCC 路径**逐字节等价于旧行为**(已端到端验证:gen/interesting/edges 与重构前一致)。

| | SymCC(默认) | SymSan(实验性) |
|---|---|---|
| 二进制后缀 | `_symcc` | `_symsan` |
| 编译 wrapper | `symcc` / `sym++` | `KO_CC` / `KO_CXX`(ko-clang) |
| 插桩检测符号 | `__sym_ctor` / `_sym_build` | `dfs$` / `__dfsan_` |
| 运行模型 | 二进制自驱,`SYMCC_OUTPUT_DIR=<out> ./t_symcc <in>` | 经 driver:`TAINT_OPTIONS=output_dir=<out> fgtest ./t_symsan <in>` |
| 求解 | 进程内 QSYM/Z3 | 进程内 Z3(fgtest)/ 进程外 FastGen |

**SymSan 适配的关键**:`R-Fuzz/symsan` 的 `driver/fgtest.cpp` 是一个独立 driver——加载 DFSan 插桩目标、
跑取 label、进程内 Z3 求解(30s)、把新输入写进 `TAINT_OPTIONS` 指定的 `output_dir/id-*`。这**正好复现
SymCC 的目录契约**,故 `SymSanEngine.wrap_run` 只需生成 `fgtest <binary> <input>` 命令 + 设 `TAINT_OPTIONS`。

## 构建 SymSan(`scripts/build_symsan.sh`)
```
git clone https://github.com/R-Fuzz/symsan
SYMSAN_SRC=/path/to/symsan  Z3_ROOT=/path/to/z3-4.13.x  scripts/build_symsan.sh
```
**依赖 / 已知阻塞(本机实测的构建级联,按出现顺序)**:
1. LLVM **18**(DFSan 影子内存对版本敏感;本机 clang-18.1.3,SymSan 官方测 18.1.18,次要版本差异需实测)。
2. **Z3 ≥ 4.8.15**(string theory API)。本机系统 Z3=4.8.12 → cmake fail。**已解**:下 Z3 官方 prebuilt
   4.13.0-x64-glibc,`Z3_ROOT` 指过去 → 版本检查通过。
3. **Boost**(`boost_container`,parsers/rgd-parser 用)→ cmake fail。需 `libboost-container-dev`。
4. 之后预计还需 `protobuf-compiler libprotobuf-dev`(rgd.proto)、`libgoogle-perftools-dev`(tcmalloc)。

一次性装齐(本机未装,属系统级变更,留给使用者确认后执行):
```
apt-get install -y libboost-container-dev protobuf-compiler libprotobuf-dev \
                   libgoogle-perftools-dev libunwind-dev
```
本机进度:Z3 阻塞已绕过;停在 Boost(未擅自装系统包)。装齐上述后 `Z3_ROOT=... scripts/build_symsan.sh` 应可继续。

## 尚未完成(整体是多人周工程,见 `SymSan_迁移评估.md`)
- [ ] 用 `KO_CC` 重编各 benchmark 目标为 `*_symsan`;`build_targets`/编译脚本按引擎选 wrapper。
- [ ] 端到端验证 fgtest 契约(一个目标:seed → fgtest → 输出目录有新用例 → 编排层收得到)。
- [ ] 5 个自研技术点在 SymSan 侧重写(**这是主要工作量**):①多分支联合求解 ②hint 传递 ③字典引导
      ④选择性符号化 ⑤fast-solve——都写在 SymCC 的 qsym 表达式/solver 内部,需在 SymSan 的 DFSan-label +
      Z3/FastGen 框架里重做(其中 ⑤因 FastGen 本就 JIT 快解而部分作废)。
- [ ] C++ 目标(libFuzzer harness)进程内 Z3 有链接问题 → 需接 FastGen(进程外)。
- [ ] `run_symcc_worker` 里 showmap 去重路径与 SymSan 输出对齐(输出即普通输入文件,应可直接复用)。

**当前状态**:引擎抽象 + `--engine` 选择 + SymCc 默认路径均已完成并验证;SymSan 引擎代码已按 fgtest 契约写好,
待其构建产出 fgtest + `*_symsan` 目标后即可端到端联调。
