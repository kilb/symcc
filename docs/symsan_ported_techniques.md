# 移植到 SymSan(DFSan 后端)的 SymCC 自研技术

本文记录把 SymCC 侧的 3 个自研优化移植到可切换的 **SymSan(DFSan)引擎**:
**④ 选择性符号化**(运行时 taint 源门控)、**③ 字典引导** 与 **① 多字段解组合**(driver 层)。
三者都经与 SymCC 同名的环境变量通道下发(`SYMCC_FOCUS_BYTES` / `SYMCC_DICT` / `SYMCC_MULTI_SOLVE`),
故编排层与 SymCC 一致、无需改动。补丁:`scripts/symsan_patches/symsan_ported_techniques.patch`
(`build_symsan.sh` 幂等应用)。

---

## 技术④ 选择性符号化

选择性符号化(selective symbolization):**只符号化输入里"相关"的字节,其余字节保持具体值**。
好处是缩小符号状态、少给求解器喂无关约束,把算力集中在真正影响分支的字节上。这是本项目在
SymCC 侧的 5 个自研优化之一(见 `runtime/src/backends/qsym/Runtime.cpp` 的 `initFocusBytes` /
`_sym_get_input_byte`),本文记录把它**移植到可切换的 SymSan(DFSan)引擎**。

## 为什么 DFSan 天生适合
SymCC 在 `_sym_get_input_byte(offset, value)` 里判断 `offset` 是否落在 focus 区间,不在则返回
`nullptr`(该字节不产生符号表达式)。SymSan 用的是 LLVM DataFlowSanitizer:**每个输入字节按文件
偏移各自持有一个 taint label**,label 传播天然是字节级的。因此"选择性"只需在**打标签的源头**按
偏移门控——不在 focus 区间的偏移直接给 label 0(具体值),其余照常。比 SymCC 更自然。

## 落点:一个统一枢纽 `get_label_for`
SymSan 运行时所有读入路径(`read/pread/fread/getdelim/mmap` 等 14 处拦截器)取输入字节 label 都经过
`runtime/dfsan/dfsan_custom.cpp` 的单一内联函数 `get_label_for(fd, offset)`。在这一处门控即覆盖全部
读入路径(altitude 正确,不必逐个拦截器改):

```cpp
static inline dfsan_label get_label_for(int fd, off_t offset) {
  if (is_stdin_taint() || (fd == 0 && flags().force_stdin)) {
    off_t off = current_stdin_offset++;      // 偏移必须始终自增以保持一致
    if (!focus_active(off)) return 0;         // 选择性符号化:非 focus → 具体
    return dfsan_create_label((uint64_t)fd, (uint64_t)off, 1);
  } else {
    if (!focus_active(offset)) return 0;      // 选择性符号化:非 focus → 具体
    return (offset + CONST_OFFSET);
  }
}
```

`focus_active(off)` 依据 `flags().focus_bytes` 解析出的区间判断;**未配置 focus 时恒 true**,默认行为
(全字节符号化)完全不变。区间语义与 SymCC 的 `SYMCC_FOCUS_BYTES="s-e"` 一致:区间内符号化、区间外
具体化。`InitializeTaintFile` 里对全体偏移的 label 预分配【保持不动】——它只预留 label id、不写 shadow,
真正的污染由 `get_label_for` 经 `dfsan_set_label` 决定;改动预分配会打乱 `offset+CONST_OFFSET` 的连续编号。

## 传参链路(编排层 → 目标进程)
focus 区间由**并行编排层**依 interesting 用例的字节偏移动态算出(`mpi_fuzzing_helper.py`,单区间
`min-max`),经既有的 `SYMCC_FOCUS_BYTES` 通道下发。SymSan 侧新增的翻译链:

```
master 算出区间
  → worker 环境 SYMCC_FOCUS_BYTES="s-e"                     (util/mpi_fuzzing_helper.py,既有)
  → SymSanEngine.wrap_run 追加 TAINT_OPTIONS "... focus_bytes=s-e"   (util/concolic_engine.py,新增)
  → fgtest 解析 focus_bytes= 暂存,symsan_init 之后调 symsan_set_focus_bytes()   (driver/fgtest.cpp)
  → launcher 把 focus_bytes= 拼进目标进程的 TAINT_OPTIONS                 (driver/launcher/launch.c)
  → 目标进程 DFSan 运行时解析成 flags().focus_bytes → get_label_for 门控   (runtime/dfsan/*)
```

两个易错点(均已解决):
1. **launcher 会重建目标 env**:`launch.c` 用固定模板重新拼 `TAINT_OPTIONS`,直接透传的 focus_bytes 会
   被丢弃 → 必须把 focus_bytes 加进 `symsan_config` 并写进模板(条件追加,空值不注入)。
2. **`symsan_init` 会重置 `g_config`**:故 fgtest 必须在 `symsan_init` **之后**再调
   `symsan_set_focus_bytes`(与 `symsan_set_debug` 等一致),否则设置被覆盖。

## 单区间限制(与 SymCC 一致,非缺陷)
DFSan 的 sanitizer flag 解析把 `,` 也当分隔符(`sanitizer_flag_parser.cpp` 的 `is_space` 含逗号),
故 `focus_bytes=0-3,8-11` 这类逗号多区间会破坏 flag 串。因此 focus 传输层只支持**单个 `s-e` 区间**——
这与 SymCC 的 `SYMCC_FOCUS_BYTES` 本身就用 `sscanf("%lu-%lu")` 只读单区间**完全一致**,且编排层也只
下发单区间。`SymSanEngine._sanitize_focus` 对逗号输入取首段并校验,非法则退回全字节符号化(不丢覆盖)。
(离散多字节集是 SymCC 的另一机制 `SYMCC_FOCUS_SET`,基于文件、不走 flag,SymSan 侧未移植。)

## 验证(focus_test:A 段=offset 0–3,B 段=offset 8–11,两段独立可达)
经 `run_symcc_worker` + `SYMCC_FOCUS_BYTES` 同一通道,两引擎表现一致地把解限制在 focus 区间:

| 引擎 | 无 focus | focus=0-3 | focus=8-11 |
|---|---|---|---|
| SymSan | 求解偏移 {0,1,2,3,8,9,10,11} | **仅 {0,1,2,3}** | **仅 {8,9,10,11}** |
| SymCC  | 求解偏移 {0,8} | **仅 {0}** | **仅 {8}** |

(SymCC 每遍每个比较只翻首个失配字节故偏移更少,但**门控行为相同**:focus 把解严格限制在指定区间。)

---

## 技术③ 字典引导(dictionary)

对被求解改动的字节位置,用**AFL 字典 token**(魔数/关键字常量)拼接生成额外候选,帮 fuzzing 越过
Z3 未必直接解出的 magic/关键字。移植自 SymCC `Solver::loadDictionary` / `saveDictVariants`。

**落点:纯 driver 层(fgtest.cpp),不碰求解器。** 与 ④ 不同,字典的求解 + 产出都在 fgtest 进程里,
故直接 `getenv("SYMCC_DICT")` 即可,无需经 launcher 透传到目标。
- `load_dictionary()`:读 `SYMCC_DICT`,解析 AFL 格式(`"token"` / `name="token"`,支持 `\xNN`)。
- `save_dict_variants(solved)`:在 `generate_input` 写出求解结果后调用——找出求解【首个】改动的偏移,
  对每个 token 从**原始种子**出发拼接产出变体(cap 20;跳过与原值/解相同者),复刻 SymCC 语义。

**验证**(focus_test,dict 含两段 magic token):`DICT=off` 8 个输出 → `DICT=on` **20 个输出**
(多出 12 个字典拼接变体)。

## 技术① 多字段解组合(multi-field solution combination)

SymCC 的 `negateGroup` 收集相邻 interesting 分支、用一次 Z3 联合求解同时翻转多个字段(如结构体头部的
magic/version/flags),一步跨过多字段校验,省去逐字段反馈迭代。

**SymSan 架构下的对应实现:在【解层面】组合而非 Z3 层面联合。** SymSan 的 rgd/Z3 求解器按 task
【逐分支】求解,重写成 QSYM 式 forest-union 联合求解是大改;而 fgtest 本就为每个分支产出 per-offset 的
SET 解。于是:
- `generate_input` 累积各分支的 SET 解到 `__combined_sets`(offset→val,后写覆盖);
- 事件循环结束时 `flush_combined_input()`:若累积到 ≥2 个不同偏移,额外产出**一个同时应用全部字段解**
  的组合输入。各偏移互斥 → 组合无冲突;结果是候选,由覆盖反馈确认(sound)。

对**同一遍即可达的多个独立字段校验**(QSYM 的"邻近字段/结构体头部"模式),该组合等价于联合求解的效果:
一个输入一次满足多字段。对短路嵌套的单路径 magic(一遍只暴露一个字节比较),则退化为无额外产出——
此时本就需反馈迭代,编排层的"输出喂回"循环负责。门控 `SYMCC_MULTI_SOLVE`(与 SymCC 同名)。

**验证**(focus_test 两段独立 magic,-O1 各融成一个 32 位比较):`MULTI_SOLVE=off` 8 个输出、无单个
输出同时满足两段;`MULTI_SOLVE=1` **9 个输出,其中 1 个组合输出同时满足 A、B 两段**——正是"一次跨过
多字段"的效果。

## 技术② hint 传递(input-to-state)

对每个求解产出,写一个 `.hints` 旁车文件,每行 `offset:old:new`(十六进制)记录相对原始种子改动的
字节。编排层(`mpi_fuzzing_helper` 读 `*.hints` → `hint_map`)据此做**精准变异 / focus_bytes 计算**——
把 concolic 求出的"哪个字节该变成什么"反馈给 AFL 侧,是 input-to-state 的桥。移植自 SymCC `saveValues`
的 `emit_hints` 段,格式逐字节一致。

**落点:纯 driver 层(fgtest.cpp)。** `write_output` 返回文件名,`emit_hints(path, out)` 对比种子写
`<id>.hints`;`generate_input`(求解输出)与 `flush_combined_input`(组合输出)各调一次。门控
`SYMCC_EMIT_HINTS`(与 SymCC 同名;`SymCCEngine` 默认置 1,SymSan 经 env 透传)。

**验证**:base64 种子上 `SYMCC_EMIT_HINTS=1` → 每个 `id-*` 旁生成 `id-*.hints`,内容如 `0:41:11`
(偏移 0,'A'→0x11),格式与编排层 `hint_map` 解析器完全吻合。

## 技术⑤ fast-solve(基本被 SymSan 原生快解吸收)

SymCC 的 ⑤ 是"简单约束/多字节 Concat 比较跳过 Z3、直接算值"(`fastSolve`/`fastSolveConcat`)。SymSan 侧
**大部分作废**:(a) SymSan 求解器把多字节比较(magic/整数)作为**单个 task 一次解出**,天然是 ⑤
`fastSolveConcat` 想达到的效果(见 base64 对拍:symsan 用 1/2 的输入达到近同覆盖);(b) SymSan 的 SOTA
后端是 **JIGSAW/FastGen 梯度求解器**(`KO_USE_FASTGEN` + rgd-solver,USENIX'22),本就是"跳过 Z3 的快
解"。当前接入用 fgtest 的进程内 Z3(简单约束下 Z3 已很快);接 JIGSAW 进程外求解器可作进一步 SOTA 化,
列为后续。故 ⑤ 不单独移植。

---

## SOTA 求解栈:RGD/JIGSAW driver(`fgtest_rgd`)

fgtest 用进程内 Z3。SymSan 的 SOTA 后端其实是 **RGD 解析器 + I2S→JIGSAW→Z3 求解级联**
(SymSan/JIGSAW USENIX'22;参考实现是 AFL++ custom-mutator `driver/aflpp/symsan.cpp`):
- **I2SSolver**——input-to-state(RedQueen 式),直接把比较操作数回写输入,cheap,专治 magic/校验和;
- **JITSolver**——JIGSAW 梯度求解,LLVM JIT 把约束编译成函数做梯度下降,解 Z3 头疼的非线性约束(可选);
- **Z3Solver**——SMT 兜底,保证不弱于 Z3 基线。

新增 driver **`driver/fgtest_rgd.cpp`**:复刻 aflpp 的事件循环(`handle_cond`/`handle_gep` →
`parse_cond`/`retrieve_task` → `FIFOTaskManager`),但保留 fgtest 的 one-shot 文件契约——运行结束抽干
任务队列、每个 task 走级联、命中即写 `id-*`。复用 ④选择性符号化 / ③字典 / ②hint;driver 侧还需自备
`__dfsan::get_label_info`(索引 shm union table)。`build_symsan.sh` 的 `make`/`make install` 自动产出。

**选用**:`SYMSAN_SOLVER=rgd`(编排层自动取 fgtest 同目录的 `fgtest_rgd`);JIGSAW 再加 `SYMSAN_USE_JIGSAW=1`。

**对拍(实测,同一反馈 campaign)**:

| 目标 | Z3 fgtest | RGD I2S+Z3 | RGD I2S+JIGSAW+Z3 |
|---|---|---|---|
| base64 | 112/192(2003 输入) | **112/192(3201 输入)** | — |
| deep_branches | 27/64(2821) | 24/64(9201) | 24/64(7461) |
| crypto_check | 22/64(1334) | 21/64(302) | 21/64(654) |

**全 hybrid 验证**(`--engine symsan --hybrid` + `SYMSAN_SOLVER=rgd SYMSAN_USE_JIGSAW=1`,base64,np=6):
run_benchmark 自动用 `fgtest_rgd`,concolic 贡献 **31 个 interesting(Z3 driver 为 21)**、覆盖同为
97/192——RGD 的高吞吐 I2S/JIGSAW 级联在完整流水线里产出更多 concolic 输入。SOTA 求解栈经
`SYMSAN_SOLVER=rgd` 在真实 benchmark 端到端可用。

**诚实结论**:RGD 栈【已集成、功能正确、鲁棒、全 hybrid 可用】(crypto_check 三者均不崩,SymCC 在此崩)。
这些微/小目标上 Z3 本就够用,故 RGD 覆盖持平(base64 112=112)、吞吐更高(I2S 快;hybrid 里 31>21
interesting),个别嵌套目标略低(默认关 nested;`SYMSAN_USE_NESTED=1` 可开)。RGD 的真正优势(JIGSAW
非线性、I2S 吞吐)需在大型真实目标上体现。两个 driver 现可自由切换,SOTA 求解栈已就位。

---

## 真实公开目标验证(LAVA-M base64)

不止微目标——用 ko-clang(FastGen)把 **LAVA-M base64**(自带 `harness_base64.c`,读文件 → 匹配 fgtest
污点契约,LAVA 注入 bug 在链接的 `lib/base64.c`)编成 `base64_harness_symsan`(`scripts/build_public_symsan.sh`;
关键:coreutils gnulib 头需 `-include config.h` 前置)。`run_benchmark` 的公开目标发现已**引擎感知**:
`--engine symsan` 时只挑 `*_symsan` 二进制、剥后缀取逻辑名 → 复用 symcc 目标名与种子目录。

**覆盖率对拍**(同一 `run_symcc_worker` 反馈式 campaign,各 45s,afl-showmap 量边):

| 引擎 | 唯一输入 | 边覆盖 | 备注 |
|---|---|---|---|
| symcc | 5235 | **113/192** | 生成更多 |
| symsan | 2121 | **111/192** | 用 40% 的输入达 98% 覆盖 |

**真·完整 hybrid**(`--engine symsan --hybrid --targets lava-base64_harness`,np=6):自动发现 →
AFL(3)+ SymSan concolic(2,经 fgtest,四技术全开)→ **边覆盖 38.02%(73)→ 50.52%(97 edges)**,
concolic 贡献 21 个 interesting。SymSan 已是能跑真实公开套件的一等引擎。

### 真实公开目标全表
LAVA-M(coreutils)之外再铺一批**格式解析器**(concolic 强项:魔数/结构/校验/嵌套)。做法统一:
用 ko-clang(FastGen)整体重编库 `.a` → 链接独立 file-reading harness(匹配 fgtest 契约)成 `*_symsan`;
发现层引擎感知自动识别。库 `.a` 均 gitignored,重编不动仓库。构建见 `scripts/build_public_symsan.sh`
(`BUILD_LIBS=1` 启用重库,base64 默认)。

| 目标 | 类别 | 构建 | dfsan符号 | concolic 实测 | 状态 |
|---|---|---|---|---|---|
| base64_harness | LAVA-M | harness+lib/base64.c | 176 | 全 hybrid 73→97 边 | ✅ |
| xml_read_fuzzer | libxml2 | 重编 libxml2.a(234) | 176 | 142 输出+hints | ✅ |
| png_read_fuzzer | libpng | 重编 libpng.a(60) | 176 | 15 输出+hints | ✅ |
| pcre2_fuzzer | pcre2 | 重编 libpcre2-8.a(54) | 176 | 全 hybrid 4022/9728 边,concolic 120 interesting | ✅ |
| sqlite_fuzzer | sqlite | amalgamation 7MB(KO_DONT_OPTIMIZE) | 176 | 全 hybrid 6019/31552 边,67 interesting | ✅ |

**sqlite(两处误判都已修正,现已跑通)**:
1. **编译**:并非"DFSan 大 TU 崩溃"。真正原因是 ko-clang 强制 `-O3` 触发自动向量化,在巨型函数
   `sqlite3VdbeExec` 上生成 `bitcast v2i64→i64`,X86 指令选择 `Cannot select` 崩溃(后端 ISel,非 DFSan
   pass——`sqlite3VdbeExec.taint` 说明 pass 已跑完)。用 `KO_DONT_OPTIMIZE=1`(跳过 -O3,fgtest 标准档)
   即编译通过(7.9MB,176 符号)。
2. **运行**:concolic 一开始产出 0,是因为 sqlite 合法的未初始化内存访问在 bounds/GEP 追踪里产生
   `kInitializingLabel`,而 fgtest 默认 `exit_on_memerror=1` → 首次即 `Die()`、只跑 1 个 cond 就退出。
   **默认改为 `exit_on_memerror=0`(见下)后跑通**:全 hybrid 边覆盖 **6019/31552(近 3 万边的大目标)**、
   concolic 贡献 **67 个 interesting**。

### 关键默认:`exit_on_memerror=0`(concolic 严格更优)
"遇内存错误即退出"是内存错误【检测】特性,对 concolic【输入生成】有害——目标合法的未初始化读会被 bounds
追踪判成错误并 `Die()`,提前终止、丢产出。两个 driver 现【默认关闭】(`SYMSAN_MEMERR_EXIT=1` 恢复;bounds
追踪保留供 GEP 求解,`SYMSAN_NO_BOUNDS` 可关)。实测**各目标严格更优**:

| 目标 | memerr-exit 开(旧默认) | 关(新默认) |
|---|---|---|
| sqlite | 0 | **51** |
| base64 | 28 | **56** |
| pcre2 | 140 | **488** |
| xml | 142 | 142(不变) |

这是一处一行默认翻转、**全面提升**且解锁 sqlite 的改动。教训:遇"0 产出"先查是不是被 `Die()` 提前打断
(debug=1 看有无 `uninitialized label` + `exit_on_memerror`),而非归因于目标本身。

**pcre2 全 hybrid 端到端**(`--engine symsan --hybrid --targets pcre2-pcre2_fuzzer`,np=6):引擎感知自动发现 →
AFL(3)+ SymSan concolic(2)→ **边覆盖 41.34%(4022/9728),concolic 贡献 120 个 interesting**,948 tc/s。
这是一个较大真实目标(近万边),端到端流水线证 SymSan 引擎在真实格式解析器上可用且有效。

**sqlite**:7MB 的 `sqlite3.c` amalgamation 会让 ko-clang 的 DFSan 插桩 pass 崩溃(clang frontend signal,
各优化档/omit-defines 均复现)——DFSan 对超大单 TU 的已知限制。需 split 源或换 harness,暂不支持。
(格式解析器目标的 AFL 二进制多为持久模式,afl-showmap 文件模式边计数不可靠 → 以"编译+concolic 跑通"
为可用性判据,不作覆盖率对拍。)

### SymSan runtime bug 修复:`taint_getc` 丢弃 label
排查 coreutils 时发现一个**真实的 SymSan 运行时 bug**:`taint_getc`(fgetc/getc/getc_unlocked 的取 label
逻辑)算出 label 后却 `return 0`——**每次 getc 读入的字符都丢污点**(还有 `label = label =` 双赋值笔误)。
故所有 **getc 逐字符读入的程序**,输入永不符号化。改为 `return label` 后:最小用例
(`fgetc`→buf→`strcmp(buf,"MAGIC")`)与 realloc 增长缓冲版**都能解出 `MAGIC`**(修复前完全无产出)。
这是一处影响面广的运行时修复(见补丁 `dfsan_custom.cpp` `taint_getc`)。

### coreutils:uniq【已攻下,92 concolic 输出】——曾误判为"不可解",实为两个 bug
整棵 coreutils autotools 树用 ko-clang 编。**uniq 现跑通:fgtest 92 输出、fgtest_rgd 305**。此前一直 0,
是**两个叠加的 bug**,深挖(逐字节 `dfsan_read_label` 插桩 + 反汇编 `__taint_trace_cond`)才厘清:

1. **`taint_getc` runtime bug**(见上):getc 每字符丢污点 → uniq 的 gnulib `readlinebuffer` 读入的行永不
   符号化。修 `return label` 后污点完整到达 `different()` 的 `memcmp`(插桩确认 `old_lbl=61 new_lbl=62`)。
2. **我自己的构建命令 env 作用域 bug**:`VAR=1 make clean; make ...` 里 `VAR` 只作用于 `make clean`,
   真正的 `make` **缺 `KO_USE_FASTGEN`** → 链接时 ko-clang 不 `--whole-archive libFastgen.a` →
   `__taint_trace_cond` 落到**空的 weak stub(`ret`)** → 所有分支不发事件 → fgtest 0 solving。反汇编对比
   base64(真函数)vs uniq(`ret` 空桩)一眼看穿。**必须 `export` 让 env 覆盖整个构建。** base64/xml/pcre2/
   sqlite 是单次 ko-clang 编译+链接,故没踩到。

**md5sum / who**:同样正确编译+链接,但 `taint_getc=0`(它们用 `fread` 读二进制,不走 getc);产出少是
**目标性质**——md5sum 无 `-c` 只做 MD5(直线运算、无输入相关分支);who 解析二进制 utmp、分支稀疏。非 bug。

**教训(第 4 次)**:又一次"不可解"实为可修 bug——而且这次一半是我自己的构建命令错。真相靠**反汇编
`__taint_trace_cond` 对比**(空桩 vs 真函数)一步锁定。uniq 已接入发现层(`lava-uniq`),
`build_public_symsan.sh build_coreutils`(`BUILD_COREUTILS=1`,已修 env 作用域)自动产出 `uniq_symsan`。

---

## 复现
三项改动共落在 5 个 SymSan 源文件,已存为 `scripts/symsan_patches/symsan_ported_techniques.patch`,
`scripts/build_symsan.sh` 在构建前幂等应用(dfsan_flags.inc 已含 `focus_bytes` 则视为已打补丁)。
- `runtime/dfsan/dfsan_flags.inc`:④ 新 `focus_bytes` flag。
- `runtime/dfsan/dfsan_custom.cpp`:④ `get_label_for` 按偏移门控 taint 源。
- `driver/launcher/launch.c` + `include/launch.h`:④ config 字段 + `symsan_set_focus_bytes` setter + 目标 env 模板。
- `driver/fgtest.cpp`:④ 解析 `focus_bytes` 下发;③ `load_dictionary`/`save_dict_variants`;
  ① `__combined_sets` 累积 + `flush_combined_input`;② `emit_hints` 写 `.hints` 旁车。

公开目标构建:`scripts/build_public_symsan.sh`(base64_harness_symsan);发现层引擎感知在
`benchmark/run_benchmark.py`(symsan 只挑 `*_symsan`、剥后缀复用种子目录)。

## 环境变量总览(均与 SymCC 同名,编排层无需区分引擎)
| 变量 | 技术 | 作用 |
|---|---|---|
| `SYMCC_FOCUS_BYTES="s-e"` | ④ | 仅符号化该区间字节,其余具体化 |
| `SYMCC_DICT=<path>` | ③ | AFL 字典;对求解改动位置拼接 token 产出变体 |
| `SYMCC_MULTI_SOLVE=1` | ① | 运行结束把多字段 SET 解组合成一个输入 |
| `SYMCC_EMIT_HINTS=1` | ② | 每个产出写 `offset:old:new` 的 `.hints` 旁车 |
| `SYMSAN_SOLVER=rgd` | SOTA | 改用 RGD/JIGSAW driver(`fgtest_rgd`,I2S→JIGSAW→Z3) |
| `SYMSAN_USE_JIGSAW=1` | SOTA | RGD driver 里启用 JIGSAW 梯度求解(非线性约束) |

## 技术点移植总表
| | 技术 | SymSan 状态 | 落点 |
|---|---|---|---|
| ④ | 选择性符号化 | ✅ | 运行时 `get_label_for` 门控 |
| ③ | 字典引导 | ✅ | fgtest driver |
| ② | hint 传递 | ✅ | fgtest driver |
| ① | 多字段解组合 | ✅ | fgtest driver |
| ⑤ | fast-solve | ◔ 基本作废 | SymSan 单 task 多字节解 + JIGSAW 本就快解 |
