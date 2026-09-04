#!/usr/bin/env python3
"""Generate and verify the complete feature appendix in the technology compendium."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


DOCS = Path(__file__).resolve().parent
CODEX = DOCS / "codex"
ARCHIVE = CODEX / "New_Implementation_Archive.md"
TRACEABILITY = CODEX / "Development_History_Traceability.md"
COMPENDIUM = CODEX / "Current_Technology_Compendium.md"
START = "<!-- FEATURE_INDEX_START -->"
END = "<!-- FEATURE_INDEX_END -->"
EXPECTED_IDS = set(range(442))


def feature_range() -> str:
    """Return the contiguous feature range rendered in current-snapshot prose."""
    if not EXPECTED_IDS or EXPECTED_IDS != set(range(max(EXPECTED_IDS) + 1)):
        raise RuntimeError("EXPECTED_IDS must be a contiguous zero-based range")
    return f"F00-F{max(EXPECTED_IDS):02d}"


def primary_layer(feature_id: int) -> str:
    """Return one primary layer; cross-layer relationships stay in the prose."""
    if feature_id == 297:
        return "基础正确性"
    if feature_id == 298:
        return "MPI / hybrid"
    if feature_id == 299:
        return "基础正确性 / evidence"
    if 300 <= feature_id <= 301:
        return "Query / solver"
    if 302 <= feature_id <= 305:
        return "MPI / solver"
    if 306 <= feature_id <= 307:
        return "Evidence / benchmark"
    if 308 <= feature_id <= 310:
        return "调度与目标引导"
    if 311 <= feature_id <= 312:
        return "调度与目标引导 / MPI"
    if 313 <= feature_id <= 346 or 351 <= feature_id <= 358:
        return "MPI / 分布式状态"
    if 347 <= feature_id <= 348:
        return "continuation / 分布式状态"
    if 349 <= feature_id <= 350:
        return "分布式状态 / CAS 完整性"
    if 359 <= feature_id <= 362:
        return "测试 / 科研证据"
    if 363 <= feature_id <= 371 or feature_id == 381:
        return "Query / solver 基础设施"
    if feature_id == 372:
        return "分布式状态 / Query 基础设施"
    if feature_id == 373:
        return "Query / 分布式状态基础设施"
    if feature_id == 374:
        return "调度 / agentic execution"
    live_layers = {
        375: "Live continuation / LLVM semantics",
        376: "Live continuation / LLVM semantics",
        377: "Live continuation / environment models",
        378: "Live continuation / exception semantics",
        379: "Live continuation / exception values",
        380: "Live continuation / exception memory",
        382: "Live continuation / C++ exception",
        383: "Live continuation / MemorySSA alias contract",
        384: "Live continuation / relational alias contract",
        385: "Live continuation / byte-lane MemorySSA contract",
        386: "Live continuation / endpoint MemorySSA contract",
        387: "Live continuation / loop-carried MemorySSA contract",
        388: "Live continuation / conditional loop MemorySSA contract",
        389: "Live continuation / multi-arm loop MemorySSA contract",
        390: "Live continuation / recursive loop MemorySSA contract",
        391: "Live continuation / persistent search and work ownership",
        400: "Live continuation / 持久调度",
        401: "Live continuation / 路径覆盖调度",
        402: "Live continuation / 分支覆盖剪枝",
        403: "Live continuation / 具体约束引导调度",
        404: "MPI / 跨运行种子选择",
        405: "Live continuation / heap points-to",
        406: "Live continuation / heap initialization proof",
        407: "Live continuation / guarded heap initialization proof",
        408: "Live continuation / MemorySSA/AA heap initialization proof",
        409: "Live continuation / interprocedural heap effect proof",
        410: "Live continuation / symbolic region memory proof",
        411: "Live continuation / loop MemoryPhi induction proof",
        412: "Live continuation / affine loop memory proof",
        413: "Live continuation / conditional loop memory proof",
        414: "Live continuation / multi-latch loop memory proof",
        415: "Live continuation / ordered loop memory effect proof",
        416: "Live continuation / nested loop memory effect proof",
        417: "Live continuation / nested loop value summary proof",
        418: "Live continuation / two-dimensional affine loop value proof",
        419: "Live continuation / affine symbolic value proof",
        420: "Live continuation / piecewise-affine value proof",
        421: "Live continuation / bounded multi-guard value proof",
        422: "Live continuation / transactional ITE loop replacement",
        423: "Agentic planning / live continuation / replay authority",
        424: "Query solving / Prefix DAG scheduling",
        425: "Concurrency / native execution graph / atomic replay",
        426: "Distributed solver / Query IR / incremental context",
        427: "Distributed solver / independently checked UNSAT result",
        428: "Distributed solver / verified learned knowledge",
        429: "Distributed solver / artifact lifecycle and GC",
        430: "Query solver / native state reuse",
        431: "Distributed solver / proof-carrying structural core reuse",
        432: "Distributed solver / activation-scoped checked clause exchange",
        433: "Distributed solver / realtime checked proof stream",
        434: "Distributed solver / qualified MPI evaluation",
        435: "Distributed solver / adaptive proof admission",
        436: "Distributed solver / native clause activity telemetry",
        437: "Distributed solver / utility-aware worker pairing",
        438: "Distributed solver / generation-fenced malleability",
        439: "Distributed solver / proof wire interoperability",
        440: "Distributed solver / native parallel proof confirmation",
        441: "MPI / generation-fenced ULFM recovery",
    }
    if feature_id in live_layers:
        return live_layers[feature_id]
    if feature_id == 392:
        return "Concurrency / executable graph exploration"
    under_constrained = {
        393: "Under-constrained execution / durable object state",
        394: "Under-constrained execution / explicit object safety",
        395: "Under-constrained execution / byte-level initialization safety",
    }
    if feature_id in under_constrained:
        return under_constrained[feature_id]
    if 396 <= feature_id <= 398:
        return "调度与目标引导 / 参数自配置"
    if feature_id == 399:
        return "Query IR / 结构化查询调度"
    if feature_id in {0, 26}:
        return "基础正确性"
    if 17 <= feature_id <= 25:
        return "MPI / hybrid"
    if feature_id in {1, 4, 6, 7, 22, 28, 247}:
        return "遥测与覆盖"
    if feature_id in {5, 8, 9, 10, 14, 15, 186}:
        return "调度与目标引导"
    if feature_id in {16, 21, 25, 60, 67}:
        return "证据与 benchmark"
    if feature_id == 11:
        return "UCSan"
    if feature_id in {13, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49,
                      50, 51, 52, 53, 54, 55, 56, 57, 58, 61, 62, 63,
                      64, 65, 221}:
        return "并发状态空间"
    if feature_id in {30, 33} or 193 <= feature_id <= 200:
        return "字符串双表示"
    if feature_id in {36, 187, 190, 191, 192} or (
        201 <= feature_id <= 235 and feature_id != 221
    ):
        return "语法与解析"
    if feature_id in {12, 38, 59, 66} or 68 <= feature_id <= 175:
        return "Live continuation"
    if feature_id in {37} or 248 <= feature_id <= 296:
        return "IFSS / Hydra"
    if feature_id in {2, 3, 27, 29, 31, 32, 34, 35, 177, 178, 181,
                      188, 189, 236, 237, 238, 239, 240, 241, 242, 243,
                      244, 245, 246} or 176 <= feature_id <= 185:
        return "Query / solver"
    raise ValueError(f"F{feature_id:02d} has no primary layer")


def escape_cell(text: str) -> str:
    return text.replace("|", r"\|").replace("\n", " ").strip()


def read_inventory() -> tuple[dict[int, str], dict[int, str], dict[int, str]]:
    archive_lines = ARCHIVE.read_text(encoding="utf-8").splitlines()
    titles: dict[int, str] = {}
    statuses: dict[int, str] = {}
    sections: dict[int, str] = {}

    table_row = re.compile(
        r"^\| F(\d+) \| (.*?) \| (.*?) \| (.*?) \| ([A-Z/]+) \|$"
    )
    for line in archive_lines:
        match = table_row.match(line)
        if not match:
            continue
        feature_id = int(match.group(1))
        if feature_id not in titles:
            titles[feature_id] = match.group(2).strip()
            statuses[feature_id] = match.group(5).strip()

    heading = re.compile(
        r"^## ([0-9]+)\. F(\d+)(?:/F(\d+))?："
        r"(.+?)(?:（20\d\d-\d\d-\d\d）)?$"
    )
    for line in archive_lines:
        match = heading.match(line)
        if not match:
            continue
        section, first, second, title = match.groups()
        ids = [int(first)]
        if second is not None:
            ids.append(int(second))
        for feature_id in ids:
            sections[feature_id] = section
            if feature_id >= 173:
                titles[feature_id] = title.strip()

    sections[0] = "3.1"
    for feature_id in range(17, 26):
        sections[feature_id] = f"17.{feature_id - 16}"

    trace_lines = TRACEABILITY.read_text(encoding="utf-8").splitlines()
    trace_row = re.compile(r"^\| F(\d+) \|")
    for line in trace_lines:
        match = trace_row.match(line)
        if not match:
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2 or not re.fullmatch(
            r"[A-Z](?:/[A-Z])+(?:-[a-z]+)?", cells[-1]
        ):
            continue
        feature_id = int(match.group(1))
        statuses.setdefault(feature_id, cells[-1])

    missing_titles = EXPECTED_IDS - set(titles)
    missing_statuses = EXPECTED_IDS - set(statuses)
    missing_sections = EXPECTED_IDS - set(sections)
    if missing_titles or missing_statuses or missing_sections:
        raise RuntimeError(
            "incomplete feature inventory: "
            f"titles={sorted(missing_titles)}, "
            f"statuses={sorted(missing_statuses)}, "
            f"sections={sorted(missing_sections)}"
        )
    if set(titles) != EXPECTED_IDS:
        raise RuntimeError(f"unexpected feature IDs: {sorted(set(titles) - EXPECTED_IDS)}")

    return titles, statuses, sections


def render_index() -> str:
    titles, statuses, sections = read_inventory()
    lines = [
        "",
        "| ID | 技术名称 | 主层 | 当前证据 | 权威明细 |",
        "| --- | --- | --- | --- | --- |",
    ]
    # Keep rendering tied to the same authoritative set used by inventory
    # validation so extending EXPECTED_IDS cannot silently omit the last row.
    for feature_id in sorted(EXPECTED_IDS):
        lines.append(
            "| "
            f"F{feature_id:02d} | "
            f"{escape_cell(titles[feature_id])} | "
            f"{primary_layer(feature_id)} | "
            f"{statuses[feature_id]} | "
            f"[档案 §{sections[feature_id]}](New_Implementation_Archive.md) |"
        )
    lines.append("")
    return "\n".join(lines)


def update_compendium(check: bool) -> None:
    original = COMPENDIUM.read_text(encoding="utf-8")
    if original.count(START) != 1 or original.count(END) != 1:
        raise RuntimeError("feature-index markers are missing or duplicated")
    before, remainder = original.split(START, 1)
    _, after = remainder.split(END, 1)
    generated = before + START + render_index() + END + after
    current_range = feature_range()
    generated, coverage_replacements = re.subn(
        r"(?m)^(- 覆盖范围：当前工作树相对上游 SymCC 新增或显著扩展的 )`F00-F\d+`$",
        rf"\g<1>`{current_range}`",
        generated,
        count=1,
    )
    generated, inventory_replacements = re.subn(
        r"(?m)(同步维护。每个 )`F00-F\d+`( 必须恰好出现一次；)",
        rf"\g<1>`{current_range}`\g<2>",
        generated,
        count=1,
    )
    if coverage_replacements != 1 or inventory_replacements != 1:
        raise RuntimeError("current feature-range prose markers are missing or duplicated")

    if check:
        if generated != original:
            raise SystemExit(
                "Current_Technology_Compendium.md feature index is stale; "
                "run this script without --check"
            )
        return
    COMPENDIUM.write_text(generated, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when the committed appendix differs from authoritative sources",
    )
    args = parser.parse_args()
    update_compendium(check=args.check)


if __name__ == "__main__":
    main()
