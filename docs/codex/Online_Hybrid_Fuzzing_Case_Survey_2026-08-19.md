# 联网调研：适合体现混合模糊测试优势的公开 testcase

日期：2026-08-19  
目的：从公开论文和 benchmark 中筛选可用于证明“加入符号执行 / concolic execution 后，hybrid fuzzing 相比纯 fuzzing 能提升覆盖率或探索深层路径”的测试目标。

## 核心结论

当前最值得优先落地的 case 分三类：

| 优先级 | Case | 来源 | 为什么适合 |
|---|---|---|---|
| P0 | LAVA-M `base64` | LAVA-M / QSYM / ICSE23 CoFuzz | 本仓库已构建；本地 3 轮已测出 hybrid 230 edges vs AFL-only 153 edges，提升约 50.3% |
| P0 | LAVA-M `base64_harness` | LAVA-M 派生 harness | 本仓库已构建；可做辅助例，但波动较大 |
| P1 | `jhead` JPEG | ICSE23 hybrid fuzzing benchmark | 论文表中 AFL 304 edges，QSYM 885，CoFuzz 915，是最明显的真实程序覆盖正例 |
| P1 | `libpng` PNG | ICSE23 / FTS / FuzzBench / Magma | AFL 1496，QSYM 2058，CoFuzz 2311；本仓库已有 FTS PNG，可先跑，再接更贴近论文版本 |
| P1 | `libxml2` XML | ICSE23 / FTS / FuzzBench / Magma | AFL 5876，QSYM 7888，CoFuzz 8640；结构化 parser，很适合展示深层分支和同步策略 |
| P1 | `bento` MP4 | ICSE23 | AFL 3001，QSYM 4017，CoFuzz 6179；结构化二进制容器，hybrid 增益大 |
| P1 | binutils `strip/readelf/nm/objdump` | ICSE23 | ELF 格式、多层解析、真实工程目标，适合符号执行补齐 fuzzing 难进路径 |
| P2 | `file`, `wavpack`, `cyclonedds`, `libming`, `libtiff`, `libjpeg`, `tcpdump` | ICSE23 | 可作为真实程序扩展矩阵；其中 `file/wavpack/strip` 在 CoFuzz crash 结果中也有区分度 |
| P2 | Magma `libpng/libtiff/libxml2/sqlite3/openssl/php/poppler` | Magma | 更适合 bug/reached/triggered 指标，而不是只看 edge coverage |
| P3 | DARPA CGC / CQE dataset | Driller / QSYM / DigFuzz | 论文证据强，但对本仓库源码级 SymCC 落地成本较高，适合作为后续二进制执行或 SymQEMU 方向 |

2026-08-19 进一步补充：完整 testcase registry、推荐执行矩阵和本地状态映射见 [`Expanded_Hybrid_Showcase_Case_Registry_2026-08-19.md`](Expanded_Hybrid_Showcase_Case_Registry_2026-08-19.md)，机器可读版本见 `benchmark/showcase_cases_2026_08_19.json`。

## 论文与公开 benchmark 证据

### QSYM

QSYM 论文明确把 hybrid fuzzing 的动机定义为结合 fuzzing 与 concolic execution，并报告：

- LAVA-M：QSYM 比 VUzzer 多发现 14x bugs。
- DARPA CGC：QSYM 在 126 个 CGC binaries 中有 104 个超过 Driller。
- 真实程序：Dropbox Lepton、ffmpeg、OpenJPEG 等。

对本项目的启发：

- LAVA-M 是最直接能展示“fuzzing 卡住，concolic 解约束”的 benchmark。
- CGC 更适合二进制 concolic 或 SymQEMU，不是当前源码级 SymCC 的低成本目标。
- OpenJPEG/ffmpeg 是可作为中长期真实目标的强候选，但构建和 oracle 成本更高。

### ICSE23：Evaluating and Improving Hybrid Fuzzing / CoFuzz

这篇论文最适合指导我们选 case，因为它统一评估了多种 hybrid fuzzers，并构建了 15 个真实程序 benchmark：

| Program | Input | Argument |
|---|---|---|
| `readelf` | ELF | `-a @@` |
| `nm` | ELF | `-C @@` |
| `objdump` | ELF | `-D @@` |
| `strip` | ELF | `@@` |
| `tcpdump` | PCAP | `-r @@` |
| `libxml2` | XML | `@@` |
| `libjpeg` | JPEG | `@@` |
| `jhead` | JPEG | `@@` |
| `libpng` | PNG | `@@` |
| `libtiff` | TIFF | `@@` |
| `file` | FILE | `-m magic @@` |
| `bento` | MP4 | `@@` |
| `wavpack` | WAV | `-y @@` |
| `cyclonedds` | IDL | `@@` |
| `libming` | SWF | `@@` |

论文的 Table III 显示 QSYM 相比 AFL 的显著覆盖优势目标包括：

| Program | AFL edges | QSYM edges | QSYM 相对 AFL | CoFuzz edges | CoFuzz 相对 AFL |
|---|---:|---:|---:|---:|---:|
| `jhead` | 304 | 885 | +191.1% | 915 | +201.0% |
| `libpng` | 1496 | 2058 | +37.6% | 2311 | +54.5% |
| `libxml2` | 5876 | 7888 | +34.2% | 8640 | +47.0% |
| `bento` | 3001 | 4017 | +33.9% | 6179 | +105.9% |
| `strip` | 6340 | 7624 | +20.3% | 9094 | +43.4% |
| `cyclonedds` | 4822 | 5612 | +16.4% | 5932 | +23.0% |
| `objdump` | 7358 | 8304 | +12.9% | 8710 | +18.4% |
| `file` | 2283 | 2553 | +11.8% | 2851 | +24.9% |
| `nm` | 5127 | 5602 | +9.3% | 8234 | +60.6% |

最值得先做的是 `jhead/libpng/libxml2/bento/strip`。这些目标满足三个条件：

1. 真实程序，输入格式复杂。
2. 论文中 AFL 和 hybrid 差距明显。
3. 能解释成 fuzzing 对精确结构/校验/深层状态机难以突破，而 concolic execution 能生成跨分支输入。

### LAVA-M

LAVA-M 使用 coreutils 8.24 的 `base64/md5sum/uniq/who`。LAVA 论文说明它选择这四个 file-input 程序，并向每个程序注入多个可验证 bugs。

ICSE23 的 LAVA-M 表显示，主流 hybrid fuzzers 在 `base64/md5sum/uniq` 上能较快暴露列出的目标；`who` 是最有区分度的长期目标。对本项目而言：

- `base64` 最适合作为当前强正例，本地已复现覆盖率提升。
- `md5sum/uniq/who` 在当前短时 edge coverage 下不适合强结论；`who` 后续应转为 bug-trigger count/time-to-trigger。

### Pangolin

Pangolin 使用 polyhedral path abstraction，论文报告在 LAVA-M 和 9 个真实程序上覆盖率提升 10%-30%。这与本项目已经实现的 polyhedral/Z3 context reuse、path abstraction、sampling/reuse 方向高度相关。

适合复现方向：

- 先在 LAVA-M `base64` 做当前实现对照。
- 再在 ICSE23 的 `libpng/libxml2/jhead/bento` 上做 polyhedral context reuse/constraint sampling 消融。

### DigFuzz

DigFuzz 的核心是 probabilistic path prioritization。论文摘要和 NDSS 页面显示，其 concolic execution 在 CQE dataset 上比 Driller 的 concolic execution 贡献了更多覆盖和发现结果。

适合本项目借鉴：

- 用 prefix DAG / target distance / branch difficulty 构建类似“path difficulty”的调度分数。
- 目标 case 优先选 CGC/CQE 或 LAVA-M；但 CGC/CQE 与当前源码级 SymCC 构建链不完全匹配，因此不作为 P0。

### FTS / FuzzBench / Magma

Google Fuzzer Test Suite 官方目标是提供来自真实库、有 hard-to-find code paths 的 benchmark；本仓库已构建其中的 PNG/XML 目标。

FuzzBench 官方 benchmark 列表包含 `libpng-1.2.56`, `libxml2-v2.9.2`, `sqlite3_ossfuzz`, `harfbuzz`, `lcms`, `libjpeg-turbo`, `libpcap` 等，适合标准化覆盖率实验。

Magma 提供 ground-truth fuzzing benchmark。样例报告和官方文档列出 `libpng/libtiff/libxml2/openssl/php/poppler/sqlite3` 等目标，更适合用 reached/triggered 指标评估，而不是只看覆盖率。

## 本仓库当前映射

| 候选 | 本地状态 | 建议 |
|---|---|---|
| LAVA-M `base64` | 已构建，已 3 轮确认 hybrid 优势 | 作为当前主汇报 case |
| LAVA-M `base64_harness` | 已构建，单轮有正向但不稳定 | 辅助说明，不作主结论 |
| FTS `xml_read_fuzzer` | 已构建 | 继续做长时，短时 AFL-only 很强，不宜直接主张 hybrid 优势 |
| FTS `png_read_fuzzer` | 已构建 | 与 ICSE23 `libpng` 方向一致，但当前短时 AFL-only 更强；需要版本/seed 对齐 |
| `pcre2/sqlite/libarchive/freetype2` | 已构建 | 适合说明真实目标链路，不适合当前覆盖率强正例 |
| `jhead` | 已构建并测试 | ICSE23 强候选，但本地 60s 短时等核测试 AFL-only 更高；后续需修 QSYM 非关系型 branch 断言并拉长预算 |
| ICSE23 `libxml2/libpng` 精确版本 | 未按论文版本完整构建 | P1，建议对齐版本和 seed |
| `bento` | 未构建 | P1，高潜力 |
| binutils `readelf/nm/objdump/strip` | 未构建 | P1/P2，工程量中等，ELF seeds 需要整理 |
| Magma | 未完整构建 | P2，做 bug/reached/triggered 指标 |

## 推荐实施顺序

1. 保留 `lava-base64` 作为强正例，继续跑 5-10 rounds、不同时间预算，生成置信区间。
2. 继续深化 `jhead` benchmark。论文中 AFL 304 edges，QSYM 885，差距最大；本轮已接入，但当前短时实验没有复现优势，下一步应修 QSYM 断言、对齐 seed corpus 并跑 10min/1h。
3. 对齐 `libpng/libxml2` 的 ICSE23/FTS/FuzzBench 版本和 seed corpus，做 10min/1h 的 AFL-only vs hybrid。
4. 新增 `bento` MP4。它在 CoFuzz 中提升非常明显，适合展示结构化二进制格式下的 synchronization/scheduling 价值。
5. 新增 binutils ELF 四目标，尤其 `strip` 和 `nm`。它们适合展示真实工程目标上的多模块解析和深层状态探索。
6. 接入 Magma 的 `libpng/libxml2/sqlite3`，改用 reached/triggered 指标补充覆盖率。

## 参考资料

- QSYM: A Practical Concolic Execution Engine Tailored for Hybrid Fuzzing, USENIX Security 2018: https://www.usenix.org/conference/usenixsecurity18/presentation/yun
- Evaluating and Improving Hybrid Fuzzing, ICSE 2023: https://shadowmydx.github.io/papers/icse23main-p966.pdf
- LAVA: Large-scale Automated Vulnerability Addition, IEEE S&P 2016: https://seclab.nu/static/publications/sp2016lava.pdf
- Pangolin: Incremental Hybrid Fuzzing with Polyhedral Path Abstraction, IEEE S&P 2020: https://5hadowblad3.github.io/files/SP2020.pdf
- DigFuzz / Probabilistic Path Prioritization for Hybrid Fuzzing, NDSS 2019: https://www.ndss-symposium.org/ndss-paper/send-hardest-problems-my-way-probabilistic-path-prioritization-for-hybrid-fuzzing/
- Google Fuzzer Test Suite: https://github.com/google/fuzzer-test-suite
- FuzzBench benchmark list: https://google.github.io/fuzzbench/reference/benchmarks/
- Magma sample report: https://hexhive.epfl.ch/magma/reports/sample/
