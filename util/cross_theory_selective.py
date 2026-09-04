"""Dependency-closed selective concolic planning across SMT theories."""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Mapping


THEORIES = frozenset({"BV", "INT", "REAL", "STRING", "ARRAY", "BOOL"})


@dataclass(frozen=True)
class ConstraintAtom:
    atom_id: str
    theory: str
    variables: tuple[str, ...]
    depends_on: tuple[str, ...] = ()
    selected: bool = False


@dataclass(frozen=True)
class SelectiveDecision:
    mode: str
    selected_atoms: tuple[str, ...]
    fixed_variables: tuple[str, ...]
    missing_dependencies: tuple[str, ...]
    reason: str


class CrossTheorySelector:
    """Choose a sound selective slice, falling back when the closure is incomplete."""

    def __init__(self, *, max_atoms: int = 100_000, max_variables: int = 100_000):
        self.max_atoms = max(1, min(1_000_000, int(max_atoms)))
        self.max_variables = max(1, min(1_000_000, int(max_variables)))

    def plan(self, atoms: list[Mapping[str, Any]],
             requested: set[str], concrete: Mapping[str, Any] | None = None) -> SelectiveDecision:
        if len(atoms) > self.max_atoms or len(requested) > self.max_variables:
            return SelectiveDecision("full", (), (), (), "input exceeds selective bounds")
        parsed: dict[str, ConstraintAtom] = {}
        for raw in atoms:
            atom_id = str(raw.get("atom_id", "")).strip()
            theory = str(raw.get("theory", "")).upper().strip()
            variables = tuple(dict.fromkeys(str(item) for item in raw.get("variables", ())))
            deps = tuple(dict.fromkeys(str(item) for item in raw.get("depends_on", ())))
            if not atom_id or theory not in THEORIES or not variables:
                return SelectiveDecision("full", (), (), (), "malformed atom")
            if atom_id in parsed:
                return SelectiveDecision("full", (), (), (), "duplicate atom id")
            parsed[atom_id] = ConstraintAtom(atom_id, theory, variables, deps,
                                             bool(raw.get("selected", False)))
        chosen = {atom_id for atom_id, atom in parsed.items()
                  if atom.selected or set(atom.variables) & requested}
        closure = set(chosen)
        missing: set[str] = set()
        changed = True
        while changed:
            changed = False
            for atom_id in tuple(closure):
                atom = parsed.get(atom_id)
                if atom is None:
                    missing.add(atom_id)
                    continue
                for dependency in atom.depends_on:
                    if dependency not in parsed:
                        missing.add(dependency)
                    elif dependency not in closure:
                        closure.add(dependency)
                        changed = True
        selected_vars = {variable for atom_id in closure if atom_id in parsed
                         for variable in parsed[atom_id].variables}
        all_vars = {variable for atom in parsed.values() for variable in atom.variables}
        fixed = sorted((all_vars - selected_vars) & set(concrete or {}))
        if missing or (all_vars - selected_vars) - set(concrete or {}):
            reason = "dependency closure incomplete; retain full theory query"
            return SelectiveDecision("full", tuple(sorted(parsed)), (), tuple(sorted(missing)), reason)
        return SelectiveDecision(
            "selective", tuple(sorted(closure)), tuple(fixed), (),
            "dependency-closed cross-theory slice")

    @staticmethod
    def as_dict(decision: SelectiveDecision) -> dict[str, Any]:
        return asdict(decision)
