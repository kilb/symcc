"""Bounded, persistent TopSeed-style campaign seed selection.

The selector is deliberately separate from live-state search.  TopSeed chooses
an input between symbolic-execution runs; it does not rank states within one
run.  Execution and coverage remain external correctness oracles.  This module
only admits immutable observations, proposes seeds, and learns from completed
runs.
"""

from __future__ import annotations

from collections import Counter, OrderedDict
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Iterable, Mapping, Sequence


TOPSEED_SNAPSHOT_SCHEMA = "symcc-topseed-selector-snapshot-v1"
TOPSEED_POLICIES = ("unique", "long", "short", "random")
_MAX_U63 = (1 << 63) - 1
_MASK_U64 = (1 << 64) - 1
_MAX_PATH_BYTES = 4096


def _bounded_integer(
    value: Any,
    name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or (
        isinstance(value, float) and not value.is_integer()
    ):
        raise ValueError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{name} must be an integer") from error
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} is outside {minimum}..{maximum}")
    return parsed


def _finite(value: Any, name: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be finite")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{name} must be finite") from error
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        raise ValueError(f"{name} is outside {minimum}..{maximum}")
    return parsed


def _identity(value: Any, name: str) -> str:
    candidate = str(value).lower()
    if len(candidate) != 64 or any(
        character not in "0123456789abcdef" for character in candidate
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 identity")
    return candidate


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("TopSeed candidate path is invalid")
    if len(value.encode("utf-8")) > _MAX_PATH_BYTES:
        raise ValueError("TopSeed candidate path exceeds its byte budget")
    return value


def _features(
    values: Iterable[Any],
    name: str,
    *,
    maximum: int,
) -> tuple[int, ...]:
    result: set[int] = set()
    for raw in values:
        result.add(_bounded_integer(
            raw, name, minimum=0, maximum=_MAX_U63,
        ))
        if len(result) > maximum:
            raise ValueError(f"{name} exceeds its cardinality budget")
    return tuple(sorted(result))


class _StableRandom:
    """Portable SplitMix64 stream used by proposal and snapshot oracles."""

    def __init__(self, seed: int) -> None:
        self.state = int(seed) & _MASK_U64
        self.draws = 0

    def _next(self) -> int:
        if self.draws >= _MAX_U63:
            raise ValueError("TopSeed random stream is exhausted")
        self.state = (self.state + 0x9E3779B97F4A7C15) & _MASK_U64
        value = self.state
        value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & _MASK_U64
        value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & _MASK_U64
        self.draws += 1
        return (value ^ (value >> 31)) & _MASK_U64

    def random(self) -> float:
        return (self._next() >> 11) * (1.0 / (1 << 53))

    def randrange(self, stop: int) -> int:
        if isinstance(stop, bool) or not isinstance(stop, int) or stop <= 0:
            raise ValueError("TopSeed random range must be positive")
        limit = (1 << 64) - ((1 << 64) % stop)
        while True:
            value = self._next()
            if value < limit:
                return value % stop

    def uniform(self, lower: float, upper: float) -> float:
        return lower + (upper - lower) * self.random()

    def snapshot(self) -> dict[str, int | str]:
        return {
            "algorithm": "splitmix64-v1",
            "state": self.state,
            "draws": self.draws,
        }

    @classmethod
    def from_snapshot(cls, raw: Any) -> "_StableRandom":
        if not isinstance(raw, Mapping) or set(raw) != {
            "algorithm", "state", "draws",
        } or raw.get("algorithm") != "splitmix64-v1":
            raise ValueError("TopSeed random snapshot is invalid")
        result = cls(0)
        result.state = _bounded_integer(
            raw.get("state"), "TopSeed random state",
            minimum=0, maximum=_MASK_U64,
        )
        result.draws = _bounded_integer(
            raw.get("draws"), "TopSeed random draws",
            minimum=0, maximum=_MAX_U63,
        )
        return result


@dataclass
class _Candidate:
    identity: str
    path: str
    coverage: tuple[int, ...]
    path_condition: tuple[int, ...] = ()
    triggers_bug: bool = False
    uses: int = 0
    generated_coverage: tuple[int, ...] = ()


@dataclass
class _Run:
    token: str
    candidate: str
    mode: str
    policy: str
    weights: tuple[float, ...]
    status: str = "pending"
    generated_coverage: tuple[int, ...] = ()
    failed: bool = False
    triggers_bug: bool = False


@dataclass(frozen=True)
class TopSeedProposal:
    """One untrusted seed proposal; commit only after dispatch succeeds."""

    token: str
    candidate_id: str
    path: str
    mode: str
    policy: str
    weights: tuple[float, ...]
    group: str


class TopSeedSelector:
    """Learn promising cross-run seeds from bounded campaign observations."""

    def __init__(
        self,
        *,
        program_context: str,
        seed: int = 0,
        explore_ratio: float = 0.75,
        learn_interval: int = 20,
        max_candidates: int = 65_536,
        max_runs: int = 262_144,
        max_features: int = 65_536,
    ) -> None:
        self.program_context = _identity(program_context, "TopSeed program context")
        self.seed = _bounded_integer(
            seed, "TopSeed seed", minimum=0, maximum=_MASK_U64,
        )
        self.explore_ratio = _finite(
            explore_ratio, "TopSeed explore ratio", minimum=0.0, maximum=1.0,
        )
        self.learn_interval = _bounded_integer(
            learn_interval, "TopSeed learn interval", minimum=1, maximum=1_000_000,
        )
        self.max_candidates = _bounded_integer(
            max_candidates, "TopSeed candidate limit", minimum=2, maximum=1_000_000,
        )
        self.max_runs = _bounded_integer(
            max_runs, "TopSeed run limit", minimum=2, maximum=1_000_000,
        )
        self.max_features = _bounded_integer(
            max_features, "TopSeed feature limit", minimum=1, maximum=1_000_000,
        )
        self.random = _StableRandom(self.seed)
        self.candidates: OrderedDict[str, _Candidate] = OrderedDict()
        self.runs: OrderedDict[str, _Run] = OrderedDict()
        self.used_groups: set[str] = set()
        self.pending: dict[str, TopSeedProposal] = {}
        self.weight_distributions: list[tuple[str, float, float]] = [
            ("uniform", 0.0, 0.0) for _ in range(5)
        ]
        self.policy_probabilities = [0.25, 0.25, 0.25, 0.25]
        self.selection_serial = 0
        self.selections = 0
        self.explore_selections = 0
        self.exploit_selections = 0
        self.completed_since_learning = 0
        self.learning_rounds = 0
        self.admission_conflicts = 0
        self.dropped_observations = 0
        self.evicted_candidates = 0
        self.evicted_runs = 0

    @staticmethod
    def coverage_features_from_bitmap(
        bitmap: bytes | bytearray | Sequence[tuple[int, int]],
        *,
        maximum: int = 65_536,
    ) -> tuple[int, ...]:
        """Encode AFL map bucket bits as stable, non-collapsing features."""
        maximum = _bounded_integer(
            maximum, "TopSeed bitmap feature limit", minimum=1, maximum=1_000_000,
        )
        result: list[int] = []
        if isinstance(bitmap, (bytes, bytearray)):
            entries = enumerate(bitmap)
        else:
            entries = bitmap
        seen: set[int] = set()
        for raw_index, raw_value in entries:
            index = _bounded_integer(
                raw_index, "TopSeed bitmap index", minimum=0,
                maximum=(_MAX_U63 >> 3),
            )
            value = _bounded_integer(
                raw_value, "TopSeed bitmap byte", minimum=0, maximum=255,
            )
            for bit in range(8):
                if value & (1 << bit):
                    feature = (index << 3) | bit
                    if feature not in seen:
                        seen.add(feature)
                        result.append(feature)
                        if len(result) >= maximum:
                            return tuple(sorted(result))
        return tuple(sorted(result))

    @staticmethod
    def path_condition_from_branch_trace(
        trace: Iterable[Sequence[Any]],
        *,
        maximum: int = 65_536,
    ) -> tuple[int, ...]:
        """Build stable site/outcome tokens from accepted solver telemetry."""
        maximum = _bounded_integer(
            maximum, "TopSeed path-condition limit", minimum=1, maximum=1_000_000,
        )
        result: set[int] = set()
        for entry in trace:
            if len(entry) < 5:
                continue
            try:
                site = _bounded_integer(
                    entry[3], "TopSeed branch site", minimum=0,
                    maximum=_MASK_U64,
                )
            except ValueError:
                continue
            if isinstance(entry[4], bool):
                taken = entry[4]
            elif isinstance(entry[4], int) and entry[4] in {0, 1}:
                taken = bool(entry[4])
            else:
                continue
            if site <= (_MAX_U63 >> 1):
                token = (site << 1) | int(taken)
            else:
                token = int.from_bytes(hashlib.sha256(
                    b"symcc-topseed-branch-outcome-v1\0"
                    + site.to_bytes(8, "big")
                    + bytes([int(taken)])
                ).digest()[:8], "big") & _MAX_U63
            result.add(token)
            if len(result) >= maximum:
                break
        return tuple(sorted(result))

    @staticmethod
    def _group_key(coverage: Sequence[int]) -> str:
        digest = hashlib.sha256()
        digest.update(b"symcc-topseed-coverage-group-v1\0")
        for feature in coverage:
            digest.update(int(feature).to_bytes(8, "big"))
        return digest.hexdigest()

    def _evict_candidate(self) -> bool:
        victim = next((
            candidate_id
            for candidate_id, candidate in self.candidates.items()
            if candidate.uses == 0 and all(
                proposal.candidate_id != candidate_id
                for proposal in self.pending.values()
            )
        ), None)
        if victim is not None:
            self.candidates.pop(victim)
            self.evicted_candidates += 1
            return True
        return False

    def admit(
        self,
        candidate_id: str,
        path: str,
        coverage: Iterable[Any],
        *,
        path_condition: Iterable[Any] = (),
        triggers_bug: bool = False,
    ) -> bool:
        """Admit one immutable input and its concretely measured coverage."""
        identity = _identity(candidate_id, "TopSeed candidate identity")
        normalized_path = _path(path)
        normalized_coverage = _features(
            coverage, "TopSeed coverage feature", maximum=self.max_features,
        )
        if not normalized_coverage:
            self.dropped_observations += 1
            return False
        normalized_condition = _features(
            path_condition, "TopSeed path-condition feature",
            maximum=self.max_features,
        )
        existing = self.candidates.get(identity)
        bug = _boolean(triggers_bug, "TopSeed candidate bug flag")
        if existing is not None:
            if existing.coverage != normalized_coverage:
                self.admission_conflicts += 1
                return False
            existing.path = normalized_path
            if normalized_condition:
                existing.path_condition = normalized_condition
            existing.triggers_bug |= bug
            self.candidates.move_to_end(identity)
            return True
        while len(self.candidates) >= self.max_candidates:
            if not self._evict_candidate():
                self.dropped_observations += 1
                return False
        self.candidates[identity] = _Candidate(
            identity,
            normalized_path,
            normalized_coverage,
            normalized_condition,
            bug,
        )
        return True

    def historical_paths(self, *, limit: int = 4096) -> tuple[str, ...]:
        limit = _bounded_integer(
            limit, "TopSeed historical path limit", minimum=1, maximum=65_536,
        )
        selected: list[str] = []
        for candidate in reversed(self.candidates.values()):
            if candidate.uses > 0:
                selected.append(candidate.path)
                if len(selected) >= limit:
                    break
        return tuple(selected)

    def _sample_weight(self) -> tuple[float, ...]:
        result: list[float] = []
        for kind, mean, deviation in self.weight_distributions:
            if kind == "uniform":
                result.append(self.random.uniform(-1.0, 1.0))
                continue
            sample = mean
            for _attempt in range(64):
                first = max(self.random.random(), 1.0 / (1 << 53))
                second = self.random.random()
                normal = math.sqrt(-2.0 * math.log(first)) * math.cos(
                    2.0 * math.pi * second
                )
                sample = mean + deviation * normal
                if -1.0 <= sample <= 1.0:
                    break
            result.append(max(-1.0, min(1.0, sample)))
        return tuple(result)

    def _sample_policy(self) -> str:
        selected = self.random.random()
        cumulative = 0.0
        for policy, probability in zip(
            TOPSEED_POLICIES, self.policy_probabilities
        ):
            cumulative += probability
            if selected < cumulative:
                return policy
        return TOPSEED_POLICIES[-1]

    def _choose_candidate(
        self,
        candidate_ids: Sequence[str],
        policy: str,
    ) -> str:
        ordered = sorted(candidate_ids)
        if not ordered:
            raise ValueError("TopSeed cannot choose from an empty candidate set")
        if policy == "random":
            return ordered[self.random.randrange(len(ordered))]
        if policy == "long":
            return min(
                ordered,
                key=lambda candidate: (
                    -len(self.candidates[candidate].path_condition), candidate,
                ),
            )
        if policy == "short":
            return min(
                ordered,
                key=lambda candidate: (
                    len(self.candidates[candidate].path_condition), candidate,
                ),
            )
        if policy != "unique":
            raise ValueError("TopSeed policy is invalid")
        conditions = {
            candidate: set(self.candidates[candidate].path_condition)
            for candidate in ordered
        }
        return min(
            ordered,
            key=lambda candidate: (
                -len(conditions[candidate] - set().union(*(
                    conditions[other]
                    for other in ordered
                    if other != candidate
                )) if len(ordered) > 1 else conditions[candidate]),
                candidate,
            ),
        )

    @staticmethod
    def _high_cluster(scores: Sequence[float]) -> tuple[int, ...]:
        if not scores:
            return ()
        if len(scores) == 1 or math.isclose(min(scores), max(scores)):
            return tuple(range(len(scores)))
        ranked = sorted(range(len(scores)), key=lambda index: (scores[index], index))
        prefix = [0.0]
        prefix_squares = [0.0]
        for index in ranked:
            value = scores[index]
            prefix.append(prefix[-1] + value)
            prefix_squares.append(prefix_squares[-1] + value * value)

        def squared_error(lower: int, upper: int) -> float:
            count = upper - lower
            total = prefix[upper] - prefix[lower]
            squares = prefix_squares[upper] - prefix_squares[lower]
            return max(0.0, squares - total * total / count)

        best = min(
            range(1, len(ranked)),
            key=lambda cut: (
                squared_error(0, cut)
                + squared_error(cut, len(ranked)),
                cut,
            ),
        )
        return tuple(sorted(ranked[best:]))

    @staticmethod
    def _rarity_scores(coverage_sets: Sequence[set[int]]) -> list[float]:
        frequencies: Counter[int] = Counter()
        for coverage in coverage_sets:
            frequencies.update(coverage)
        return [
            math.fsum(1.0 / frequencies[feature] for feature in coverage)
            for coverage in coverage_sets
        ]

    def _explore_candidate(
        self,
        available: set[str],
        weights: tuple[float, ...],
        policy: str,
    ) -> tuple[str, str] | None:
        groups: dict[str, list[str]] = {}
        coverages: dict[str, tuple[int, ...]] = {}
        for candidate_id in available:
            candidate = self.candidates[candidate_id]
            group = self._group_key(candidate.coverage)
            if group in self.used_groups:
                continue
            groups.setdefault(group, []).append(candidate_id)
            coverages[group] = candidate.coverage
        if not groups:
            return None

        branch_frequency: Counter[int] = Counter()
        for candidate in self.candidates.values():
            branch_frequency.update(candidate.coverage)
        previously_selected: set[int] = set()
        for candidate in self.candidates.values():
            if self._group_key(candidate.coverage) in self.used_groups:
                previously_selected.update(candidate.coverage)

        def score(group: str) -> float:
            coverage = set(coverages[group])
            members = groups[group]
            features = (
                float(len(coverage)),
                math.fsum(
                    1.0 / branch_frequency[branch] for branch in coverage
                ),
                float(len(coverage - previously_selected)),
                float(sum(
                    int(self.candidates[candidate].triggers_bug)
                    for candidate in members
                )),
                float(len(members)),
            )
            return math.fsum(
                feature * weight for feature, weight in zip(features, weights)
            )

        best_group = min(groups, key=lambda group: (-score(group), group))
        return self._choose_candidate(groups[best_group], policy), best_group

    def _exploit_candidate(
        self,
        available: set[str],
        policy: str,
    ) -> tuple[str, str] | None:
        candidates = sorted(
            candidate_id for candidate_id in available
            if self.candidates[candidate_id].uses > 0
            and self.candidates[candidate_id].generated_coverage
        )
        if not candidates:
            return None
        coverage_sets = [
            set(self.candidates[candidate].generated_coverage)
            for candidate in candidates
        ]
        scores = self._rarity_scores(coverage_sets)
        good = [candidates[index] for index in self._high_cluster(scores)]
        selected = self._choose_candidate(good, policy)
        return selected, self._group_key(
            self.candidates[selected].coverage
        )

    def propose(self, paths: Iterable[str]) -> TopSeedProposal | None:
        """Propose one available seed without changing learned history."""
        if len(self.pending) >= min(self.max_runs, 4096):
            self.dropped_observations += 1
            return None
        if len(self.runs) >= self.max_runs and not any(
            run.status != "pending" for run in self.runs.values()
        ):
            self.dropped_observations += 1
            return None
        available_paths = {_path(path) for path in paths}
        available = {
            candidate_id for candidate_id, candidate in self.candidates.items()
            if candidate.path in available_paths
        }
        if not available:
            return None
        weights = self._sample_weight()
        policy = self._sample_policy()
        prefer_explore = self.random.random() < self.explore_ratio
        selected: tuple[str, str] | None
        if prefer_explore:
            mode = "explore"
            selected = self._explore_candidate(available, weights, policy)
            if selected is None:
                mode = "exploit"
                selected = self._exploit_candidate(available, policy)
        else:
            mode = "exploit"
            selected = self._exploit_candidate(available, policy)
            if selected is None:
                mode = "explore"
                selected = self._explore_candidate(available, weights, policy)
        if selected is None:
            return None
        candidate_id, group = selected
        self.selection_serial += 1
        if self.selection_serial > _MAX_U63:
            raise ValueError("TopSeed selection serial is exhausted")
        token_payload = json.dumps(
            {
                "candidate": candidate_id,
                "group": group,
                "mode": mode,
                "policy": policy,
                "serial": self.selection_serial,
                "weights": list(weights),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        token = hashlib.sha256(
            b"symcc-topseed-proposal-v1\0" + token_payload
        ).hexdigest()
        proposal = TopSeedProposal(
            token,
            candidate_id,
            self.candidates[candidate_id].path,
            mode,
            policy,
            weights,
            group,
        )
        self.pending[token] = proposal
        return proposal

    def discard(self, token: str) -> bool:
        return self.pending.pop(str(token), None) is not None

    def _evict_run(self) -> bool:
        victim = next((
            token for token, run in self.runs.items()
            if run.status != "pending"
        ), None)
        if victim is not None:
            self.runs.pop(victim)
            self.evicted_runs += 1
            return True
        return False

    def commit(self, token: str) -> str:
        """Commit a proposal after its worker dispatch is externally durable."""
        proposal = self.pending.get(str(token))
        if proposal is None:
            raise ValueError("TopSeed proposal is missing or already committed")
        while len(self.runs) >= self.max_runs:
            if not self._evict_run():
                raise ValueError("TopSeed run history has no evictable entry")
        candidate = self.candidates.get(proposal.candidate_id)
        if candidate is None or candidate.path != proposal.path:
            raise ValueError("TopSeed proposal candidate changed before commit")
        self.pending.pop(proposal.token)
        candidate.uses += 1
        self.used_groups.add(proposal.group)
        self.runs[proposal.token] = _Run(
            proposal.token,
            proposal.candidate_id,
            proposal.mode,
            proposal.policy,
            proposal.weights,
        )
        self.selections += 1
        if proposal.mode == "explore":
            self.explore_selections += 1
        else:
            self.exploit_selections += 1
        return proposal.token

    def observe(
        self,
        token: str,
        generated_coverage: Iterable[Any],
        *,
        path_condition: Iterable[Any] = (),
        triggers_bug: bool = False,
        failed: bool = False,
    ) -> bool:
        """Admit one fenced run result and periodically update distributions."""
        run = self.runs.get(str(token))
        if run is None or run.status != "pending":
            self.dropped_observations += 1
            return False
        coverage = _features(
            generated_coverage, "TopSeed generated coverage feature",
            maximum=self.max_features,
        )
        condition = _features(
            path_condition, "TopSeed path-condition feature",
            maximum=self.max_features,
        )
        candidate = self.candidates.get(run.candidate)
        if candidate is None:
            self.dropped_observations += 1
            return False
        bug = _boolean(triggers_bug, "TopSeed run bug flag")
        run_failed = _boolean(failed, "TopSeed run failure flag")
        run.status = "completed"
        run.generated_coverage = coverage
        run.failed = run_failed
        run.triggers_bug = bug
        if coverage:
            candidate.generated_coverage = tuple(sorted(
                set(candidate.generated_coverage) | set(coverage)
            ))
            if len(candidate.generated_coverage) > self.max_features:
                candidate.generated_coverage = candidate.generated_coverage[
                    :self.max_features
                ]
        if condition:
            candidate.path_condition = condition
        candidate.triggers_bug |= bug
        self.completed_since_learning += 1
        if self.completed_since_learning >= self.learn_interval:
            self._learn()
            self.completed_since_learning = 0
        return True

    def fail_pending_runs(self) -> int:
        """Retire results that cannot be re-associated after coordinator loss."""
        pending = [
            run.token for run in self.runs.values()
            if run.status == "pending"
        ]
        for token in pending:
            self.observe(token, (), failed=True)
        return len(pending)

    def _learn(self) -> None:
        completed = [run for run in self.runs.values() if run.status == "completed"]
        if len(completed) < 2:
            return
        coverages = [set(run.generated_coverage) for run in completed]
        scores = self._rarity_scores(coverages)
        top_indices = set(self._high_cluster(scores))
        bottom_indices = set(range(len(completed))) - top_indices
        if bottom_indices:
            distributions: list[tuple[str, float, float]] = []
            for feature in range(5):
                top = [completed[index].weights[feature] for index in top_indices]
                bottom = [
                    completed[index].weights[feature] for index in bottom_indices
                ]
                top_mean = math.fsum(top) / len(top)
                bottom_mean = math.fsum(bottom) / len(bottom)
                top_std = math.sqrt(math.fsum(
                    (value - top_mean) ** 2 for value in top
                ) / len(top))
                bottom_std = math.sqrt(math.fsum(
                    (value - bottom_mean) ** 2 for value in bottom
                ) / len(bottom))
                if abs(top_mean - bottom_mean) + abs(top_std - bottom_std) > 0.1:
                    distributions.append((
                        "truncated-normal", top_mean,
                        top_std if top_std > 0.0 else 1.0,
                    ))
                else:
                    distributions.append(("uniform", 0.0, 0.0))
            self.weight_distributions = distributions

        policy_coverage = [set() for _ in TOPSEED_POLICIES]
        for run in completed:
            policy_coverage[TOPSEED_POLICIES.index(run.policy)].update(
                run.generated_coverage
            )
        if all(policy_coverage):
            policy_scores = self._rarity_scores(policy_coverage)
            total = math.fsum(policy_scores)
            self.policy_probabilities = (
                [score / total for score in policy_scores]
                if total > 0.0
                else [0.25, 0.25, 0.25, 0.25]
            )
        self.learning_rounds += 1

    def telemetry(self) -> dict[str, Any]:
        return {
            "schema": "symcc-topseed-telemetry-v1",
            "enabled": True,
            "program_context": self.program_context,
            "candidates": len(self.candidates),
            "used_groups": len(self.used_groups),
            "runs": len(self.runs),
            "pending_runs": sum(
                run.status == "pending" for run in self.runs.values()
            ),
            "selections": self.selections,
            "explore_selections": self.explore_selections,
            "exploit_selections": self.exploit_selections,
            "learning_rounds": self.learning_rounds,
            "policy_probabilities": dict(zip(
                TOPSEED_POLICIES, self.policy_probabilities
            )),
            "weight_distributions": [list(value) for value in self.weight_distributions],
            "admission_conflicts": self.admission_conflicts,
            "dropped_observations": self.dropped_observations,
            "evicted_candidates": self.evicted_candidates,
            "evicted_runs": self.evicted_runs,
            "random_draws": self.random.draws,
            "proposal_plane_only": True,
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema": TOPSEED_SNAPSHOT_SCHEMA,
            "program_context": self.program_context,
            "seed": self.seed,
            "explore_ratio": self.explore_ratio,
            "learn_interval": self.learn_interval,
            "max_candidates": self.max_candidates,
            "max_runs": self.max_runs,
            "max_features": self.max_features,
            "random": self.random.snapshot(),
            "weight_distributions": [
                list(distribution) for distribution in self.weight_distributions
            ],
            "policy_probabilities": list(self.policy_probabilities),
            "selection_serial": self.selection_serial,
            "selections": self.selections,
            "explore_selections": self.explore_selections,
            "exploit_selections": self.exploit_selections,
            "completed_since_learning": self.completed_since_learning,
            "learning_rounds": self.learning_rounds,
            "admission_conflicts": self.admission_conflicts,
            "dropped_observations": self.dropped_observations,
            "evicted_candidates": self.evicted_candidates,
            "evicted_runs": self.evicted_runs,
            "used_groups": sorted(self.used_groups),
            "candidates": [
                {
                    "identity": candidate.identity,
                    "path": candidate.path,
                    "coverage": list(candidate.coverage),
                    "path_condition": list(candidate.path_condition),
                    "triggers_bug": candidate.triggers_bug,
                    "uses": candidate.uses,
                    "generated_coverage": list(candidate.generated_coverage),
                }
                for candidate in self.candidates.values()
            ],
            "runs": [
                {
                    "token": run.token,
                    "candidate": run.candidate,
                    "mode": run.mode,
                    "policy": run.policy,
                    "weights": list(run.weights),
                    "status": run.status,
                    "generated_coverage": list(run.generated_coverage),
                    "failed": run.failed,
                    "triggers_bug": run.triggers_bug,
                }
                for run in self.runs.values()
            ],
        }

    @classmethod
    def from_snapshot(cls, raw: Any) -> "TopSeedSelector":
        expected_keys = {
            "schema", "program_context", "seed", "explore_ratio",
            "learn_interval", "max_candidates", "max_runs", "max_features",
            "random", "weight_distributions", "policy_probabilities",
            "selection_serial", "selections", "explore_selections",
            "exploit_selections", "completed_since_learning",
            "learning_rounds", "admission_conflicts", "dropped_observations",
            "evicted_candidates", "evicted_runs", "used_groups", "candidates",
            "runs",
        }
        if (
            not isinstance(raw, Mapping)
            or set(raw) != expected_keys
            or raw.get("schema") != TOPSEED_SNAPSHOT_SCHEMA
        ):
            raise ValueError("TopSeed snapshot schema is invalid")
        result = cls(
            program_context=_identity(
                raw.get("program_context"), "TopSeed program context"
            ),
            seed=_bounded_integer(
                raw.get("seed"), "TopSeed seed", minimum=0, maximum=_MASK_U64,
            ),
            explore_ratio=_finite(
                raw.get("explore_ratio"), "TopSeed explore ratio",
                minimum=0.0, maximum=1.0,
            ),
            learn_interval=_bounded_integer(
                raw.get("learn_interval"), "TopSeed learn interval",
                minimum=1, maximum=1_000_000,
            ),
            max_candidates=_bounded_integer(
                raw.get("max_candidates"), "TopSeed candidate limit",
                minimum=2, maximum=1_000_000,
            ),
            max_runs=_bounded_integer(
                raw.get("max_runs"), "TopSeed run limit",
                minimum=2, maximum=1_000_000,
            ),
            max_features=_bounded_integer(
                raw.get("max_features"), "TopSeed feature limit",
                minimum=1, maximum=1_000_000,
            ),
        )
        result.random = _StableRandom.from_snapshot(raw.get("random"))
        distributions = raw.get("weight_distributions")
        if not isinstance(distributions, list) or len(distributions) != 5:
            raise ValueError("TopSeed weight distributions are invalid")
        parsed_distributions: list[tuple[str, float, float]] = []
        for distribution in distributions:
            if not isinstance(distribution, list) or len(distribution) != 3:
                raise ValueError("TopSeed weight distribution is invalid")
            kind = str(distribution[0])
            if kind not in {"uniform", "truncated-normal"}:
                raise ValueError("TopSeed weight distribution kind is invalid")
            mean = _finite(
                distribution[1], "TopSeed weight mean", minimum=-1.0, maximum=1.0,
            )
            deviation = _finite(
                distribution[2], "TopSeed weight deviation",
                minimum=0.0, maximum=2.0,
            )
            if (kind == "uniform") != (mean == 0.0 and deviation == 0.0):
                raise ValueError("TopSeed uniform distribution is non-canonical")
            if kind == "truncated-normal" and deviation <= 0.0:
                raise ValueError("TopSeed normal deviation must be positive")
            parsed_distributions.append((kind, mean, deviation))
        result.weight_distributions = parsed_distributions
        probabilities = raw.get("policy_probabilities")
        if not isinstance(probabilities, list) or len(probabilities) != 4:
            raise ValueError("TopSeed policy probabilities are invalid")
        result.policy_probabilities = [
            _finite(
                probability, "TopSeed policy probability",
                minimum=0.0, maximum=1.0,
            )
            for probability in probabilities
        ]
        if not math.isclose(
            math.fsum(result.policy_probabilities), 1.0,
            rel_tol=1e-12, abs_tol=1e-12,
        ):
            raise ValueError("TopSeed policy probabilities do not sum to one")

        for name in (
            "selection_serial", "selections", "explore_selections",
            "exploit_selections", "completed_since_learning", "learning_rounds",
            "admission_conflicts", "dropped_observations", "evicted_candidates",
            "evicted_runs",
        ):
            setattr(result, name, _bounded_integer(
                raw.get(name), f"TopSeed {name}", minimum=0, maximum=_MAX_U63,
            ))
        if result.explore_selections + result.exploit_selections != result.selections:
            raise ValueError("TopSeed selection counters are inconsistent")
        if result.completed_since_learning >= result.learn_interval:
            raise ValueError("TopSeed learning counter is not canonical")

        def snapshot_features(value: Any, name: str) -> tuple[int, ...]:
            if not isinstance(value, list):
                raise ValueError(f"{name} snapshot is invalid")
            normalized = _features(
                value, name, maximum=result.max_features,
            )
            if list(normalized) != value:
                raise ValueError(f"{name} snapshot is non-canonical")
            return normalized

        raw_candidates = raw.get("candidates")
        if not isinstance(raw_candidates, list) or len(raw_candidates) > result.max_candidates:
            raise ValueError("TopSeed candidate snapshot is invalid")
        for item in raw_candidates:
            if not isinstance(item, Mapping) or set(item) != {
                "identity", "path", "coverage", "path_condition",
                "triggers_bug", "uses", "generated_coverage",
            }:
                raise ValueError("TopSeed candidate snapshot is invalid")
            identity = _identity(item.get("identity"), "TopSeed candidate identity")
            if identity in result.candidates:
                raise ValueError("TopSeed candidate snapshot contains duplicates")
            result.candidates[identity] = _Candidate(
                identity,
                _path(item.get("path")),
                snapshot_features(
                    item.get("coverage"), "TopSeed coverage feature",
                ),
                snapshot_features(
                    item.get("path_condition"),
                    "TopSeed path-condition feature",
                ),
                _boolean(
                    item.get("triggers_bug"), "TopSeed candidate bug flag"
                ),
                _bounded_integer(
                    item.get("uses"), "TopSeed candidate uses",
                    minimum=0, maximum=_MAX_U63,
                ),
                snapshot_features(
                    item.get("generated_coverage"),
                    "TopSeed generated coverage feature",
                ),
            )
            if not result.candidates[identity].coverage:
                raise ValueError("TopSeed candidate coverage is empty")

        raw_groups = raw.get("used_groups")
        if not isinstance(raw_groups, list) or raw_groups != sorted(raw_groups):
            raise ValueError("TopSeed used groups are invalid")
        result.used_groups = {
            _identity(group, "TopSeed group identity") for group in raw_groups
        }
        if len(result.used_groups) != len(raw_groups):
            raise ValueError("TopSeed used groups contain duplicates")

        raw_runs = raw.get("runs")
        if not isinstance(raw_runs, list) or len(raw_runs) > result.max_runs:
            raise ValueError("TopSeed run snapshot is invalid")
        for item in raw_runs:
            if not isinstance(item, Mapping) or set(item) != {
                "token", "candidate", "mode", "policy", "weights", "status",
                "generated_coverage", "failed", "triggers_bug",
            }:
                raise ValueError("TopSeed run snapshot is invalid")
            token = _identity(item.get("token"), "TopSeed run token")
            candidate = _identity(
                item.get("candidate"), "TopSeed run candidate"
            )
            mode = str(item.get("mode"))
            policy = str(item.get("policy"))
            status = str(item.get("status"))
            weights = item.get("weights")
            if (
                token in result.runs
                or candidate not in result.candidates
                or mode not in {"explore", "exploit"}
                or policy not in TOPSEED_POLICIES
                or status not in {"pending", "completed"}
                or not isinstance(weights, list)
                or len(weights) != 5
            ):
                raise ValueError("TopSeed run snapshot is invalid")
            result.runs[token] = _Run(
                token,
                candidate,
                mode,
                policy,
                tuple(_finite(
                    weight, "TopSeed run weight", minimum=-1.0, maximum=1.0,
                ) for weight in weights),
                status,
                snapshot_features(
                    item.get("generated_coverage"),
                    "TopSeed generated coverage feature",
                ),
                _boolean(item.get("failed"), "TopSeed run failure flag"),
                _boolean(item.get("triggers_bug"), "TopSeed run bug flag"),
            )
        expected_groups = {
            result._group_key(candidate.coverage)
            for candidate in result.candidates.values()
            if candidate.uses > 0
        }
        if result.used_groups != expected_groups:
            raise ValueError("TopSeed used-group history is inconsistent")
        if sum(candidate.uses for candidate in result.candidates.values()) != (
            result.selections
        ):
            raise ValueError("TopSeed candidate-use counters are inconsistent")
        if len(result.runs) + result.evicted_runs != result.selections:
            raise ValueError("TopSeed run counters are inconsistent")
        if result.selection_serial < result.selections:
            raise ValueError("TopSeed selection serial is inconsistent")
        for run in result.runs.values():
            candidate = result.candidates[run.candidate]
            if run.status == "pending" and (
                run.generated_coverage or run.failed or run.triggers_bug
            ):
                raise ValueError("TopSeed pending run contains an outcome")
            if run.triggers_bug and not candidate.triggers_bug:
                raise ValueError("TopSeed run bug is absent from its candidate")
        return result

    def save(self, path: str | os.PathLike[str], *, max_bytes: int = 64 << 20) -> None:
        """Atomically publish a canonical, size-bounded selector snapshot."""
        maximum = _bounded_integer(
            max_bytes, "TopSeed snapshot byte limit",
            minimum=4096, maximum=1 << 30,
        )
        target = Path(path)
        encoded = json.dumps(
            self.snapshot(), sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > maximum:
            raise ValueError("TopSeed snapshot exceeds its byte budget")
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.{os.getpid()}.",
            suffix=".tmp",
            dir=target.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    @classmethod
    def load(
        cls,
        path: str | os.PathLike[str],
        *,
        max_bytes: int = 64 << 20,
    ) -> "TopSeedSelector":
        maximum = _bounded_integer(
            max_bytes, "TopSeed snapshot byte limit",
            minimum=4096, maximum=1 << 30,
        )
        target = Path(path)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(target, flags)
        except OSError as error:
            raise ValueError(
                "TopSeed snapshot is not an admissible regular file"
            ) from error
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
                raise ValueError(
                    "TopSeed snapshot is not an admissible regular file"
                )
            chunks: list[bytes] = []
            remaining = maximum + 1
            while remaining:
                chunk = os.read(descriptor, min(1 << 20, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            encoded = b"".join(chunks)
        finally:
            os.close(descriptor)
        if len(encoded) > maximum:
            raise ValueError("TopSeed snapshot exceeds its byte budget")
        try:
            raw = json.loads(encoded.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("TopSeed snapshot JSON is invalid") from error
        return cls.from_snapshot(raw)
