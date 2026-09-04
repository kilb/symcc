#!/usr/bin/env python3
"""Merge SymCC directed-coloration fragments into a whole-program map."""

from __future__ import annotations

import argparse
import os
from collections import defaultdict, deque
from pathlib import Path
from typing import Iterable


def _split(line: str) -> list[str]:
    return line.rstrip("\n").split()


def merge_lines(lines: Iterable[str]) -> list[str]:
    comments: list[str] = []
    legacy_rows: list[str] = []
    graph: dict[str, set[str]] = defaultdict(set)
    targets: set[str] = set()
    entries: dict[str, list[str]] = defaultdict(list)
    entry_functions: dict[str, str] = {}
    exits: dict[str, list[str]] = defaultdict(list)
    addr_by_sig: dict[str, list[str]] = defaultdict(list)
    external_calls: list[tuple[str, str]] = []
    indirect_calls: list[tuple[str, str]] = []
    sites: dict[str, tuple[str, str, str, str]] = {}

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if not line.startswith("#"):
            legacy_rows.append(raw if raw.endswith("\n") else raw + "\n")
            continue
        comments.append(raw if raw.endswith("\n") else raw + "\n")
        fields = _split(line)
        if not fields:
            continue
        tag = fields[0]
        if tag == "#N" and len(fields) >= 2:
            graph.setdefault(fields[1], set())
        elif tag == "#E" and len(fields) >= 3:
            graph[fields[1]].add(fields[2])
            graph.setdefault(fields[2], set())
        elif tag == "#TARGET" and len(fields) >= 2:
            targets.add(fields[1])
            graph.setdefault(fields[1], set())
        elif tag == "#ENTRY" and len(fields) >= 4:
            func, _sig, block = fields[1], fields[2], fields[3]
            entries[func].append(block)
            entry_functions[block] = func
            graph.setdefault(block, set())
        elif tag == "#ADDR" and len(fields) >= 4:
            _func, sig, block = fields[1], fields[2], fields[3]
            addr_by_sig[sig].append(block)
            graph.setdefault(block, set())
        elif tag == "#EXIT" and len(fields) >= 3:
            exits[fields[1]].append(fields[2])
            graph.setdefault(fields[2], set())
        elif tag == "#X" and len(fields) >= 3:
            external_calls.append((fields[1], fields[2]))
            graph.setdefault(fields[1], set())
        elif tag == "#IX" and len(fields) >= 3:
            indirect_calls.append((fields[1], fields[2]))
            graph.setdefault(fields[1], set())
        elif tag == "#SITE" and len(fields) >= 5:
            loc = fields[5] if len(fields) >= 6 else "-"
            sites[fields[1]] = (fields[2], fields[3], fields[4], loc)
            graph.setdefault(fields[2], set())

    for caller_block, callee in external_calls:
        for entry in entries.get(callee, []):
            graph[caller_block].add(entry)
        for exit_block in exits.get(callee, []):
            graph[exit_block].add(caller_block)

    for caller_block, sig in indirect_calls:
        for entry in addr_by_sig.get(sig, []):
            graph[caller_block].add(entry)
            func = entry_functions.get(entry)
            if func:
                for exit_block in exits.get(func, []):
                    graph[exit_block].add(caller_block)

    if not sites or not targets:
        # A DynamiQ structural-task graph deliberately has no target or numeric
        # distance rows. Preserve its compiler summaries when the same merger is
        # used to combine multi-module fragments.
        if comments:
            return ["# symcc-structural-task-graph-merged-v1\n", *comments,
                    *legacy_rows]
        return legacy_rows

    reverse: dict[str, set[str]] = defaultdict(set)
    for src, dsts in graph.items():
        reverse.setdefault(src, set())
        for dst in dsts:
            reverse[dst].add(src)

    distance: dict[str, int] = {}
    queue: deque[str] = deque()
    for target in sorted(targets):
        if target not in distance:
            distance[target] = 0
            queue.append(target)
    while queue:
        node = queue.popleft()
        for pred in reverse.get(node, set()):
            next_distance = distance[node] + 1
            if pred in distance and distance[pred] <= next_distance:
                continue
            distance[pred] = next_distance
            queue.append(pred)

    best_rows: dict[str, tuple[int, str]] = {}
    for site, (block, func, opcode, loc) in sites.items():
        if block not in distance:
            continue
        row = f"{site} {distance[block]} # {func} {opcode} {loc}\n"
        old = best_rows.get(site)
        if old is None or distance[block] < old[0]:
            best_rows[site] = (distance[block], row)

    if not best_rows:
        return ["# symcc-directed-distance-merged-v1\n", *comments,
                *legacy_rows]
    merged = [
        "# symcc-directed-distance-merged-v1\n",
        *comments,
    ]
    merged.extend(row for _dist, row in sorted(best_rows.values()))
    return merged


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", help="fragment file emitted by SYMCC_COLORATION_OUT")
    parser.add_argument("--output", "-o", help="merged output path; defaults to stdout")
    args = parser.parse_args()

    input_path = Path(args.input)
    lines = input_path.read_text(encoding="utf-8", errors="ignore").splitlines(True)
    merged = merge_lines(lines)
    text = "".join(merged)
    if not args.output:
        print(text, end="")
        return 0

    output_path = Path(args.output)
    tmp_path = output_path.with_name(f".{output_path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
