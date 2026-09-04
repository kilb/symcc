# F441 Generation-Fenced MPI / ULFM 恢复证据

- 日期：2026-08-19
- 功能：F441
- 等级：I/T/E-local
- 结论：稳定endpoint身份、generation/shard/lease栅栏、ULFM communicator repair与全在途任务保守重放已形成可独立复验版本；本机真实进程退出完成4→3恢复并得到一致survivor receipt
- 边界：尚未接入主执行器默认热路径和durable QueryStore；没有跨节点MTTR、solver speedup、coverage或defect-yield结论

## 文件

| 文件 | 内容 |
| --- | --- |
| `deterministic.json` | 12 endpoints / 96 shards 的严格协议oracle |
| `capability.json` | 固定Open MPI 5.0.10四rank语义能力探测 |
| `live-failure.json` | rank物理退出后的4→3恢复结果 |
| `live-failure.log` | survivor阶段跟踪 |
| `live-failure-launcher-exit.txt` | 被有意终止rank的预期launcher状态86 |
| `system-openmpi4-negative.json` | 系统Open MPI 4.1.6多rank `Revoke`负结果 |
| `focused_tests.txt` / `associated_tests.txt` | 专项与关联测试 |
| `inventory_rebuild.txt` | 规范pytest node-id清单重建 |
| `full_python_gate.json` / `full_python_tests.txt` | 完整capability-closed Python门禁 |
| `llvm17_lit.txt` / `llvm18_lit.txt` | 串行双LLVM完整lit门禁 |
| `full_suite_summary.json` | 机器可读总结果 |
| `runtime_build.txt` | 固定runtime、配置和MPIX符号 |
| `static_checks.txt` | 语法、lint、脚本、索引、图形与diff检查 |
| `review_findings.txt` | 五轮review、发现、修复与边界 |
| `research_sources.md` | 主要论文与官方资料 |
| `source_manifest.txt` | F441权威源码/测试/文档SHA-256合同 |
| `f441_ulfm_generation_recovery.svg/.png` | 人工布局机制图和高分辨率渲染图 |

`SHA256SUMS.txt`覆盖除自身和`delivery_verifier.txt`以外的全部证据文件。
`delivery_verifier.txt`由顶层verifier产生，避免局部清单自引用。

## 实测摘要

- 专项：11 passed；关联：128 passed + 79 subtests；
- Python：1337 passed + 310 subtests，node-id 1337/1337；
- LLVM 17：331 discovered，329 passed，2 expected unsupported；
- LLVM 18：331 discovered，330 passed，1 expected unsupported；
- 协议oracle：2个endpoint失效，16/96 shards重分配，24/24在途lease重放，24/24旧token拒绝；
- capability：Open MPI 5.0.10四个rank六项语义全部通过；
- 真实失效：rank 3退出，world 4→3，一次repair，survivor receipt完全一致。

## 复现

```bash
bash benchmark/install_openmpi_ulfm_5_0_10.sh
F441_OUTPUT_DIR=/tmp/f441 benchmark/run_f441_ulfm_tests.sh
```

详细协议、执行顺序和结论边界见
`docs/codex/research-progress/Generation_Fenced_ULFM_Recovery_F441_2026-08-19.md`。
