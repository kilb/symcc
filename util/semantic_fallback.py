#!/usr/bin/env python3
"""Semantic constraint summaries for solver fallback routing.

This is a scheduling-layer abstraction over telemetry rather than a replacement
for QSYM/Z3.  It classifies branch constraints by observable semantics
(byte-local, token-progress, wide/nonlinear, timeout-prone) and emits the same
bounded hints used by S2F/actionseed and executor portfolio routing.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
import tempfile
from typing import Any, Iterable


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _path_key(path: str, sha256: str = "") -> str:
    digest = str(sha256 or "").lower()
    if len(digest) == 64 and all(ch in "0123456789abcdef" for ch in digest):
        return digest
    return "path:" + hashlib.sha256(
        os.path.abspath(path).encode("utf-8", errors="surrogateescape")
    ).hexdigest()


@dataclass
class SemanticBranchState:
    branch: int
    site: int = 0
    kind: str = "opaque"
    lo: int = 0
    hi: int = -1
    observations: int = 0
    successes: int = 0
    failures: int = 0
    timeouts: int = 0
    reward_ema: float = 0.0
    cost_ema: float = 0.0
    data_progress: float = 0.0
    locality: float = 0.0
    last_seen: int = 0
    last_action: str = ""

    @property
    def span(self) -> int:
        return max(0, self.hi - self.lo + 1)

    def action(self) -> str:
        if (self.failures >= 3 and self.reward_ema < 0.04
                and (self.timeouts > 0 or self.kind == "timeout")):
            return "skip"
        if self.kind in {"wide_nonlinear", "timeout"}:
            return "sample"
        if self.kind in {"byte_local", "token_progress"}:
            return "solve"
        if self.locality >= 0.55 and self.span <= 32:
            return "solve"
        if self.cost_ema > 1.0 or self.timeouts:
            return "sample"
        return "solve"

    def priority(self) -> float:
        action_bonus = {"sample": 0.12, "solve": 0.08, "skip": -0.25}.get(
            self.action(), 0.0)
        novelty = 1.0 / math.sqrt(max(1, self.observations))
        return (
            0.42 * self.reward_ema
            + 0.20 * self.locality
            + 0.16 * self.data_progress
            + 0.12 * novelty
            + action_bonus
            - 0.10 * min(1.0, self.timeouts / max(1, self.observations))
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "site": self.site,
            "kind": self.kind,
            "lo": self.lo,
            "hi": self.hi,
            "observations": self.observations,
            "successes": self.successes,
            "failures": self.failures,
            "timeouts": self.timeouts,
            "reward_ema": self.reward_ema,
            "cost_ema": self.cost_ema,
            "data_progress": self.data_progress,
            "locality": self.locality,
            "last_seen": self.last_seen,
            "last_action": self.last_action,
        }

    @classmethod
    def from_mapping(cls, raw: Any) -> "SemanticBranchState | None":
        if not isinstance(raw, dict):
            return None
        branch = _nonnegative_int(raw.get("branch"))
        if branch <= 0:
            return None
        state = cls(branch=branch)
        state.site = _nonnegative_int(raw.get("site"))
        kind = str(raw.get("kind", "opaque"))
        state.kind = kind if kind in {
            "byte_local", "token_progress", "wide_nonlinear",
            "timeout", "opaque",
        } else "opaque"
        state.lo = _nonnegative_int(raw.get("lo"))
        state.hi = int(raw.get("hi", -1)) if isinstance(
            raw.get("hi", -1), int) else -1
        state.observations = _nonnegative_int(raw.get("observations"))
        state.successes = _nonnegative_int(raw.get("successes"))
        state.failures = _nonnegative_int(raw.get("failures"))
        state.timeouts = _nonnegative_int(raw.get("timeouts"))
        state.reward_ema = _clamp01(raw.get("reward_ema", 0.0))
        state.cost_ema = max(0.0, float(raw.get("cost_ema", 0.0) or 0.0))
        state.data_progress = _clamp01(raw.get("data_progress", 0.0))
        state.locality = _clamp01(raw.get("locality", 0.0))
        state.last_seen = _nonnegative_int(raw.get("last_seen"))
        state.last_action = str(raw.get("last_action", ""))[:16]
        return state


class SemanticFallbackPlanner:
    """Learn semantic branch classes and produce bounded fallback hints."""

    def __init__(
        self,
        state_path: str | None,
        *,
        strategy_count: int,
        action_cap: int = 8,
        exact_bytes: int = 16,
        focus_span: int = 128,
        max_branches: int = 32768,
    ) -> None:
        self.state_path = state_path
        self.strategy_count = max(1, int(strategy_count))
        self.action_cap = max(1, int(action_cap))
        self.exact_bytes = max(1, int(exact_bytes))
        self.focus_span = max(1, int(focus_span))
        self.max_branches = max(128, int(max_branches))
        self.branches: dict[int, SemanticBranchState] = {}
        self.path_branches: dict[str, set[int]] = {}
        self.clock = 0
        self.observations = 0
        if state_path:
            self._load()

    @staticmethod
    def _strategy_for_action(action: str, strategy_count: int) -> int:
        if action == "sample":
            if strategy_count > 6:
                return 6
            if strategy_count > 3:
                return 3
        if action == "solve":
            return 0
        return 0

    def _classify(
        self,
        *,
        span: int,
        count: int,
        interesting: int,
        data_progress: float,
        timeout_pressure: float,
        difficulty: float,
    ) -> str:
        density = min(1.0, count / max(1, span))
        if timeout_pressure >= 0.35 and (span > self.exact_bytes or difficulty >= 0.55):
            return "timeout"
        if span <= self.exact_bytes and density >= 0.45:
            return "byte_local"
        if 0.0 < data_progress < 0.98 and (interesting or density >= 0.20):
            return "token_progress"
        if span > max(self.exact_bytes * 2, 32) or difficulty >= 0.65:
            return "wide_nonlinear"
        return "opaque"

    def _update_state(
        self,
        branch: int,
        *,
        site: int,
        kind: str,
        lo: int,
        hi: int,
        locality: float,
        data_progress: float,
        reward: float,
        elapsed: float,
        killed: bool,
        timed_out: bool,
    ) -> None:
        if branch <= 0:
            return
        state = self.branches.get(branch)
        if state is None:
            state = SemanticBranchState(branch=branch)
            self.branches[branch] = state
        state.site = site or state.site
        state.kind = kind
        if hi >= lo:
            state.lo, state.hi = lo, hi
        state.locality = _clamp01(0.75 * state.locality + 0.25 * locality)
        state.data_progress = _clamp01(
            max(data_progress, 0.70 * state.data_progress))
        state.observations += 1
        state.last_seen = self.clock
        state.last_action = state.action()
        state.reward_ema = (
            reward if state.observations == 1
            else 0.82 * state.reward_ema + 0.18 * reward)
        state.cost_ema = (
            max(0.001, elapsed) if state.observations == 1
            else 0.82 * state.cost_ema + 0.18 * max(0.001, elapsed))
        if reward > 0.05:
            state.successes += 1
        if killed or reward <= 0.0:
            state.failures += 1
        if timed_out:
            state.timeouts += 1

    def observe(
        self,
        path: str,
        telemetry: Any,
        *,
        sha256: str = "",
        coverage_delta: int = 0,
        interesting_cases: int = 0,
        elapsed: float = 0.0,
        killed: bool = False,
    ) -> None:
        if telemetry is None:
            return
        self.clock += 1
        self.observations += 1
        key = _path_key(path, sha256)
        reward = _clamp01(
            0.48 * (1.0 - math.exp(-max(0, coverage_delta) / 3.0))
            + 0.32 * (1.0 - math.exp(-max(0, interesting_cases) / 2.0))
            + 0.20 * (1.0 - math.exp(-max(0, getattr(telemetry, "generated", 0)) / 16.0))
        )
        difficulty = _clamp01(getattr(telemetry, "difficulty", 0.0))
        timeout_pressure = max(
            _clamp01(getattr(telemetry, "timeout_ratio", 0.0)),
            1.0 if _nonnegative_int(getattr(telemetry, "z3_timeouts", 0)) else 0.0,
        )
        data_by_site: dict[int, float] = {}
        for site, matched, width in getattr(telemetry, "data_features", ()) or ():
            if width:
                data_by_site[_nonnegative_int(site)] = max(
                    data_by_site.get(_nonnegative_int(site), 0.0),
                    _clamp01(matched / max(1, width)),
                )

        path_set = self.path_branches.setdefault(key, set())
        seen: set[int] = set()
        for site, branch, count, lo, hi, _taken, interesting in (
                getattr(telemetry, "comparison_taints", ()) or ()):
            site = _nonnegative_int(site)
            branch = _nonnegative_int(branch)
            lo = _nonnegative_int(lo)
            hi = max(lo, _nonnegative_int(hi))
            span = hi - lo + 1
            count = max(1, _nonnegative_int(count))
            density = min(1.0, count / max(1, span))
            data_progress = data_by_site.get(site, 0.0)
            kind = self._classify(
                span=span,
                count=count,
                interesting=_nonnegative_int(interesting),
                data_progress=data_progress,
                timeout_pressure=timeout_pressure,
                difficulty=difficulty,
            )
            self._update_state(
                branch,
                site=site,
                kind=kind,
                lo=lo,
                hi=hi,
                locality=density,
                data_progress=data_progress,
                reward=reward,
                elapsed=elapsed,
                killed=killed,
                timed_out=timeout_pressure > 0.0,
            )
            path_set.add(branch)
            seen.add(branch)

        for _parent, _actual, open_branch, site, _taken, interesting in (
                getattr(telemetry, "branch_trace", ()) or ()):
            branch = _nonnegative_int(open_branch)
            if branch <= 0 or branch in seen:
                continue
            site = _nonnegative_int(site)
            kind = "timeout" if timeout_pressure >= 0.35 else "opaque"
            self._update_state(
                branch,
                site=site,
                kind=kind,
                lo=0,
                hi=-1,
                locality=0.0,
                data_progress=data_by_site.get(site, 0.0),
                reward=reward * (0.5 + 0.5 * int(bool(interesting))),
                elapsed=elapsed,
                killed=killed,
                timed_out=timeout_pressure > 0.0,
            )
            path_set.add(branch)

        self._evict_if_needed()

    def _evict_if_needed(self) -> None:
        if len(self.branches) <= self.max_branches:
            return
        keep = {
            branch for branch, _state in sorted(
                self.branches.items(),
                key=lambda item: item[1].last_seen,
                reverse=True,
            )[:self.max_branches]
        }
        self.branches = {
            branch: state for branch, state in self.branches.items()
            if branch in keep
        }
        for key, branches in list(self.path_branches.items()):
            branches.intersection_update(keep)
            if not branches:
                self.path_branches.pop(key, None)

    def _candidate_states(
        self,
        path: str,
        sha256: str,
        target_branch: int,
    ) -> list[SemanticBranchState]:
        if target_branch > 0:
            state = self.branches.get(target_branch)
            return [state] if state is not None else []
        key = _path_key(path, sha256)
        branches = self.path_branches.get(key, set())
        states = [self.branches[branch] for branch in branches
                  if branch in self.branches]
        if states:
            return states
        return sorted(
            self.branches.values(),
            key=lambda state: state.priority(),
            reverse=True,
        )[:self.action_cap]

    def suggest(
        self,
        path: str,
        *,
        sha256: str = "",
        target_branch: int = 0,
    ) -> dict[str, Any]:
        states = sorted(
            self._candidate_states(path, sha256, target_branch),
            key=lambda state: state.priority(),
            reverse=True,
        )
        actions: list[list[Any]] = []
        focus = ""
        route = ""
        for state in states:
            action = state.action()
            if action not in {"solve", "sample", "skip"}:
                continue
            actions.append([state.branch, action])
            if not route:
                route = "semantic-" + state.kind.replace("_", "-")
            if (not focus and action != "skip"
                    and state.span > 0 and state.span <= self.focus_span):
                focus = f"{max(0, state.lo - 2)}-{state.hi + 2}"
            if len(actions) >= self.action_cap:
                break
        if not actions:
            return {}
        first_action = str(actions[0][1])
        hint: dict[str, Any] = {
            "strategy": self._strategy_for_action(
                first_action, self.strategy_count),
            "s2f_actions": actions,
            "route": route or "semantic",
        }
        if focus:
            hint["focus_bytes"] = focus
        if target_branch > 0:
            hint["target_branch"] = target_branch
        return hint

    def snapshot(self) -> dict[str, Any]:
        kinds: dict[str, int] = {}
        actions: dict[str, int] = {}
        for state in self.branches.values():
            kinds[state.kind] = kinds.get(state.kind, 0) + 1
            action = state.action()
            actions[action] = actions.get(action, 0) + 1
        return {
            "branches": len(self.branches),
            "paths": len(self.path_branches),
            "observations": self.observations,
            "kinds": kinds,
            "actions": actions,
        }

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "clock": self.clock,
            "observations": self.observations,
            "branches": [
                state.to_mapping()
                for state in sorted(
                    self.branches.values(),
                    key=lambda item: item.last_seen,
                    reverse=True,
                )[:self.max_branches]
            ],
            "paths": {
                key: sorted(branches)[-512:]
                for key, branches in list(self.path_branches.items())[-16384:]
            },
        }

    def _load(self) -> None:
        try:
            with open(str(self.state_path), encoding="utf-8") as stream:
                raw = json.load(stream)
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(raw, dict):
            return
        self.clock = _nonnegative_int(raw.get("clock"))
        self.observations = _nonnegative_int(raw.get("observations"))
        for item in raw.get("branches", ())[-self.max_branches:]:
            state = SemanticBranchState.from_mapping(item)
            if state is not None:
                self.branches[state.branch] = state
        paths = raw.get("paths", {})
        if isinstance(paths, dict):
            for key, values in list(paths.items())[-16384:]:
                if (not isinstance(key, str)
                        or isinstance(values, (str, bytes))
                        or not isinstance(values, Iterable)):
                    continue
                branches = {
                    branch for branch in (_nonnegative_int(value) for value in values)
                    if branch in self.branches
                }
                if branches:
                    self.path_branches[key] = branches

    def save(self) -> None:
        if not self.state_path:
            return
        directory = os.path.dirname(str(self.state_path)) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=os.path.basename(str(self.state_path)) + ".",
            suffix=".tmp",
            dir=directory,
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(
                    self.to_mapping(), stream,
                    sort_keys=True, separators=(",", ":"))
            os.replace(tmp, str(self.state_path))
        except (OSError, TypeError, ValueError):
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(tmp)
            except OSError:
                pass
