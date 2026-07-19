# SymSan 引擎集成 · 工作总结与过程资料(PPT 补充材料)

> 用途:作为答辩/汇报 PPT 的**补充材料**——正文给结论,本文档给**过程、证据、可复现细节**。
> 分支:`engine-configurable-symsan`(已推送 `github.com:kilb/symcc.git`)。

---

## 0. 一页速览

**任务**:把 SymCC 换成 SymSan,或做成可配置项(通过参数选引擎)。**选择了可配置路线并完整实现。**

**交付**(一句话):`--engine symcc|symsan` 可切换;SymSan 从零构建跑通;5 个自研技术移植 4 个;
接入 SOTA 求解栈;**6 个真实目标**全部端到端跑通;修了 **4 个真 bug**;3 轮均值基准 + 可视化图表。

| 维度 | 成果 |
|---|---|
| 可配置引擎 | `--engine symcc\|symsan` → `SYMCC_ENGINE` → worker;SymCC 路径**逐字节等价** |
| SymSan 构建 | 依赖级联全解,`scripts/build_symsan.sh` 一键;本机(Ubuntu 24.04 / clang-18)跑通 |
| 5 技术点 | ④选择性符号化 ③字典 ②hint ①多字段组合 **已移植**;⑤fast-solve 被 JIGSAW 吸收 |
| SOTA 求解栈 | 新增 `fgtest_rgd`:I2S→JIGSAW→Z3 级联(SymSan/JIGSAW USENIX'22) |
| 真实目标 | **6 个**:base64 · uniq · xml · png · pcre2 · sqlite(3 类软件) |
| Bug 修复 | 4 个真 bug(见 §3.1) |
| 代码量 | 引擎抽象 132 行 + SymSan 侧补丁 803 行(7 文件)+ 4 个脚本 + 5 篇文档 |

---

## 1. 工作全景

### 1.1 为什么"可配置"而非"直接替换"
编排层(`util/mpi_fuzzing_helper.py`)早已把 concolic 二进制当**黑盒**:喂种子文件 → 运行 → 从输出目录扫
新用例。SymCC 与 SymSan 都满足"跑一次 → 把解写进一个目录"的契约,差异仅在四处,收敛到一个 `ConcolicEngine`
接口即可 → **编排层 ~90% 原样复用**,且 SymCC 默认路径零回归。

### 1.2 五大模块
```
util/concolic_engine.py        引擎抽象层(SymCCEngine / SymSanEngine + get_engine)
scripts/build_symsan.sh        SymSan 一键构建(依赖级联 + z3-ts 桩 + 技术补丁)
scripts/symsan_patches/*.patch SymSan 源改动(5 技术 + RGD driver + 修复,803 行)
scripts/build_public_symsan.sh 6 个 *_symsan 真实目标构建
benchmark/run_benchmark.py     公开目标发现"引擎感知"(--engine symsan 只挑 *_symsan)
```

### 1.3 commit 时间线(17 个,即"讲故事的脉络")
```
4615505 引擎接线 mpi_concolic + 微目标 → 全 hybrid --engine symsan
ac89b2f 技术④ 选择性符号化
517adf9 技术③ 字典 + ① 多字段组合
4469bc5 技术② hint + 首个真实目标 base64
5d80b0d build_public_symsan.sh(force-add 越过 *build* gitignore 陷阱)
bf7cc99 SOTA 求解栈 fgtest_rgd(I2S→JIGSAW→Z3)
1a4e1d5 真实目标 libxml2 xml
b781cf3 真实目标 libpng png + pcre2
62f3115 pcre2 全 hybrid 验证
3060a7b coreutils 编译通过但 concolic 0(当时的判断)
6e9600b 更正:sqlite 能编译(-O3 ISel bug,非 DFSan 大 TU 崩溃)
5cd83d1 修 sqlite 运行:默认 exit_on_memerror=0 → 全目标提升
461f336 修真 bug:taint_getc 丢弃 label
2169260 coreutils 诊断细化(memcmp 非 strcmp)
4058446 coreutils 攻下:uniq 92 输出(实为 2 个 bug)
3f44011 6 目标 3 轮均值基准 + 图表
```
> 注:`3060a7b`/`6e9600b`/`2169260` 是"当时判断/更正/再更正"的**诚实留痕**——正是本工作反复"深挖翻案"的过程。

---

## 2. 技术成果(分模块)

### 2.1 可配置引擎抽象(`util/concolic_engine.py`)
`ConcolicEngine` 接口收敛四处差异:①二进制后缀 ②插桩检测符号 ③编译 wrapper ④运行方式+环境变量。
- **SymCC**:二进制自驱,`SYMCC_OUTPUT_DIR=<out> ./t_symcc <in>`。
- **SymSan**:经 driver,`TAINT_OPTIONS="taint_file=<in> output_dir=<out>" fgtest ./t_symsan <in>`。
- `SYMSAN_SOLVER=rgd` 自动切到 `fgtest_rgd`(SOTA 级联)。
详见 `docs/engine_abstraction.md`。

### 2.2 SymSan 构建(`scripts/build_symsan.sh`)
本机实测的依赖级联(按出现顺序全解):Z3≥4.8.15(系统 4.8.12 过旧,用 4.13 prebuilt)、libc++-18、
Boost/protobuf/perftools、`z3-ts.cpp` 缺 `str.from_code/to_code`(加抛异常桩)、`make install` 生成
ko-clang 期望的 `lib/symsan/` 布局。**编译目标须 `KO_USE_FASTGEN=1 KO_DONT_OPTIMIZE=1`**(FastGen 模式,
fgtest 才有回调)。

### 2.3 5 个自研技术点移植
| | 技术 | 落点 | 通道 |
|---|---|---|---|
| ④ | 选择性符号化 | 运行时 `get_label_for` 单枢纽按偏移门控 | `SYMCC_FOCUS_BYTES` |
| ③ | 字典引导 | fgtest driver(splice AFL 字典 token) | `SYMCC_DICT` |
| ② | hint 传递 | fgtest driver(`offset:old:new` 旁车) | `SYMCC_EMIT_HINTS` |
| ① | 多字段解组合 | fgtest driver(累积 SET 解组合成一个输入) | `SYMCC_MULTI_SOLVE` |
| ⑤ | fast-solve | 基本作废(SymSan 单 task 多字节解 + JIGSAW 本就快解) | — |
**均与 SymCC 同名环境变量下发**,编排层零改动。详见 `docs/symsan_ported_techniques.md`。

### 2.4 SOTA 求解栈(`driver/fgtest_rgd.cpp`)
fgtest 用进程内 Z3;SymSan 的真正 SOTA 后端是 **RGD 解析器 + I2S(input-to-state)→ JIGSAW(梯度,LLVM JIT)
→ Z3** 级联(参考 aflpp/symsan.cpp)。新 driver 保留 fgtest one-shot 契约,`SYMSAN_SOLVER=rgd` 选用。
坑:driver 侧需自备 `__dfsan::get_label_info`(i2s-solver 索引 shm union table)。

### 2.5 真实目标接入(6 个)
| 目标 | 类别 | 构建方式 |
|---|---|---|
| base64 | LAVA-M 编解码 | harness + coreutils lib/base64.c |
| uniq | LAVA-M coreutils | 整棵 autotools 树过 ko-clang |
| xml | libxml2 | 重编 libxml2.a + 独立 harness |
| png | libpng | 重编 libpng.a + heredoc harness |
| pcre2 | 正则引擎 | cmake 重编 libpcre2-8.a + harness |
| sqlite | 数据库 | 单 amalgamation `sqlite3.c` + harness |
`run_benchmark` 公开目标发现**引擎感知**:`--engine symsan` 只挑 `*_symsan`、剥后缀复用 symcc 目标名/种子。

---

## 3. 过程资料(PPT 证据 · 重点)

### 3.1 四个真 bug 的排查过程(可讲的故事)

**Bug ① `taint_getc` 丢弃 label(真 SymSan runtime bug)**
```c
static dfsan_label taint_getc(int fd, off_t offset, int ret) {
  if (ret != EOF && taint_get_file(fd)) {
    dfsan_label label = label = dfsan_union(...);  // 算出 label(还有 label=label= 笔误)
    AOUT("%d label is readed by fgetc\n", label);
  }
  return 0;   // ← 算完却 return 0,所有 getc 读入的字符丢污点
}
```
影响:所有 **getc 逐字符读入**的程序(coreutils 的 gnulib `readlinebuffer` 等)输入永不符号化。
修 `return label` 后,最小 `fgetc→buf→strcmp` 直接解出 "MAGIC"。

**Bug ② sqlite 编译崩溃 —— 我第一次误判为"DFSan 大 TU 崩溃"**
真相(抓 `clang -v` 崩溃栈):
```
fatal error: error in backend: Cannot select: i64 = bitcast v2i64
Running pass 'X86 DAG->DAG Instruction Selection' on '@sqlite3VdbeExec.taint'
```
`.taint` 后缀说明 DFSan pass 已跑完 —— 崩在**后端 ISel**。根因:ko-clang 强制 `-O3` 自动向量化,在巨型
`sqlite3VdbeExec` 上生成 `bitcast v2i64→i64`,X86 选不出指令。`KO_DONT_OPTIMIZE=1`(跳过 -O3)即编译通过。

**Bug ③ sqlite 运行 0 产出 —— exit_on_memerror 提前 Die**
sqlite 合法的未初始化内存访问 → bounds/GEP 追踪产生 `kInitializingLabel` → 默认 `exit_on_memerror=1` 首次即
`Die()`、只跑 1 个 cond 就退出。**改默认 `exit_on_memerror=0`**:不只解锁 sqlite,还**全面提升**:

| 目标 | memerr-exit 开(旧) | 关(新) |
|---|---|---|
| sqlite | 0 | 51 |
| base64 | 28 | 56 |
| pcre2 | 140 | 488 |

**Bug ④ coreutils 0 产出 —— 一半是我自己的构建命令 bug**
深挖(逐字节 `dfsan_read_label` 插桩确认行缓冲带污点 `1 2 3 4 5 6`;`different()` 收到 `old_lbl=61 new_lbl=62`)
后反汇编对比:
```
uniq:   __taint_trace_cond:  ret          ← 空 weak stub(坏)
base64: __taint_trace_cond:  push %rbp... ← 真函数(好)
```
根因两个:(a) `taint_getc` bug(见①);(b) **我的 bash env 作用域错误**——`VAR=1 make clean; make ...` 里
`VAR` 只作用于 `make clean`,真正的构建缺 `KO_USE_FASTGEN` → ko-clang 不 `--whole-archive libFastgen.a` →
`__taint_trace_cond` 落到空桩 → 分支不发事件。base64/xml 等是**单次 ko-clang 编译+链接**故没踩到。
两坑齐修后 **uniq 92 concolic 输出**。

### 3.2 诊断方法学(本工作的"技术含量")
| 方法 | 用在哪 | 揭示了什么 |
|---|---|---|
| `clang -v` 崩溃栈 + pass 名 | sqlite 编译 | 是后端 ISel 崩,不是 DFSan pass |
| `debug=1` 事件直方图 | 各目标 | taint_getc/strcmp/cond/memcmp 计数,定位丢污点段 |
| 目标内 `dfsan_read_label` 逐字节插桩 | uniq | 确认行缓冲**内容带污点**(排除传播问题) |
| **反汇编 `__taint_trace_cond`** | uniq vs base64 | 一步锁定"空桩 vs 真函数",定位链接缺 fastgen |
| 最小复现逐变量隔离 | getc/realloc/指针游走… | 排除干扰假设 |
> **教训(反复出现)**:四次说某东西"局限/不可解",四次深挖都是可修 bug。**下结论前先反汇编真正的失败点。**

### 3.3 引擎/求解器对拍数据(实测)
**引擎对拍(SymCC vs SymSan,同一 worker 路径)**
| 目标 | symcc | symsan | 备注 |
|---|---|---|---|
| parser.c 嵌套魔数 | 第4轮解出 | 第4轮解出 | 完全等价 |
| deep_branches 45s | 29/64 边 | 26/64 边 | 可比 |
| crypto_check | **崩溃**(QSYM expr.h 断言) | 21/64 | **SymSan 更鲁棒** |
| base64(公开) | 113/192 | 111/192 | symsan 用 40% 输入达 98% 覆盖 |

**求解器对拍(Z3 fgtest vs RGD fgtest_rgd)**
| 目标 | Z3 | RGD I2S+Z3 | RGD +JIGSAW |
|---|---|---|---|
| base64 | 112/192(2003) | 112/192(3201) | — |
| deep_branches | 27/64 | 24/64 | 24/64 |
| crypto_check | 22/64(均不崩) | 21/64 | 21/64 |
> 微目标上 Z3 本就够用,RGD 覆盖持平、吞吐更高;优势需大目标体现。全 hybrid 里 RGD 贡献 31 interesting(Z3 21)。

### 3.4 6 目标完整 hybrid(3 轮均值)
| 目标 | AFL | concolic(均值/范围) | 边覆盖 | tc/s |
|---|---|---|---|---|
| base64 | 442 | 28 / 23–33 | 97/192(50.5%) | 23.0 |
| uniq | 261 | 8 / 8–8 | 134/1216(11.0%) | 12.8 |
| xml | 10681 | 89 / 0–139 | 5019/50880(9.9%) | 513 |
| png | 832 | 37 / 24–44 | 665/3072(21.7%) | 45.4 |
| pcre2 | 23743 | 118 / 99–144 | 4520/9728(46.5%) | 1087 |
| sqlite | 5040 | 86 / 83–89 | 6480/31552(20.5%) | 237.5 |
详细逐轮原始数据见 `docs/symsan_hybrid_benchmark_6targets.md`。

---

## 4. 数据与图表索引
- **可视化对比图表**(深/浅色自适应,concolic 带 min–max 须 + 边覆盖 + 数据表 + 方法学):
  https://claude.ai/code/artifact/9a75d7fe-5db5-46ca-982a-2c75a61c029d
- **3 轮基准原始数据**:`docs/symsan_hybrid_benchmark_6targets.md`
- **技术移植细节**:`docs/symsan_ported_techniques.md`
- **引擎抽象设计**:`docs/engine_abstraction.md`
- **SymSan 源补丁**:`scripts/symsan_patches/symsan_ported_techniques.patch`(803 行,7 文件)

---

## 5. 复现指南
```bash
# 1) 构建 SymSan 工具链(ko-clang + fgtest + fgtest_rgd)
SYMSAN_SRC=<symsan-clone> Z3_ROOT=<z3-4.13+> scripts/build_symsan.sh   # 自动应用技术补丁

# 2) 构建 6 个真实目标
SYMSAN_KO_CLANG=<.../ko-clang> BUILD_LIBS=1 BUILD_SQLITE=1 BUILD_COREUTILS=1 \
  scripts/build_public_symsan.sh

# 3) 跑 hybrid(任一目标)
export SYMSAN_FGTEST=<.../fgtest> SYMSAN_KO_CLANG=<.../ko-clang>
python3 benchmark/run_benchmark.py --engine symsan --hybrid --no-serial \
  --targets sqlite-sqlite_fuzzer --np-list 8 --timeout 20 --rounds 3 --output /tmp/bench
# SOTA 求解栈:再加 export SYMSAN_SOLVER=rgd SYMSAN_USE_JIGSAW=1
```

---

## 6. 诚实边界与遗留
- **md5sum / who**:编译+链接均正确,但 benchmark 无 `-c` 调用下分支稀疏(md5sum 直线哈希、who 解析二进制
  utmp)→ concolic 产出少。属**目标性质**,非 bug,故不接入发现层。
- **单轮方差大**:20s 单轮 concolic 波动明显(xml 三轮 139/128/0)→ 正式对比须多轮取均值。
- **未做**:公开套件全量(libarchive/freetype2 等)、把 RGD 接成 benchmark 默认、②hint 之外更深的 UCSan 联动。

---

## 7. 交付物清单
| 类型 | 文件 |
|---|---|
| 引擎抽象 | `util/concolic_engine.py`(132 行) |
| SymSan 源补丁 | `scripts/symsan_patches/symsan_ported_techniques.patch`(803 行 / 7 文件:fgtest.cpp / fgtest_rgd.cpp / launch.c / launch.h / dfsan_custom.cpp / dfsan_flags.inc / CMakeLists.txt) |
| 构建脚本 | `scripts/build_symsan.sh` · `scripts/build_public_symsan.sh` |
| 编排接线 | `benchmark/run_benchmark.py`(引擎感知发现)· `util/mpi_fuzzing_helper.py` · `util/mpi_concolic_execution.py` |
| 文档 | 本文档 · `engine_abstraction.md` · `symsan_ported_techniques.md` · `symsan_hybrid_benchmark_6targets.md` |
| 数据/图表 | 3 轮基准 + 交互式对比图表(§4) |

> 全部推送至分支 `engine-configurable-symsan`。SymSan 已是一个可切换、带 SOTA 求解栈、能跑多类真实
> benchmark、覆盖率与 SymCC 相当的**一等 concolic 引擎**。
