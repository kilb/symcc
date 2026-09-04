# F453 在线 Activity/Cost 引导分块证据

本目录封存 F453 的机制 oracle、专项/耦合/完整门禁、静态检查、环境和多轮审查记录。权威设计说明为
[`Online_Activity_Cost_Guided_Cubing_F453_2026-08-26.md`](../../research-progress/Online_Activity_Cost_Guided_Cubing_F453_2026-08-26.md)。

## 证据内容

| 文件 | 含义 |
| --- | --- |
| `oracle.json` | static/activity/cost 各 5 次、同公式族、等配置 CPU-ms 的生产机制实验 |
| `focused-tests.log` | F453 policy 与 partition production integration 专项门禁 |
| `coupled-tests.log` | activity/proof/partition/backend/QueryStore 耦合回归 |
| `full-python-gate.log` / `.json` / `.time.txt` | capability-closed、nodeid-exact 完整 Python 门禁 |
| `static-checks.txt` | ruff、py_compile、SVG parser 与 diff check |
| `review.txt` | 八轮反例驱动审查和修复 |
| `environment.txt` | 封存环境版本与命令 |
| `SHA256SUMS.txt` | 除自身外全部证据文件的 SHA-256 |

## 机制结果

- 15 次 trial，static/activity/cost 各 5 次；同一 formula family 与 policy SHA-256；
- 配置 CPU 上限均为 8000 ms，guided prerun 从上限扣除；
- static/activity/cost 的总时间中位为 348.016/1813.961/1742.517 ms；
- cost 的 cube 序列为 `4,8,16,2,2`，完成候选探索后继续选择观测成本较优的 2；
- guided 两个 arm 各消费 60 个可重放 checked activity receipt，static 为 0。
- 完整门禁 `1482 passed + 310 subtests`，16 项能力齐全，nodeid `1482/1482`，零退化。

该合成矛盾公式是策略和预算机制 oracle，不是优势展示 benchmark。它显示 guided overhead 在简单查询上
占主导，因此支持保留 static 门控，不支持 solver speedup、coverage 或 defect-yield 声明。

## 复现

```bash
python3 benchmark/check_qfbv_online_cubing_oracles.py \
  --rounds 5 --cube-candidates 2,4,8,16 \
  --output /tmp/f453-online-cubing.json
```

专项、耦合与完整门禁命令见 `docs/Testing.txt`。完整门禁要求所有 16 项显式能力存在、零 skip/xfail/
deselect，并与 `test/pytest-nodeids.json` 精确一致。

## 声明等级

`I/T/E-mechanism`。本目录不包含公开目标、长时 campaign、多 seed 统计功效或端到端混合模糊测试收益。
