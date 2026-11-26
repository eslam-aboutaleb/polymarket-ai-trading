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
from itertools import product
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


# ---- exact LP over joint worlds -----------------------------------------------------
MAX_EVENTS = 10  # 2**10 joint worlds keeps the exact simplex bounded and fast.


def _pivot(tab, basis, row, col):
    pivot = tab[row][col]
    tab[row] = [v / pivot for v in tab[row]]
    for i, other in enumerate(tab):
        if i != row and other[col] != 0:
            factor = other[col]
            tab[i] = [a - factor * b for a, b in zip(other, tab[row], strict=True)]
    basis[row] = col


def _run_simplex(tab, basis, cost, limit):
    """Bland's rule: terminates on degenerate problems. Minimises cost over columns < limit."""
    while True:
        entering = None
        for j in range(limit):
            reduced = cost[j] - sum(
                (cost[basis[i]] * tab[i][j] for i in range(len(tab)) if cost[basis[i]] != 0), ZERO
            )
            if reduced < 0:
                entering = j
                break
        if entering is None:
            return
        best = None
        for i in range(len(tab)):
            if tab[i][entering] > 0:
                ratio = tab[i][-1] / tab[i][entering]
                if (
                    best is None
                    or ratio < best[0]
                    or (ratio == best[0] and basis[i] < basis[best[1]])
                ):
                    best = (ratio, i)
        if best is None:  # unreachable: variables are bounded by sum(q)=1
            raise ArithmeticError("unbounded program")
        _pivot(tab, basis, best[1], entering)


def solve_lp(a_matrix, b_vector, objective):
    """Exact two-phase simplex: minimise objective.x s.t. A x = b, x >= 0.

    Returns ``None`` if infeasible, else the exact optimal value (Fraction).
    """
    m, n = len(a_matrix), len(a_matrix[0])
    tab = []
    for i in range(m):
        row = [Fraction(v) for v in a_matrix[i]]
        rhs = Fraction(b_vector[i])
        if rhs < 0:
            row, rhs = [-v for v in row], -rhs
        tab.append(row + [ONE if k == i else ZERO for k in range(m)] + [rhs])
    basis = [n + i for i in range(m)]
    phase1 = [ZERO] * n + [ONE] * m
    _run_simplex(tab, basis, phase1 + [ZERO], n + m)
    if sum((phase1[basis[i]] * tab[i][-1] for i in range(m)), ZERO) > 0:
        return None
    keep = []
    for i in range(m):
        if basis[i] >= n:
            col = next((j for j in range(n) if tab[i][j] != 0), None)
            if col is None:
                continue  # redundant equality row
            _pivot(tab, basis, i, col)
        keep.append(i)
    tab, basis = [tab[i] for i in keep], [basis[i] for i in keep]
    cost = [Fraction(v) for v in objective] + [ZERO] * m + [ZERO]
    _run_simplex(tab, basis, cost, n)
    return sum((cost[basis[i]] * tab[i][-1] for i in range(len(tab))), ZERO)


# ---- relation-aware joint model ---------------------------------------------------------
RELATION_TYPES = {
    "implies",
    "prerequisite",
    "complement",
    "exclusive_group",
    "thresholds",
    "identical",
}


@dataclass(frozen=True)
class Relation:
    """A typed relation that is only usable with approved rule evidence."""

    kind: str
    events: tuple[str, ...]
    exhaustive: bool = False
    evidence_hash: str | None = None
    approved: bool = False
    pinned_rule_hashes: tuple[tuple[str, str], ...] = ()

    def label(self) -> str:
        return f"{self.kind}:{'|'.join(self.events)}"


def _allowed(rel: Relation, world: dict[str, bool]) -> bool:
    ev = rel.events
    if rel.kind in {"implies", "prerequisite"}:
        return not world[ev[0]] or world[ev[1]]  # prerequisite(a, b): a requires b
    if rel.kind == "complement":
        return world[ev[0]] != world[ev[1]]
    if rel.kind == "identical":
        return world[ev[0]] == world[ev[1]]
    if rel.kind == "thresholds":  # ordered low -> high: each higher event implies the lower
        return all(not world[ev[i + 1]] or world[ev[i]] for i in range(len(ev) - 1))
    if rel.kind == "exclusive_group":
        n_true = sum(world[e] for e in ev)
        return n_true <= 1 and (not rel.exhaustive or n_true == 1)
    raise ValueError("unknown relation")


class JointModel:
    """Feasible joint distributions over binary events given marginals and relations.

    Marginals are equality constraints, relations remove impossible worlds. Nothing is
    normalised, no independence is assumed: questions about unconstrained overlap return
    the exact LP range.
    """

    def __init__(self, marginals: dict[str, Fraction], relations: list[Relation]):
        if not 0 < len(marginals) <= MAX_EVENTS:
            raise ValueError("TOO_MANY_OR_NO_EVENTS")
        self.names = tuple(marginals)
        self.marginals = dict(marginals)
        self.relations = list(relations)

    def _worlds(self, relations):
        out = []
        for values in product((False, True), repeat=len(self.names)):
            world = dict(zip(self.names, values, strict=True))
            if all(_allowed(r, world) for r in relations):
                out.append(world)
        return out

    def _program(self, relations, marginal_names):
        worlds = self._worlds(relations)
        if not worlds:
            return worlds, None, None
        a = [[ONE] * len(worlds)]
        b = [ONE]
        for name in marginal_names:
            a.append([ONE if w[name] else ZERO for w in worlds])
            b.append(self.marginals[name])
        return worlds, a, b

    def feasible(self, relations=None, marginal_names=None) -> bool:
        rels = self.relations if relations is None else relations
        names = self.names if marginal_names is None else marginal_names
        worlds, a, b = self._program(rels, names)
        if not worlds:
            return False
        return solve_lp(a, b, [ZERO] * len(worlds)) is not None

    def minimal_conflict(self) -> list[str]:
        """Deletion filter: a minimal subset of constraints that is jointly infeasible."""
        items = [("marginal", n) for n in self.names] + [
            ("relation", i) for i in range(len(self.relations))
        ]
        for item in list(items):
            trial = [x for x in items if x != item]
            rels = [self.relations[i] for k, i in trial if k == "relation"]
            names = [i for k, i in trial if k == "marginal"]
            if not self.feasible(rels, names):
                items = trial
        return [f"marginal:{i}" if k == "marginal" else self.relations[i].label() for k, i in items]

    def bounds(self, expr) -> tuple[Fraction, Fraction] | None:
        """Exact [min, max] of P(expr) over every feasible joint, or None if infeasible."""
        worlds, a, b = self._program(self.relations, self.names)
        if not worlds:
            return None
        c = [ONE if evaluate(expr, w) else ZERO for w in worlds]
        lo = solve_lp(a, b, c)
        if lo is None:
            return None
        hi = -solve_lp(a, b, [-v for v in c])
        return lo, hi


# ---- Bayes updating with explicit provenance -----------------------------------------------
def bayes_update(
    prior, like_given_h, like_given_not_h, *, provenance, sample_size, calibrated, min_samples=30
) -> dict[str, Any]:
    """Posterior RANGE from likelihood intervals; abstains unless provenance is defensible.

    Likelihoods must be supplied (never defaulted), come from observed or historical
    data (a subjective forecast is not a likelihood), be calibrated, and rest on at
    least ``min_samples`` observations.
    """
    reasons = []
    if provenance not in {"observed", "historical"}:
        reasons.append("LIKELIHOOD_PROVENANCE_UNDEFINED")
    if not calibrated:
        reasons.append("LIKELIHOOD_NOT_CALIBRATED")
    if (
        not isinstance(sample_size, int)
        or isinstance(sample_size, bool)
        or sample_size < min_samples
    ):
        reasons.append("LIKELIHOOD_SAMPLE_TOO_SMALL")
    if reasons:
        return {"status": "abstain", "reasons": reasons, "posterior": None}
    pr = _p(prior)
    h_lo, h_hi = (_p(v) for v in like_given_h)
    n_lo, n_hi = (_p(v) for v in like_given_not_h)
    if h_lo > h_hi or n_lo > n_hi:
        return {"status": "abstain", "reasons": ["LIKELIHOOD_INTERVAL_INVERTED"], "posterior": None}

    def post(lh, ln):
        den = pr * lh + (ONE - pr) * ln
        return None if den == 0 else pr * lh / den

    lo, hi = post(h_lo, n_hi), post(h_hi, n_lo)
    if lo is None or hi is None:
        return {"status": "abstain", "reasons": ["EVIDENCE_PROBABILITY_ZERO"], "posterior": None}
    return {"status": "ok", "reasons": [], "posterior": (lo, hi), "assumes_independence": False}
