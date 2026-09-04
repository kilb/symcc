"""Bounded multi-objective scheduling for live symbolic states.

The engine owns state execution; this module only ranks ready states and learns
from completed transitions.  It deliberately keeps no solver semantics so it
can be used by MPI workers, the local executor, or a replay controller.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import threading
from typing import Any, Mapping


MAX_STATES = 100_000


def _finite(value: Any, name: str, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{name} must be finite") from error
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _nonnegative(value: Any, name: str) -> float:
    result = _finite(value, name)
    if result < 0:
        raise ValueError(f"{name} must be non-negative")
    return result


@dataclass(frozen=True)
class LiveState:
    state_id: str
    prefix_id: str
    depth: int
    target_distance: float
    constraint_cost: float
    novelty: float
    data_novelty: float
    pending_branches: int
    worker: int = 0

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "LiveState":
        if not isinstance(raw, Mapping):
            raise ValueError("live state must be an object")
        state_id = str(raw.get("state_id", "")).strip()
        prefix_id = str(raw.get("prefix_id", state_id)).strip()
        if not state_id or not prefix_id or len(state_id) > 512 or len(prefix_id) > 512:
            raise ValueError("live state identifiers are invalid")
        depth = raw.get("depth", 0)
        if isinstance(depth, bool):
            raise ValueError("depth must be an integer")
        try:
            depth = int(depth)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("depth must be an integer") from error
        if depth < 0 or depth > 1_000_000:
            raise ValueError("depth is outside its bound")
        pending = raw.get("pending_branches", 0)
        if isinstance(pending, bool):
            raise ValueError("pending_branches must be an integer")
        try:
            pending = int(pending)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("pending_branches must be an integer") from error
        if pending < 0 or pending > 1_000_000:
            raise ValueError("pending_branches is outside its bound")
        return cls(
            state_id=state_id,
            prefix_id=prefix_id,
            depth=depth,
            target_distance=_nonnegative(raw.get("target_distance", 0), "target_distance"),
            constraint_cost=_nonnegative(raw.get("constraint_cost", 0), "constraint_cost"),
            novelty=max(0.0, min(1.0, _finite(raw.get("novelty", 0), "novelty"))),
            data_novelty=max(0.0, min(1.0, _finite(raw.get("data_novelty", 0), "data_novelty"))),
            pending_branches=pending,
            worker=max(0, int(raw.get("worker", 0))),
        )


@dataclass
class _Stats:
    pulls: int = 0
    reward: float = 0.0
    cost: float = 0.0
    failures: int = 0


class LiveStateScheduler:
    """Thread-safe UCB scheduler with novelty, distance and cost awareness."""

    def __init__(self, *, max_states: int = MAX_STATES, exploration: float = 0.35):
        if isinstance(max_states, bool) or not 1 <= int(max_states) <= MAX_STATES:
            raise ValueError("max_states is outside its bound")
        self.max_states = int(max_states)
        self.exploration = max(0.0, min(4.0, _finite(exploration, "exploration")))
        self._states: dict[str, LiveState] = {}
        self._stats: dict[str, _Stats] = {}
        self._total_pulls = 0
        self._lock = threading.RLock()

    def upsert(self, raw: Mapping[str, Any] | LiveState) -> LiveState:
        state = raw if isinstance(raw, LiveState) else LiveState.from_mapping(raw)
        with self._lock:
            if state.state_id not in self._states and len(self._states) >= self.max_states:
                # Evict the least promising state, never an arbitrary one.
                victim = min(self._states, key=lambda key: self.score(self._states[key]))
                self._states.pop(victim, None)
                self._stats.pop(victim, None)
            self._states[state.state_id] = state
            self._stats.setdefault(state.state_id, _Stats())
        return state

    def remove(self, state_id: str) -> None:
        with self._lock:
            self._states.pop(state_id, None)
            self._stats.pop(state_id, None)

    def score(self, state: LiveState) -> float:
        stats = self._stats.get(state.state_id, _Stats())
        total = max(1, self._total_pulls)
        exploitation = (
            0.34 * state.novelty
            + 0.16 * state.data_novelty
            + 0.18 / (1.0 + state.target_distance)
            + 0.12 * min(1.0, state.pending_branches / 8.0)
            + 0.06 / (1.0 + math.log1p(state.depth))
        )
        observed = stats.reward / stats.pulls if stats.pulls else 0.0
        cost = stats.cost / stats.pulls if stats.pulls else max(1.0, state.constraint_cost)
        efficiency = observed / math.sqrt(max(0.05, cost))
        uncertainty = self.exploration * math.sqrt(
            math.log1p(total) / max(1, stats.pulls))
        failure_penalty = min(0.25, stats.failures / max(1.0, stats.pulls + 1.0))
        return exploitation + efficiency + uncertainty - failure_penalty

    def select(self, limit: int, *, worker: int | None = None) -> list[LiveState]:
        if isinstance(limit, bool) or not 1 <= int(limit) <= 256:
            raise ValueError("limit must be in 1..256")
        with self._lock:
            candidates = [
                state for state in self._states.values()
                if worker is None or state.worker == worker
            ]
            ranked = sorted(candidates, key=lambda state: (-self.score(state), state.state_id))
            selected = ranked[:int(limit)]
            for state in selected:
                self._stats[state.state_id].pulls += 1
            self._total_pulls += len(selected)
            return list(selected)

    def observe(self, state_id: str, *, reward: float, elapsed: float, killed: bool = False) -> None:
        reward = max(0.0, min(1.0, _finite(reward, "reward")))
        elapsed = _nonnegative(elapsed, "elapsed")
        with self._lock:
            stats = self._stats.setdefault(state_id, _Stats())
            # A pull is counted at selection time; observe only adds its result.
            stats.reward += 0.0 if killed else reward
            stats.cost += elapsed
            stats.failures += int(killed)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schema": "symcc-live-state-scheduler-v1",
                "states": len(self._states),
                "total_pulls": self._total_pulls,
                "stats": {
                    key: {"pulls": value.pulls, "reward": value.reward,
                          "cost": value.cost, "failures": value.failures}
                    for key, value in self._stats.items()
                },
            }
