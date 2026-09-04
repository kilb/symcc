# 实验证据截图

这 7 张是**一手运行/编译产物的截图**，与 `docs/codex/diagrams/qa3/` 的示意图分工不同：
示意图解释**机制**，这些截图给出**当场可见的原始证据**。

来源：`down.kew.ac/symcc-parallel-research-latest/images/experimental-evidence/`
（同一仓库的另一次打包），本文合并进来并在 `Architecture_QA3.md` 的对应小节引用。

| 文件 | 内容 | 在文档里证明什么 |
|---|---|---|
| `img_d2_pcguard.png` | `clang -fsanitize-coverage=trace-pc-guard` + `objdump -dr` | §1.4：每基本块一个 guard、`__sancov_guards` 段 44 B = 11 个 4B guard、**无 hash 碰撞**；`afl-showmap -C` 报 `map size 16` |
| `img_showmap.png` | `afl-showmap -C -i <seeddir>` on pcre2 | §1.4：**第四种 showmap 调用**（`-C`，ShowmapCov 的来源）；`map size 9697` ≠ 65536；`Persistent mode binary detected` |
| `img_ir.png` | `build/symcc -S -emit-llvm` 后的 LLVM IR | §2.1 / §4.1：`_sym_push_path_constraint(ptr %29, **i1 %30**, …)` 把**具体 i1** 一起传下去；`icmp`/`br` 原封不动 |
| `img_gdb.png` | `gdb -ex 'b _sym_push_path_constraint'` | §4.1：运行时可见 `taken=0`、`site_id=…`；断在 `Runtime.cpp:267 / :320` |
| `img_fuzzer_stats.png` | pcre2 的 `fuzzer_stats` 全量 | §1.10：**`sync_time: 0`、`corpus_imported: 0`**（比 `.synced/` 为空更直接）；§6.6：`target_mode: persistent shmem_testcase deferred`；`total_edges: 9706` |
| `img_process.png` | `run_benchmark.py --hybrid --hybrid-adaptive` 实跑 | §1.10：**60 s** 的 hybrid 轮次报 `+12.04pp / +104.0% relative`——按 §1.10 的实测，这个预算内 concolic 产出根本没进 AFL，该数字只能按并集口径读 |
| `img_report.png` | `benchmark_results_scaling_hybrid/benchmark_report.txt` | §6.7 / 图 6-3 整张表的**一手来源**（afl-only 7.55 %、hybrid 8.84 %、mpi 6.26 %、serial 3.09 % 等） |

⚠ 这些截图记录的是**各自当时的一次运行**，与文档正文的 【实测】 数据来自不同批次，
不能相互替代，也不能合并成同一组统计。它们的价值在于**佐证机制**，不在于提供可比数值。

## Codex 新功能证据目录

F433 求解中实时 checked QF_BV proof stream 的真实 CaDiCaL 双 oracle、
双机制基准、测试身份和源码哈希位于
[`f433-realtime-proof-stream-2026-08-18/`](f433-realtime-proof-stream-2026-08-18/README.md)。

F434 有资格门禁的多 rank 实时 proof-stream 评测包含 5-rank 真实 CaDiCaL
preloaded/active jobs、root 独立 ACK replay、过短 solve 失败关闭和完整时钟结论边界，位于
[`f434-multirank-proof-evaluation-2026-08-18/`](f434-multirank-proof-evaluation-2026-08-18/README.md)。
