"""Heap/object-path frontier optimization for UCSan/POSE-style execution."""

from __future__ import annotations

from dataclasses import dataclass
import heapq
import math
from typing import Any, Mapping


@dataclass(frozen=True)
class HeapPath:
    path_id: str
    object_id: int
    depth: int
    allocation_bytes: int
    alias_count: int
    target_distance: float
    novelty: float

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "HeapPath":
        path_id = str(raw.get("path_id", "")).strip()
        if not path_id:
            raise ValueError("path_id is required")
        integer_values = ("object_id", "depth", "allocation_bytes", "alias_count")
        parsed_ints: list[int] = []
        for name in integer_values:
            value = raw.get(name, 0)
            if isinstance(value, bool):
                raise ValueError(f"{name} must be an integer")
            try:
                parsed = int(value)
            except (TypeError, ValueError, OverflowError) as error:
                raise ValueError(f"{name} must be an integer") from error
            if parsed < 0:
                raise ValueError(f"{name} must be non-negative")
            parsed_ints.append(parsed)
        try:
            distance = float(raw.get("target_distance", 0.0))
            novelty = float(raw.get("novelty", 0.0))
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("heap path score fields must be numeric") from error
        if not math.isfinite(distance) or not math.isfinite(novelty):
            raise ValueError("heap path score fields must be finite")
        return cls(path_id, parsed_ints[0], parsed_ints[1], parsed_ints[2],
                   parsed_ints[3], max(0.0, distance),
                   max(0.0, min(1.0, novelty)))


class HeapPathOptimizer:
    """Select a bounded Pareto-like frontier and expose optimality evidence."""

    def __init__(self, *, max_paths: int = 65536):
        self.max_paths = max(1, min(1_000_000, int(max_paths)))
        self.paths: dict[str, HeapPath] = {}

    @staticmethod
    def score(path: HeapPath) -> float:
        return (0.42 * path.novelty + 0.28 / (1.0 + path.target_distance)
                + 0.18 * min(1.0, path.alias_count / 4.0)
                + 0.12 / (1.0 + path.depth + path.allocation_bytes / 4096.0))

    def add(self, raw: Mapping[str, Any] | HeapPath) -> HeapPath:
        path = raw if isinstance(raw, HeapPath) else HeapPath.from_mapping(raw)
        if path.path_id not in self.paths and len(self.paths) >= self.max_paths:
            victim = min(self.paths.values(), key=self.score)
            self.paths.pop(victim.path_id)
        self.paths[path.path_id] = path
        return path

    def select(self, limit: int = 1) -> list[HeapPath]:
        if not 1 <= int(limit) <= 256:
            raise ValueError("limit must be in 1..256")
        return heapq.nsmallest(
            min(int(limit), len(self.paths)), self.paths.values(),
            key=lambda path: (-self.score(path), path.path_id))

    def evidence(self, selected: list[HeapPath]) -> dict[str, Any]:
        scores = [self.score(path) for path in selected]
        return {
            "schema": "symcc-heap-path-optimality-v1",
            "selected": [path.path_id for path in selected],
            "frontier_size": len(self.paths),
            "best_score": max(scores, default=0.0),
            "objective": "novelty+target-distance+alias-awareness-cost",
        }
