# docs/cc —— 2026-07 项目汇报材料

这个目录存放一份独立维护的项目汇报材料，**与 `docs/` 下的同名文件相互独立**。

## 为什么单独一个目录

`docs/Project_Report_2026-07.md` 与 `docs/diagrams/render_project_report.py` 同时被
多方修改过，出现过两类问题：

1. 基于旧内容做的字符串替换静默落空（会报未命中，可察觉）；
2. 整文件回写可能覆盖他人在读写之间提交的改动（**不报错，难察觉**）。

因此把本份材料整体迁到 `docs/cc/`。`docs/` 下的原始文件保持原样，归其原维护者。

## 内容

| 路径 | 说明 |
|---|---|
| [`Project_Report_2026-07.md`](Project_Report_2026-07.md) | 汇报正文：系统架构、关键技术原理、实验与结论 |
| `diagrams/render_project_report.py` | 配图生成脚本，一条命令重出全部 21 张图 |
| `diagrams/report/*.svg` `*.png` | 21 张配图（R-1 … R-21） |
| `diagrams/report/src/*.dot` | 结构图的 Graphviz 图源 |

正文另引用 25 张 QA3 图集配图（`../diagrams/qa3/`）与若干证据文件
（`../evidence/`），这些是**只读引用**，不由本目录维护。

## 重新生成配图

```bash
sudo apt-get install graphviz
python3 docs/cc/diagrams/render_project_report.py
```

脚本从 `docs/diagrams/render_qa3_diagrams.py` 引入配色与图元常量（只读），
因此两套图集混排时视觉一致。结构图走 Graphviz DOT，定量图直接生成 SVG，
PNG 由 headless Chrome 栅格化。

`fig-r2-scale` 的全部计数在**渲染时当场从仓库统计**（提交数、功能条目数、
`test/` 文件数、`Configuration.txt` 登记的 `SYMCC_*` 数、各目录行数），
不写死，避免随工作树漂移。

## 数据来源与证据等级

正文每个数字都标注来源与等级，规则见正文 §4.1 与图 R-14。主要来源：

| 内容 | 文件 | 等级 |
|---|---|---|
| 20 轮独立样本配置比较 | `../evidence/current-eval-2026-07-30/independent_comparisons.csv` | A |
| LAVA-M 求解技术族消融 | `../evidence/lava_m_current_2026_07_30/` | B |
| SymSan 六目标基准 | `../symsan_hybrid_benchmark_6targets.md` | B |
| 机制实测（漏斗 / 策略占比 / 求解开关等） | `../Architecture_QA3.md`、`../../benchmark/qa3_repro/` | C |

## 一致性自检

改完正文或配图后建议跑一遍：

```bash
python3 - <<'EOF'
import pathlib, re, unicodedata
base = pathlib.Path("docs/cc")
s = (base / "Project_Report_2026-07.md").read_text()

def slug(t: str) -> str:
    """与 GitHub 一致的锚点生成规则（保留 CJK）。"""
    t = unicodedata.normalize("NFKC", t)
    t = re.sub(r"[^\w\s-]", "", t).strip().lower()
    return re.sub(r"[-\s]+", "-", t)

heads = {slug(re.sub(r"[`*]", "", m.group(2)).strip())
         for m in re.finditer(r"^(#{1,4})\s+(.*)$", s, re.M)}
heads |= set(re.findall(r'<a id="([^"]+)"', s))
print("失效锚点:", [m.group(1) for m in re.finditer(r"\]\(#([^)]+)\)", s)
                    if m.group(1) not in heads] or "无")
print("失效链接:", [m.group(1) for m in re.finditer(r"\]\(([^)#][^)]*)\)", s)
                    if not m.group(1).startswith("http")
                    and not (base / m.group(1)).exists()] or "无")
cap = [int(x) for x in re.findall(r"> 图 R-(\d+)", s)]
fil = [int(x) for x in re.findall(r"!\[[^\]]*\]\(diagrams/report/fig-r(\d+)-", s)]
print("题注与图片序列一致:", cap == fil)
EOF
```
