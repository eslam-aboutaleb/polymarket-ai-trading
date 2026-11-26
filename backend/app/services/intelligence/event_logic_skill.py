"""Typed, read-only request/response layer for the event-logic skill (#20).

Deterministic and side-effect free: it opens no network connection, writes nothing and has
no order, signing or wallet capability. Relations are accepted only with approved rule
evidence; infeasible, stale, leaking or unsupported input abstains with explicit conflicts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.intelligence import event_logic as el

SKILL_VERSION = "event-logic-v1"
FEATURE_FLAG = "EVENT_LOGIC_ENABLED"
DEFAULT_MAX_AGE_SECONDS = 3600
MAX_EXPR_DEPTH = 8
MAX_EXPR_NODES = 64

Provenance = Literal["observed", "forecast", "historical"]


class EventIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    p: Decimal = Field(ge=0, le=1)
    source: Provenance
    as_of: datetime
    condition_id: str | None = Field(default=None, max_length=128)
    token_id: str | None = Field(default=None, max_length=128)
    source_id: str | None = Field(default=None, max_length=128)
    rule_hash: str | None = Field(default=None, max_length=128)


class RelationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal[
        "implies", "prerequisite", "complement", "exclusive_group", "thresholds", "identical"
    ]
    events: list[str] = Field(min_length=2, max_length=el.MAX_EVENTS)
    exhaustive: bool = False
    approved: bool = False
    evidence_hash: str | None = Field(default=None, max_length=128)
    rule_hashes: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _arity(self):
        if (
            self.type in {"implies", "prerequisite", "complement", "identical"}
            and len(self.events) != 2
        ):
            raise ValueError("this relation takes exactly two events")
        if len(set(self.events)) != len(self.events):
            raise ValueError("relation events must be distinct")
        return self


class ExpressionQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)
    expr: dict[str, Any]


class ConditionalQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: str
    given: str


class BayesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hypothesis: str
    like_given_h: tuple[Decimal, Decimal]
    like_given_not_h: tuple[Decimal, Decimal]
    likelihood_source: Provenance
    sample_size: int = Field(ge=0)
    calibrated: bool
    likelihood_source_id: str | None = Field(default=None, max_length=128)


class EventLogicRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision_time: datetime
    events: list[EventIn] = Field(min_length=1, max_length=el.MAX_EVENTS)
    relations: list[RelationIn] = Field(default_factory=list, max_length=32)
    joint: list[tuple[str, str]] = Field(default_factory=list, max_length=32)
    conditional: list[ConditionalQuery] = Field(default_factory=list, max_length=32)
    expressions: list[ExpressionQuery] = Field(default_factory=list, max_length=16)
    bayes: list[BayesIn] = Field(default_factory=list, max_length=8)
    max_age_seconds: int = Field(default=DEFAULT_MAX_AGE_SECONDS, ge=1, le=86400 * 7)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamps must carry a timezone")
    return value.astimezone(UTC)


def parse_expression(
    node: Any, names: set[str], *, _depth: int = 0, _count: list[int] | None = None
):
    """JSON -> AST. Only var/not/and/or/implies/xor over declared events."""
    count = _count if _count is not None else [0]
    count[0] += 1
    if _depth > MAX_EXPR_DEPTH or count[0] > MAX_EXPR_NODES:
        raise ValueError("EXPRESSION_TOO_LARGE")
    if not isinstance(node, dict) or len(node) != 1:
        raise ValueError("EXPRESSION_MALFORMED")
    ((op, arg),) = node.items()
    if op == "var":
        if arg not in names:
            raise ValueError(f"EXPRESSION_UNKNOWN_EVENT:{arg}")
        return el.Var(arg)
    if op == "not":
        return el.Not(parse_expression(arg, names, _depth=_depth + 1, _count=count))
    if op in {"and", "or"}:
        if not isinstance(arg, list) or len(arg) < 2:
            raise ValueError("EXPRESSION_MALFORMED")
        args = tuple(parse_expression(a, names, _depth=_depth + 1, _count=count) for a in arg)
        return el.And(args) if op == "and" else el.Or(args)
    if op in {"implies", "xor"}:
        if not isinstance(arg, list) or len(arg) != 2:
            raise ValueError("EXPRESSION_MALFORMED")
        a, b = (parse_expression(x, names, _depth=_depth + 1, _count=count) for x in arg)
        return el.Implies(a, b) if op == "implies" else el.Xor(a, b)
    raise ValueError("EXPRESSION_OPERATOR_UNSUPPORTED")


def _s(value: Fraction | None) -> str | None:
    if value is None:
        return None
    return (
        str(value.numerator) if value.denominator == 1 else f"{value.numerator}/{value.denominator}"
    )


def _dec(value: Fraction | None, places: int = 12) -> str | None:
    if value is None:
        return None
    return (
        format(Decimal(value.numerator) / Decimal(value.denominator), f".{places}f")
        .rstrip("0")
        .rstrip(".")
        or "0"
    )


def _rng(rng: tuple[Fraction, Fraction]) -> dict[str, Any]:
    return {"lower": _dec(rng[0]), "upper": _dec(rng[1]), "exact": [_s(rng[0]), _s(rng[1])]}


def _bounds(model, expr):
    rng = model.bounds(expr)
    if rng is None:  # feasibility was verified before any query
        raise RuntimeError("feasible model returned no bounds")
    return rng


def _abstain(reason: str, conflicts: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "schema_version": SKILL_VERSION,
        "status": "abstain",
        "reason": reason,
        "conflicts": conflicts,
        "execution_eligible": False,
        "feature_flag": FEATURE_FLAG,
        **extra,
    }


def analyze(request: EventLogicRequest) -> dict[str, Any]:
    decision = _utc(request.decision_time)
    max_age = timedelta(seconds=request.max_age_seconds)
    conflicts: list[dict[str, Any]] = []
    by_name: dict[str, EventIn] = {}
    for ev in request.events:
        if ev.name in by_name:
            conflicts.append({"code": "DUPLICATE_EVENT", "events": [ev.name]})
        by_name[ev.name] = ev
        as_of = _utc(ev.as_of)
        if as_of > decision:
            conflicts.append({"code": "TEMPORAL_LEAKAGE", "events": [ev.name]})
        elif decision - as_of > max_age:
            conflicts.append({"code": "STALE_INPUT", "events": [ev.name]})
    relations: list[el.Relation] = []
    for rel in request.relations:
        unknown = [e for e in rel.events if e not in by_name]
        if unknown:
            conflicts.append({"code": "RELATION_UNKNOWN_EVENT", "events": unknown})
            continue
        if not rel.approved or not rel.evidence_hash:
            conflicts.append(
                {"code": "UNAPPROVED_RELATION", "events": list(rel.events), "relation": rel.type}
            )
            continue
        pinned_bad = [
            e for e, h in rel.rule_hashes.items() if e not in by_name or by_name[e].rule_hash != h
        ]
        if pinned_bad:
            conflicts.append(
                {"code": "RULE_REVISED", "events": sorted(pinned_bad), "relation": rel.type}
            )
            continue
        if rel.type == "identical":
            hashes = {by_name[e].rule_hash for e in rel.events}
            if None in hashes or len(hashes) != 1:
                conflicts.append(
                    {"code": "RULE_MISMATCH_FOR_IDENTICAL", "events": list(rel.events)}
                )
                continue
        relations.append(
            el.Relation(
                rel.type,
                tuple(rel.events),
                rel.exhaustive,
                rel.evidence_hash,
                True,
                tuple(sorted(rel.rule_hashes.items())),
            )
        )
    names = set(by_name)
    queried = {x for pair in request.joint for x in pair}
    queried |= {q.event for q in request.conditional} | {q.given for q in request.conditional}
    queried |= {b.hypothesis for b in request.bayes}
    for missing in sorted(queried - names):
        conflicts.append({"code": "QUERY_UNKNOWN_EVENT", "events": [missing]})
    expressions: list[tuple[str, Any]] = []
    for q in request.expressions:
        try:
            expressions.append((q.name, parse_expression(q.expr, names)))
        except ValueError as exc:
            conflicts.append(
                {"code": "EXPRESSION_REJECTED", "events": [q.name], "detail": str(exc)}
            )
    if conflicts:
        return _abstain("INVALID_OR_UNSUPPORTED_INPUT", conflicts)

    graph = el.EventConstraintGraph()
    for ev in request.events:
        graph.add_marginal(
            ev.name, ev.p, source=ev.source, rule_hash=ev.rule_hash, as_of=_utc(ev.as_of)
        )
    for r in relations:
        if r.kind in {"implies", "prerequisite"}:
            graph.implies(*r.events)
        elif r.kind == "complement":
            graph.complement_pair(*r.events)
        elif r.kind == "exclusive_group":
            graph.exclusive_group(r.events, r.exhaustive)
        elif r.kind == "thresholds":
            graph.nested_thresholds(r.events)
        else:  # identical = mutual implication
            graph.implies(*r.events)
            graph.implies(r.events[1], r.events[0])
    pairwise = graph.check(decision)
    marginals = {n: Fraction(str(e.p)) for n, e in by_name.items()}
    model = el.JointModel(marginals, relations)
    if pairwise["status"] != "ok" or not model.feasible():
        diag = list(pairwise["conflicts"])
        minimal = model.minimal_conflict() if not model.feasible() else []
        return _abstain(
            "INFEASIBLE_CONSTRAINTS",
            diag,
            minimal_conflicting_constraints=minimal,
            assumptions=pairwise["assumptions"],
            normalization="none: inputs are never renormalised or repaired",
        )

    joint = []
    for a, b in request.joint:
        rng = _bounds(model, el.And((el.Var(a), el.Var(b))))
        joint.append(
            {
                "events": [a, b],
                **_rng(rng),
                "dependence": "unknown" if rng[0] != rng[1] else "implied_by_constraints",
                "frechet": _rng(el.frechet_and(marginals[a], marginals[b])),
            }
        )
    conditional = []
    for q in request.conditional:
        pb = marginals[q.given]
        if pb == 0:
            conditional.append(
                {
                    "event": q.event,
                    "given": q.given,
                    "status": "abstain",
                    "reason": "CONDITIONING_EVENT_ZERO_PROBABILITY",
                }
            )
            continue
        rng = _bounds(model, el.And((el.Var(q.event), el.Var(q.given))))
        conditional.append(
            {
                "event": q.event,
                "given": q.given,
                "status": "ok",
                **_rng((rng[0] / pb, rng[1] / pb)),
                "dependence": "unknown" if rng[0] != rng[1] else "implied_by_constraints",
            }
        )
    exprs = []
    for name, expr in expressions:
        rng = _bounds(model, expr)
        exprs.append({"name": name, **_rng(rng)})
    bayes = []
    for b in request.bayes:
        result = el.bayes_update(
            marginals[b.hypothesis],
            tuple(b.like_given_h),
            tuple(b.like_given_not_h),
            provenance=b.likelihood_source,
            sample_size=b.sample_size,
            calibrated=b.calibrated,
        )
        post = result.get("posterior")
        bayes.append(
            {
                "hypothesis": b.hypothesis,
                "status": result["status"],
                "reasons": result["reasons"],
                "posterior": _rng(post) if post else None,
                "prior_source": by_name[b.hypothesis].source,
                "likelihood_source": b.likelihood_source,
            }
        )
    assumptions = list(pairwise["assumptions"]) + [
        "joint ranges are exact LP bounds over every feasible joint distribution",
        "price-implied inputs are never mixed with subjective forecasts without a label",
    ]
    return {
        "schema_version": SKILL_VERSION,
        "status": "ok",
        "reason": None,
        "conflicts": [],
        "joint": joint,
        "conditional": conditional,
        "expressions": exprs,
        "bayes": bayes,
        "evidence_graph": evidence_graph(by_name, relations),
        "assumptions": assumptions,
        "as_of_audit": {
            "decision_time": decision.isoformat(),
            "max_age_seconds": request.max_age_seconds,
            "oldest_input_age_seconds": int(
                max((decision - _utc(e.as_of)).total_seconds() for e in request.events)
            ),
        },
        "execution_eligible": False,
        "feature_flag": FEATURE_FLAG,
    }


def evidence_graph(by_name, relations) -> dict[str, Any]:
    """Serialised graph: every node carries its provenance and as-of link."""
    return {
        "version": SKILL_VERSION,
        "nodes": [
            {
                "name": e.name,
                "p": str(e.p),
                "source": e.source,
                "source_id": e.source_id,
                "condition_id": e.condition_id,
                "token_id": e.token_id,
                "rule_hash": e.rule_hash,
                "as_of": _utc(e.as_of).isoformat(),
            }
            for e in by_name.values()
        ],
        "edges": [
            {
                "type": r.kind,
                "events": list(r.events),
                "exhaustive": r.exhaustive,
                "evidence_hash": r.evidence_hash,
                "pinned_rule_hashes": dict(r.pinned_rule_hashes),
            }
            for r in relations
        ],
    }


def describe() -> dict[str, Any]:
    return {
        "name": "event_logic",
        "read_only": True,
        "version": SKILL_VERSION,
        "description": (
            "Exact probability bounds and coherence checks between related contracts "
            "(complement, exclusive groups, nested thresholds, implication) with explicit "
            "infeasibility conflicts and abstention; research only, no order action."
        ),
        "input_schema": EventLogicRequest.model_json_schema(),
    }


# ---- matched scoring of joint forecasts ---------------------------------------------------
def score_joint_forecasts(records: list[dict[str, Any]], *, min_events: int = 30) -> dict[str, Any]:
    """Compare the independence-product joint with the Frechet-clipped joint on outcomes.

    Each record: {"cluster", "pa", "pb", "pab_independent", "a", "b"} where a,b are observed
    outcomes. A joint is only scored when BOTH actual outcomes are observable. The coherent
    candidate clips the independence product into the exact Frechet range, so it can only
    differ where independence was logically impossible. Reports no improvement honestly.
    """
    scored, skipped = [], 0
    for r in records:
        if r.get("a") is None or r.get("b") is None:
            skipped += 1
            continue
        pa, pb = Fraction(str(r["pa"])), Fraction(str(r["pb"]))
        naive = Fraction(str(r["pab_independent"])) if "pab_independent" in r else pa * pb
        lo, hi = el.frechet_and(pa, pb)
        coherent = min(max(naive, lo), hi)
        y = 1 if (r["a"] and r["b"]) else 0
        scored.append((str(r.get("cluster", len(scored))), (naive - y) ** 2, (coherent - y) ** 2))
    clusters: dict[str, list[tuple[Fraction, Fraction]]] = {}
    for c, nb, cb in scored:
        clusters.setdefault(c, []).append((nb, cb))
    n = len(clusters)
    if n == 0:
        return {"status": "abstain", "reason": "NO_OBSERVABLE_JOINT_STATES", "skipped": skipped}
    naive_brier = sum(sum(x for x, _ in v) / len(v) for v in clusters.values()) / n
    coherent_brier = sum(sum(y for _, y in v) / len(v) for v in clusters.values()) / n
    improvement = naive_brier - coherent_brier
    verdict = (
        "INSUFFICIENT_SAMPLE"
        if n < min_events
        else "IMPROVED"
        if improvement > 0
        else "NO_IMPROVEMENT"
    )
    return {
        "status": "ok",
        "verdict": verdict,
        "effective_independent_events": n,
        "min_events": min_events,
        "independence_brier": _dec(naive_brier),
        "coherent_brier": _dec(coherent_brier),
        "brier_improvement": _dec(improvement),
        "convention": "independence minus coherent; positive means coherence helped",
        "skipped_unobserved": skipped,
        "claims_alpha": False,
    }
