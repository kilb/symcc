# 技术④ 选择性符号化 —— 移植到 SymSan(DFSan 后端)

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

## 复现
改动落在 5 个 SymSan 源文件,已存为 `scripts/symsan_patches/selective-symbolization.patch`,
`scripts/build_symsan.sh` 在构建前幂等应用(dfsan_flags.inc 已含 `focus_bytes` 则跳过)。
涉及文件:`runtime/dfsan/dfsan_flags.inc`(新 flag)、`runtime/dfsan/dfsan_custom.cpp`(门控)、
`driver/launcher/launch.c` + `include/launch.h`(config 字段 + setter + 模板)、`driver/fgtest.cpp`(解析下发)。
