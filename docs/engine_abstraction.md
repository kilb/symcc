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
# 切 SymSan(实验性):编译器 + fgtest 都指向已构建的 SymSan(见 scripts/build_symsan.sh)
SYMSAN_KO_CLANG=/path/to/symsan/build/compiler/ko-clang \
SYMSAN_FGTEST=/path/to/symsan/build/driver/fgtest \
python3 benchmark/run_benchmark.py --engine symsan --targets ... --hybrid
```
- `--engine` → `SYMCC_ENGINE` → 经 `os.environ.copy()` 透传给各 MPI worker(运行期选引擎)。
- **构建期也按引擎走**:`build_targets` 用 `get_engine()` 选编译器与二进制后缀——
  symcc 用 `symcc` 编 `*_symcc`;symsan 用 `SYMSAN_KO_CLANG`(FastGen 模式)编 `*_symsan`。
  插桩检测(`_has_symcc_instrumentation`)也按 `engine.detect_symbols` 走(symcc `__sym_ctor` / symsan `__taint`)。
  (已验证:微目标 `parser.c` 两引擎均正确编译 + 检测。)

> 注:目前引擎化覆盖 `benchmark/targets/*.c` 微目标的**编译流程**;公开套件(base64/xml…)的 `*_symcc`
> 二进制是预编译的,用 ko-clang 批量重编是后续工作(其 setup 脚本 + C++ 目标的 FastGen 接入)。

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

一次性装齐(本机已装):
```
apt-get install -y libc++-18-dev libc++abi-18-dev libunwind-18-dev libboost-container-dev \
                   protobuf-compiler libprotobuf-dev libgoogle-perftools-dev libbsd-dev
```
5. `z3-ts.cpp` 用了本机 Z3 无的 `Z3_mk_string_from_code/to_code`(SMT 字符串理论,字节级目标不需要)→
   `build_symsan.sh` 自动加抛异常桩使其编译(用 CI 的 Z3 4.15.4 通常无需)。

**✅ 本机已完整构建 + 端到端跑通**(级联全解:Z3 4.13.0 prebuilt + libc++ + Boost/protobuf/perftools + z3-ts 桩):
- `make install` 生成 ko-clang 期望的 `install/lib/symsan/` 布局(passes + runtime + 各 `.a` + `taint.ld` + abilist)。
- 编译:`KO_CC=clang-18 KO_USE_FASTGEN=1 KO_DONT_OPTIMIZE=1 ko-clang -o t_symsan t.c`(**FastGen 插桩模式**是关键,fgtest 才有回调)。
- 验证:一个 4 字节 magic 守卫(`b[0..3]=="SYMS"`)的目标,种子 `"AAAAAAAA"` → `fgtest` **求解首个分支、
  把字节 0 从 'A' 翻成 'S'、输出 `id-0-0-0="SAAAAAAA"`**。逐次喂回即迭代解出全部 magic——正是 SymCC 的目录契约。

## 尚未完成(引擎已跑通,剩余为整体的多人周工程,见 `SymSan_迁移评估.md`)
- [x] SymSan 构建 + fgtest 契约端到端验证(见上,已跑通)。
- [x] 引擎抽象 + `--engine` + SymSanEngine 按 fgtest 契约实现(`TAINT_OPTIONS="taint_file=<in> output_dir=<out>"`)。
- [ ] 用 ko-clang(**FastGen 模式**)批量重编各 benchmark 目标为 `*_symsan`;`build_targets`/编译脚本按引擎选 wrapper;
      `_has_symsan_instrumentation`(检测 `__taint`/dfsan 符号)。
- 5 个自研技术点在 SymSan 侧重写(**主要工作量**):都写在 SymCC 的 qsym 表达式/solver 内部,需在 SymSan 的
  DFSan-label + Z3/FastGen 框架里重做:
  - [x] **④选择性符号化**——已移植:在 DFSan 运行时 `get_label_for` 单一枢纽按偏移门控(非 focus 字节返回
        label 0 保持具体),经既有 `SYMCC_FOCUS_BYTES` 通道 → `TAINT_OPTIONS focus_bytes=` → fgtest/launcher →
        目标 DFSan flags。两引擎 focus 行为一致(见 `docs/symsan_selective_symbolization.md`)。补丁存于
        `scripts/symsan_patches/selective-symbolization.patch`,`build_symsan.sh` 幂等应用。
  - [ ] ①多分支联合求解 ②hint 传递 ③字典引导 ⑤fast-solve(⑤因 FastGen 本就 JIT 快解而部分作废)。
- [ ] C++ 目标(libFuzzer harness):SymSan 的进程内 Z3 对 C++ 目标有链接问题 → 需接 FastGen(进程外)。
- [ ] fgtest 单遍只解一个嵌套分支——编排层的"输出喂回"循环(现成)会迭代解深;确认与 showmap 去重路径对齐
      (SymSan 输出即普通输入文件,应可直接复用)。

## 引擎对拍(`scripts/engine_dogfight.py`,实测)
把 `benchmark/targets/parser.c`(嵌套 4 字节魔数 `"SYM\x01"`)分别用 `build/symcc` 与 ko-clang(FastGen)编成
`parser_symcc` / `parser_symsan`,经**同一 `run_symcc_worker` + `get_engine().wrap_run` 路径**跑反馈式 concolic:

| 引擎 | 结果 |
|---|---|
| symcc | ✓ 第 4 轮解出完整魔数 `SYM\x01`,累计 11 个唯一输入 |
| symsan | ✓ 第 4 轮解出完整魔数 `SYM\x01`,累计 11 个唯一输入 |

→ 两引擎经 `--engine` 切换、走同一并行编排路径,反馈循环逐层解开嵌套魔数,**结果完全等价**。

## 引擎覆盖率对拍(`scripts/engine_coverage_dogfight.py`,实测)
同目标、同 45s 预算跑反馈式 concolic campaign,afl-showmap 量边覆盖:

| 目标 | 引擎 | 唯一输入 | 边覆盖 | 备注 |
|---|---|---|---|---|
| deep_branches | symcc | 15,886 | **29/64** | 生成更多、覆盖略高 |
| deep_branches | symsan | 3,236 | **26/64** | 输入少但覆盖接近(90%) |
| crypto_check | symcc | — | **崩溃** | QSYM `expr.h:439 l->bits()==r->bits()`(uint32 casts + 混合位宽比较) |
| crypto_check | symsan | 1,464 | **21/64** | 正常——DFSan 路径更鲁棒 |

**观察(诚实)**:两引擎【可比】但各有短长——SymCC 生成更快/更多、能跑的目标上覆盖略高;**SymSan 更鲁棒**
(crypto_check 上 SymCC 的 QSYM 表达式构造器断言崩溃,SymSan 正常)。这正是"可配置引擎"的价值:按目标选引擎。

## 全 hybrid 对拍(CLI 端到端,实测)
`run_benchmark --engine {symcc,symsan} --hybrid`(deep_branches 微目标,np=6,10s):CLI 自动用 ko-clang 编 `*_symsan`
+ afl-clang-fast 编 `*_afl`,跑 **AFL 并行实例 + MPI concolic workers(经 fgtest,引擎化)+ afl-showmap 覆盖测量**:

| 引擎 | AFL 用例 | concolic interesting | 边覆盖 | bitmap |
|---|---|---|---|---|
| symcc | 240 | 39 | **30/38** | 78.95% |
| symsan | 242 | 25 | **30/38** | 78.95% |

→ **完整 hybrid 流水线两引擎均跑通、覆盖一致**(SymCC concolic 贡献 39 个 interesting,SymSan 25 个)。
为此 `mpi_concolic_execution.py` 也引擎化了,且 `build_targets`/hybrid 现支持微目标(自动编 `*_afl`)。

**当前状态**:引擎抽象 + `--engine` + SymCC 默认(逐字节等价)+ **SymSan 构建 & fgtest & 真实目标对拍均已跑通验证**;
剩下的是"用 ko-clang 批量重编全部 benchmark 目标"和"5 个技术点在 DFSan 侧重写"这两块真正的工作量。
