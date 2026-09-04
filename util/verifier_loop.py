"""Verifier-in-the-loop admission for generated symbolic-execution inputs.

Generation is never evidence.  A proposal becomes eligible only after bounded
validators return structured, independent evidence; concrete replay is a
mandatory gate for coverage-affecting candidates.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
import hashlib
import json
import math
import time
from typing import Any, Callable, Mapping


Validator = Callable[[Mapping[str, Any]], Mapping[str, Any]]


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                      allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class VerificationEvidence:
    validator: str
    passed: bool
    coverage_delta: int = 0
    target_reached: bool = False
    certificate: str = ""
    reason: str = ""
    elapsed_seconds: float = 0.0


@dataclass(frozen=True)
class VerificationDecision:
    proposal_id: str
    accepted: bool
    status: str
    evidence: tuple[VerificationEvidence, ...]
    decision_id: str
    reason: str


class VerifierInTheLoop:
    """Bounded, deterministic admission around concrete and semantic validators."""

    def __init__(self, validators: Mapping[str, Validator], *, quorum: int = 1,
                 timeout_seconds: float = 30.0, max_evidence: int = 64):
        if not validators or len(validators) > 32:
            raise ValueError("validators must contain 1..32 entries")
        if isinstance(quorum, bool) or not 1 <= int(quorum) <= len(validators):
            raise ValueError("quorum is outside validator count")
        self.validators = dict(validators)
        self.quorum = int(quorum)
        self.timeout_seconds = float(timeout_seconds)
        if not math.isfinite(self.timeout_seconds) or not 0.01 <= self.timeout_seconds <= 3600:
            raise ValueError("timeout_seconds is outside its bound")
        self.max_evidence = max(1, min(256, int(max_evidence)))
        self._decisions: dict[str, VerificationDecision] = {}

    def verify(self, proposal: Mapping[str, Any]) -> VerificationDecision:
        if not isinstance(proposal, Mapping):
            raise ValueError("proposal must be an object")
        proposal_id = str(proposal.get("proposal_id", "")).strip()
        if not proposal_id or len(proposal_id) > 512:
            raise ValueError("proposal_id is invalid")
        if proposal_id in self._decisions:
            return self._decisions[proposal_id]
        started = time.monotonic()
        evidence: list[VerificationEvidence] = []
        for name, validator in self.validators.items():
            if len(evidence) >= self.max_evidence:
                break
            if time.monotonic() - started > self.timeout_seconds:
                evidence.append(VerificationEvidence(name, False, reason="timeout"))
                continue
            try:
                validator_started = time.monotonic()
                raw = validator(proposal)
                validator_elapsed = time.monotonic() - validator_started
                if validator_elapsed > self.timeout_seconds:
                    evidence.append(VerificationEvidence(
                        name, False, reason="timeout",
                        elapsed_seconds=validator_elapsed))
                    continue
                if not isinstance(raw, Mapping):
                    raise ValueError("validator returned a non-object")
                passed = raw.get("passed") is True
                elapsed = max(0.0, float(raw.get("elapsed_seconds", validator_elapsed)))
                if not math.isfinite(elapsed):
                    raise ValueError("validator elapsed_seconds must be finite")
                evidence.append(VerificationEvidence(
                    validator=name,
                    passed=passed,
                    coverage_delta=max(0, int(raw.get("coverage_delta", 0))),
                    target_reached=raw.get("target_reached") is True,
                    certificate=str(raw.get("certificate", ""))[:4096],
                    reason=str(raw.get("reason", ""))[:2048],
                    elapsed_seconds=elapsed,
                ))
            except (TypeError, ValueError, OverflowError) as error:
                evidence.append(VerificationEvidence(name, False, reason=type(error).__name__))
        # Concrete replay is a mandatory validator whenever the proposal claims coverage.
        concrete = next((item for item in evidence if item.validator == "concrete_replay"), None)
        passed = sum(item.passed for item in evidence)
        coverage_claim = any(key in proposal for key in ("target_branch", "coverage_delta"))
        accepted = (
            passed >= self.quorum
            and concrete is not None
            and (not coverage_claim or concrete.passed)
        )
        status = "accepted" if accepted else "rejected"
        reason = "quorum satisfied" if accepted else "verification evidence insufficient"
        decision_payload = {
            "proposal_id": proposal_id,
            "accepted": accepted,
            "status": status,
            "evidence": [asdict(item) for item in evidence],
        }
        decision = VerificationDecision(
            proposal_id, accepted, status, tuple(evidence), _digest(decision_payload), reason)
        self._decisions[proposal_id] = decision
        return decision

    def snapshot(self) -> dict[str, Any]:
        return {"schema": "symcc-verifier-loop-v1", "decisions": len(self._decisions),
                "accepted": sum(item.accepted for item in self._decisions.values())}
