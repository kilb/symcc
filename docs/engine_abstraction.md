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

> 注:引擎化已覆盖 `benchmark/targets/*.c` 微目标和 base64、libxml2、libpng、PCRE2、
> SQLite、coreutils/uniq 等公开目标的编译与发现。C++ libFuzzer harness 的外部
> FastGen 链接仍是独立的未完成边界。

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
SymCC 的目录契约**。无额外目标参数时，`SymSanEngine.wrap_run` 生成
`fgtest <binary> <input>`；有参数时使用
`fgtest <binary> <taint-input> -- <target-argv[1:]>`。编排层逐参数替换 `@@`，
两个 driver 再把 `--` 后的参数原样交给目标。该协议不经过 shell，空参数、空格和标点不会被重解析。
补丁位于 `scripts/symsan_patches/symsan_target_argv.patch`，构建脚本应用失败时终止，
避免目标实际少参数而实验仍被记录为成功。

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

## 当前状态与剩余边界
- [x] SymSan 构建 + fgtest 契约端到端验证(见上,已跑通)。
- [x] 引擎抽象 + `--engine` + SymSanEngine 按 fgtest 契约实现(`TAINT_OPTIONS="taint_file=<in> output_dir=<out>"`)。
- [x] 用 ko-clang(**FastGen 模式**)编译微目标和多类公开 benchmark 为 `*_symsan`;
      `build_targets`/公开目标脚本按引擎选 wrapper,发现层按后缀和
      `_has_symsan_instrumentation` 过滤。
- 5 个自研技术点在 SymSan 侧完成四项直接移植和一项等价性分析(见
  `docs/symsan_ported_techniques.md`,
  补丁 `scripts/symsan_patches/symsan_ported_techniques.patch`,`build_symsan.sh` 幂等应用):
  - [x] **④选择性符号化**——DFSan 运行时 `get_label_for` 单一枢纽按偏移门控(非 focus 字节返回 label 0),
        经 `SYMCC_FOCUS_BYTES` → `TAINT_OPTIONS focus_bytes=` → fgtest/launcher → 目标 DFSan flags。
  - [x] **③字典引导**——fgtest driver 层:`SYMCC_DICT` 载入 AFL 字典,对求解改动位置拼接 token 产出变体
        (cap 20)。复刻 `saveDictVariants`。
  - [x] **①多字段解组合**——fgtest driver 层:`SYMCC_MULTI_SOLVE` 累积各分支 SET 解,结束时组合成一个
        "同时满足多字段"的输入(SymSan 按 task 逐分支解,故在解层面组合,等价于 `negateGroup` 对邻近字段的效果)。
  - [x] **②hint 传递**——fgtest driver 层:`SYMCC_EMIT_HINTS` 给每个产出写 `offset:old:new` 的 `.hints`
        旁车,编排层 `hint_map` 复用。复刻 SymCC `emit_hints`。
  - [~] ⑤fast-solve——基本作废:SymSan 多字节比较单 task 一次解出、JIGSAW/FastGen 本就跳 Z3 快解。
- **真实公开目标**:LAVA-M base64 已用 ko-clang 编成 `base64_harness_symsan`(`scripts/build_public_symsan.sh`),
  `run_benchmark` 公开目标发现【引擎感知】(symsan 只挑 `*_symsan`)。全 hybrid 实测:边覆盖 73→97,
  concolic 贡献 21 interesting;覆盖率对拍 symcc 113/192 vs symsan 111/192。**已铺 5 个真实目标**:
  base64、libxml2 xml、libpng png、pcre2、**sqlite**(全 hybrid 6019/31552 边,67 interesting)——均
  ko-clang 编 + 发现层引擎感知。sqlite 曾两度误判(-O3 向量化 ISel 崩溃→KO_DONT_OPTIMIZE 解;运行期
  exit_on_memerror Die→默认关 memerr-exit 解,后者还令 base64 28→56、pcre2 140→488 严格更优)。
  coreutils md5sum/uniq/who 能编译但 strcmp 主导、concolic 0 产出(不接入)。
- **SOTA 求解栈**:新增 `driver/fgtest_rgd.cpp`——RGD 解析器 + I2S(input-to-state)→JIGSAW(梯度)→Z3
  级联(SymSan/JIGSAW USENIX'22),保留 fgtest one-shot 契约。`SYMSAN_SOLVER=rgd` 切换、`SYMSAN_USE_JIGSAW=1`
  开梯度。实测已集成+功能正确+鲁棒,微目标上与 Z3 持平(base64 112=112,吞吐更高);优势需大目标体现。
- [ ] C++ 目标(libFuzzer harness):SymSan 的进程内 Z3 对 C++ 目标有链接问题 → 需接 FastGen(进程外)。
- [x] fgtest 单遍只解一个嵌套分支时,编排层通过"输出喂回"循环迭代解深,并与
      showmap 去重路径对齐;parser dogfight 和 full hybrid 已验证。
- [ ] 离散 `SYMCC_FOCUS_SET` 尚未迁移;当前 SymSan 选择性符号化只支持单个连续
      `focus_bytes=s-e` 区间。
- [ ] F00-F16 的统一 telemetry、data coverage 与新 scheduler 需要逐项定义
      SymSan capability/conformance,不能只依赖 SymCC 默认路径。

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

**当前状态**:引擎抽象、引擎感知构建/发现、SymCC 默认兼容、SymSan fgtest、
四项技术迁移、RGD/I2S/JIGSAW/Z3 和多类真实目标 full-hybrid 均已跑通。剩余工作
集中在 C++ harness 的外部 FastGen 链接、离散 focus set、F00-F16 capability 对齐
以及等 CPU 多轮统计验证,不再是早期文档所述的"公开目标尚未重编/技术点尚未移植"。
