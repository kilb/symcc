# SymSan 引擎 · 6 目标完整 hybrid 基准(3 轮均值)

`--engine symsan` 的 hybrid 流水线(AFL++ 并行 + MPI concolic worker,SymSan/DFSan 驱动)在 **6 个真实程序**
上端到端跑通的基准数据。每个值为 **3 轮独立 20s** 的均值。可视化对比图见文末链接。

## 运行配置
| 项 | 值 |
|---|---|
| 引擎 | symsan(DFSan + fgtest driver) |
| 模式 | hybrid,`--np-list 8`(3 AFL 实例 + 2 concolic worker) |
| 预算 | 每目标 20s × 3 轮 |
| 覆盖测量 | `afl-showmap -C`(边覆盖) |
| driver 默认 | `exit_on_memerror=0`(concolic 需要);④③②① 技术全开 |

## 3 轮均值结果

| 目标 | 类别 | AFL 用例 | concolic interesting(均值) | 范围 | 边覆盖 | 边% | tc/s |
|---|---|---:|---:|---:|---:|---:|---:|
| base64 | LAVA-M 编解码 | 442 | **28** | 23–33 | 97/192 | 50.5% | 23.0 |
| uniq | LAVA-M coreutils | 261 | **8** | 8–8 | 134/1216 | 11.0% | 12.8 |
| xml | libxml2 解析器 | 10681 | **89** | 0–139 | 5019/50880 | 9.9% | 513 |
| png | libpng 解析器 | 832 | **37** | 24–44 | 665/3072 | 21.7% | 45.4 |
| pcre2 | 正则引擎 | 23743 | **118** | 99–144 | 4520/9728 | 46.5% | 1087 |
| sqlite | 数据库引擎 | 5040 | **86** | 83–89 | 6480/31552 | 20.5% | 237.5 |

> 边覆盖为 3 轮取整均值;concolic/AFL/tc/s 为 3 轮均值。

## 关键结论
1. **看 concolic 列,不是 edge%**。hybrid 里 AFL 每轮产出上千用例、主导原始边覆盖;SymSan 的价值是它解出并
   喂给 AFL 的 **interesting 输入**(magic 字节 / 校验和 / 结构键——AFL 猜不出的"钥匙")。
2. **大目标 concolic 贡献更多**:pcre2(118)、xml(89)、sqlite(86)——结构越丰富,solver 越有的解。
3. **单轮方差真实**。xml 三轮是 139 / 128 / **0**(有一轮 concolic worker 20s 内超时未贡献);均值抹平、
   范围列显示区间。这正是要跑 3 轮的原因。
4. **uniq 是攻下的**。它需要一个真实的 SymSan runtime 修复(`taint_getc` 每次 `getc` 丢污点)+ 一个构建
   flag 修复,gnulib 的行比较分支才变得可解。稳定的 concolic=8(三轮一致)说明它真在跑,非噪声。
5. **三类真实软件**:编解码(base64)、coreutils(uniq)、格式/引擎解析器(xml/png/pcre2/sqlite)——全部
   经同一 `--engine symsan` 接口 + 引擎感知目标发现跑通。

## 原始逐轮数据(可复现)

| round | target | afl | concolic | edge% | edges | edges_total | tc/s |
|---:|---|---:|---:|---:|---:|---:|---:|
| 1 | base64 | 441 | 33 | 50.52 | 97 | 192 | 23.1 |
| 1 | uniq | 245 | 8 | 11.02 | 134 | 1216 | 11.9 |
| 1 | xml | 10636 | 139 | 9.90 | 5037 | 50880 | 510.4 |
| 1 | png | 839 | 42 | 21.71 | 667 | 3072 | 45.6 |
| 1 | pcre2 | 23525 | 112 | 46.21 | 4495 | 9728 | 1066.3 |
| 1 | sqlite | 5521 | 86 | 20.87 | 6584 | 31552 | 259.4 |
| 2 | base64 | 440 | 23 | 50.52 | 97 | 192 | 22.6 |
| 2 | uniq | 271 | 8 | 11.02 | 134 | 1216 | 13.3 |
| 2 | xml | 10783 | 0 | 9.86 | 5015 | 50880 | 519.2 |
| 2 | png | 842 | 44 | 21.42 | 658 | 3072 | 46.2 |
| 2 | pcre2 | 23935 | 99 | 46.62 | 4535 | 9728 | 1101.6 |
| 2 | sqlite | 4692 | 89 | 20.50 | 6467 | 31552 | 223.1 |
| 3 | base64 | 446 | 28 | 50.52 | 97 | 192 | 23.2 |
| 3 | uniq | 266 | 8 | 11.02 | 134 | 1216 | 13.1 |
| 3 | xml | 10623 | 128 | 9.84 | 5006 | 50880 | 509.9 |
| 3 | png | 814 | 24 | 21.84 | 671 | 3072 | 44.3 |
| 3 | pcre2 | 23770 | 144 | 46.56 | 4529 | 9728 | 1093.5 |
| 3 | sqlite | 4907 | 83 | 20.25 | 6388 | 31552 | 230.0 |

## 复现
```
# 前置:构建 SymSan(scripts/build_symsan.sh)+ 6 个 *_symsan 目标(scripts/build_public_symsan.sh,
#       base64 默认;BUILD_LIBS=1 编 xml/png/pcre2;BUILD_SQLITE=1 编 sqlite;BUILD_COREUTILS=1 编 uniq)
export SYMSAN_FGTEST=<.../fgtest> SYMSAN_KO_CLANG=<.../ko-clang>
for t in lava-base64_harness lava-uniq gfts-xml_read_fuzzer gfts-png_read_fuzzer \
         pcre2-pcre2_fuzzer sqlite-sqlite_fuzzer; do
  python3 benchmark/run_benchmark.py --engine symsan --hybrid --no-serial \
    --targets "$t" --np-list 8 --timeout 20 --rounds 1 --output /tmp/bench_$t
done
```

## 可视化对比图
一页交互式对比图表(concolic 贡献带 min–max 须、全流水线边覆盖、完整数据表、方法学注释;深/浅色自适应):
**https://claude.ai/code/artifact/9a75d7fe-5db5-46ca-982a-2c75a61c029d**
(默认私有,可在 claude.ai 上分享给团队。)
