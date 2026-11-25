"""Event constraint graph: exact probability bounds between related contracts (#20).

Pure, deterministic, side-effect free and research-only. Uses Fraction/Decimal-safe
arithmetic so boundary cases (P(B)=0, sums exactly 1) are exact. Never normalizes or
guesses dependence: unknown overlap yields a Frechet range, infeasible input yields a
conflict list and abstain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from fractions import Fraction
from typing import Any

SCHEMA_VERSION = "event-logic-v1"

ZERO, ONE = Fraction(0), Fraction(1)


def _p(value: Any) -> Fraction:
    if isinstance(value, bool):
        raise ValueError("probability must be numeric")
    f = Fraction(str(value)) if not isinstance(value, Fraction) else value
    if not ZERO <= f <= ONE:
        raise ValueError("probability outside [0,1]")
    return f


# ---- expression AST -------------------------------------------------------------
@dataclass(frozen=True)
class Var:
    name: str


@dataclass(frozen=True)
class Not:
    arg: Any


@dataclass(frozen=True)
class And:
    args: tuple


@dataclass(frozen=True)
class Or:
    args: tuple


def Implies(a, b):  # noqa: N802
    return Or((Not(a), b))


def Xor(a, b):  # noqa: N802
    return Or((And((a, Not(b))), And((Not(a), b))))


def evaluate(expr, world: dict[str, bool]) -> bool:
    if isinstance(expr, Var):
        return world[expr.name]
    if isinstance(expr, Not):
        return not evaluate(expr.arg, world)
    if isinstance(expr, And):
        return all(evaluate(a, world) for a in expr.args)
    if isinstance(expr, Or):
        return any(evaluate(a, world) for a in expr.args)
    raise TypeError("unsupported expression")


# ---- bounds ---------------------------------------------------------------------
def complement(p) -> Fraction:
    return ONE - _p(p)


def frechet_and(pa, pb) -> tuple[Fraction, Fraction]:
    a, b = _p(pa), _p(pb)
    return max(ZERO, a + b - ONE), min(a, b)


def frechet_or(pa, pb) -> tuple[Fraction, Fraction]:
    a, b = _p(pa), _p(pb)
    return max(a, b), min(ONE, a + b)


def conditional(p_a_and_b, p_b) -> dict[str, Any]:
    """P(A|B) only when P(B)>0; never fabricates a likelihood."""
    ab, b = _p(p_a_and_b), _p(p_b)
    if b == 0:
        return {"status": "abstain", "reason": "CONDITIONING_EVENT_ZERO_PROBABILITY", "value": None}
    if ab > b:
        return {"status": "abstain", "reason": "JOINT_EXCEEDS_MARGINAL", "value": None}
    return {"status": "ok", "reason": None, "value": ab / b}


@dataclass
class EventConstraintGraph:
    marginals: dict[str, Fraction] = field(default_factory=dict)
    provenance: dict[str, str] = field(default_factory=dict)  # observed|forecast|historical
    implications: list[tuple[str, str]] = field(default_factory=list)  # A implies B
    complements: list[tuple[str, str]] = field(default_factory=list)
    exclusive_groups: list[tuple[tuple[str, ...], bool]] = field(default_factory=list)
    thresholds: list[tuple[str, ...]] = field(default_factory=list)  # nested, ascending
    rule_hashes: dict[str, str] = field(default_factory=dict)
    as_of: dict[str, datetime] = field(default_factory=dict)

    def add_marginal(self, name, p, *, source="forecast", rule_hash=None, as_of=None):
        if source not in {"observed", "forecast", "historical"}:
            raise ValueError("unknown provenance")
        self.marginals[name] = _p(p)
        self.provenance[name] = source
        if rule_hash:
            self.rule_hashes[name] = rule_hash
        if as_of:
            self.as_of[name] = as_of

    def implies(self, a, b):
        self.implications.append((a, b))

    def complement_pair(self, a, b):
        self.complements.append((a, b))

    def exclusive_group(self, names, exhaustive=False):
        self.exclusive_groups.append((tuple(names), exhaustive))

    def nested_thresholds(self, names_low_to_high):
        """names ordered so each event implies the previous (e.g. >=100k implies >=90k)."""
        self.thresholds.append(tuple(names_low_to_high))

    def check(self, decision_time: datetime | None = None) -> dict[str, Any]:
        conflicts: list[dict[str, Any]] = []
        assumptions = ["no independence assumed", "unknown dependence reported as a range"]
        m = self.marginals

        def need(n):
            if n not in m:
                conflicts.append({"code": "MISSING_MARGINAL", "events": [n]})
                return False
            return True

        if decision_time is not None:
            for n, t in self.as_of.items():
                if t > decision_time:
                    conflicts.append({"code": "TEMPORAL_LEAKAGE", "events": [n]})
        imps = list(self.implications)
        for chain in self.thresholds:
            imps += [(chain[i + 1], chain[i]) for i in range(len(chain) - 1)]
        for a, b in imps:
            if need(a) and need(b) and m[a] > m[b]:
                conflicts.append({"code": "IMPLICATION_VIOLATED", "events": [a, b]})
        for a, b in self.complements:
            if need(a) and need(b) and m[a] + m[b] != ONE:
                conflicts.append({"code": "COMPLEMENT_SUM_NOT_ONE", "events": [a, b]})
        for names, exhaustive in self.exclusive_groups:
            if not all(need(n) for n in names):
                continue
            total = sum((m[n] for n in names), ZERO)
            if total > ONE or (exhaustive and total != ONE):
                conflicts.append({"code": "GROUP_SUM_INCONSISTENT", "events": list(names)})
            if not exhaustive:
                assumptions.append(f"group {list(names)} is not exhaustive: sum may be <1")
        for a, b in imps:
            for c, _exh in self.exclusive_groups:
                if a in c[0] and b in c[0] and a != b and m.get(a, ZERO) > 0:
                    conflicts.append(
                        {"code": "IMPLICATION_CONTRADICTS_EXCLUSIVITY", "events": [a, b]}
                    )
        if conflicts:
            return {
                "schema_version": SCHEMA_VERSION,
                "status": "abstain",
                "reason": "INFEASIBLE_CONSTRAINTS",
                "conflicts": conflicts,
                "assumptions": assumptions,
                "execution_eligible": False,
            }
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "ok",
            "reason": None,
            "conflicts": [],
            "assumptions": assumptions,
            "provenance": dict(self.provenance),
            "rule_hashes": dict(self.rule_hashes),
            "execution_eligible": False,
        }

    def joint_bounds(self, a: str, b: str) -> dict[str, Any]:
        """Range for P(A and B) respecting Frechet bounds and implications; no independence."""
        verdict = self.check()
        if verdict["status"] != "ok":
            return verdict
        pa, pb = self.marginals[a], self.marginals[b]
        lo, hi = frechet_and(pa, pb)
        if (a, b) in self.implications:
            lo = hi = pa
        elif (b, a) in self.implications:
            lo = hi = pb
        for names, _ in self.exclusive_groups:
            if a in names and b in names and a != b:
                lo = hi = ZERO
        return {
            **verdict,
            "lower": lo,
            "upper": hi,
            "dependence": "unknown" if lo != hi else "implied",
        }
