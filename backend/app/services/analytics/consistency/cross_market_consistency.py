"""Deterministic cross-market consistency detector (issue #19).

Tests whether the quoted bid/ask bands of verified related markets admit ANY set of
probabilities that satisfies a declared logical relation. Exact rational arithmetic
(Fourier-Motzkin elimination), a minimal inconsistent subset by deletion filter.
Output is an alert only: ``executable`` is always False. Executable baskets, depth and
leg risk belong to the #9 feasibility engine. No keys, no orders, no side effects.
"""

from datetime import timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.services.market_data.binary_book import Contract

SKILL_VERSION = "cross-market-consistency-v1"
MAX_MARKETS = 12
MAX_ROWS = 4000
MAX_SNAPSHOT_SKEW_SECONDS = 5
MAX_QUOTE_AGE_SECONDS = 60

Template = Literal["complement", "partition", "threshold", "implication"]
Classification = Literal[
    "logical_inconsistency", "pricing_anomaly", "executable_opportunity_not_established"
]


class MarketQuote(Contract):
    market_id: str = Field(min_length=1, max_length=128)
    condition_id: str = Field(min_length=1, max_length=128)
    token_id: str = Field(min_length=1, max_length=128)
    question: str = Field(default="", max_length=500)
    rules_hash: str = Field(min_length=1, max_length=128)
    rules_text: str = Field(default="", max_length=4000)
    oracle: str = Field(min_length=1, max_length=128)
    settlement_venue: str = Field(min_length=1, max_length=64)
    end_time: AwareDatetime
    bid: Decimal | None = Field(default=None, ge=0, le=1)
    ask: Decimal | None = Field(default=None, ge=0, le=1)
    bid_size: Decimal | None = Field(default=None, ge=0)
    ask_size: Decimal | None = Field(default=None, ge=0)
    snapshot_at: AwareDatetime


class Relation(Contract):
    relation_id: str = Field(min_length=1, max_length=128)
    template: Template
    members: list[str] = Field(min_length=2, max_length=MAX_MARKETS)
    verified: bool = False  # rule mapping verified out of band; never inferred here
    # partition only: exhaustive mutually exclusive (sum == 1), exclusive only (sum <= 1),
    # or overlapping (no constraint is implied)
    partition_kind: Literal["exhaustive_exclusive", "exclusive", "overlapping"] = "exclusive"


class ConsistencyRequest(Contract):
    as_of: AwareDatetime
    markets: list[MarketQuote] = Field(min_length=2, max_length=MAX_MARKETS)
    relation: Relation
    min_basket_size: Decimal = Field(default=Decimal(10), gt=0, le=1_000_000)

    @model_validator(mode="after")
    def _members_exist(self) -> "ConsistencyRequest":
        ids = {m.market_id for m in self.markets}
        if len(ids) != len(self.markets):
            raise ValueError("duplicate market_id")
        missing = [m for m in self.relation.members if m not in ids]
        if missing:
            raise ValueError(f"relation members not in markets: {missing}")
        return self


class ConsistencyResult(Contract):
    skill_version: str = SKILL_VERSION
    relation_id: str
    template: Template
    classification: Classification
    abstained: bool
    abstain_reasons: list[str]
    violation_magnitude: Decimal | None = None
    minimal_inconsistent_subset: list[str] = []
    violated_constraint: str | None = None
    assumption_chain: list[str]
    one_leg_loss_bound_per_share: Decimal | None = None
    executable: Literal[False] = False
    execution_note: str = "Alert only. Basket cost, depth and leg risk are assessed by #9."
    as_of: AwareDatetime
    source_ids: list[str]
    rules_hashes: dict[str, str]
    rules_for_review: dict[str, str]
    telemetry: dict[str, int | str]


# ---- exact LP feasibility ---------------------------------------------------------------
Row = tuple[dict[int, Fraction], Fraction, frozenset[str]]  # sum(a_j x_j) <= b


def _fm_infeasible(rows: list[Row], nvars: int) -> bool | None:
    """True if infeasible, False if feasible, None if the size limit was hit."""
    cur = list(rows)
    for v in range(nvars):
        pos = [r for r in cur if r[0].get(v, 0) > 0]
        neg = [r for r in cur if r[0].get(v, 0) < 0]
        rest = [r for r in cur if r[0].get(v, 0) == 0]
        if len(pos) * len(neg) + len(rest) > MAX_ROWS:
            return None
        for p in pos:
            for n in neg:
                lp, ln = p[0][v], -n[0][v]
                co: dict[int, Fraction] = {}
                for j in set(p[0]) | set(n[0]):
                    if j == v:
                        continue
                    c = p[0].get(j, Fraction(0)) * ln + n[0].get(j, Fraction(0)) * lp
                    if c:
                        co[j] = c
                rest.append((co, p[1] * ln + n[1] * lp, p[2] | n[2]))
        cur = rest
    return any(not r[0] and r[1] < 0 for r in cur)


def _relation_rows(template: str, kind: str, idx: list[int]) -> list[Row]:
    rid = "relation"
    one = Fraction(1)
    if template == "complement" or (template == "partition" and kind == "exhaustive_exclusive"):
        c = dict.fromkeys(idx, one)
        return [
            (c, one, frozenset({rid + ":sum<=1"})),
            (dict.fromkeys(idx, -one), -one, frozenset({rid + ":sum>=1"})),
        ]
    if template == "partition" and kind == "exclusive":
        return [(dict.fromkeys(idx, one), one, frozenset({rid + ":sum<=1"}))]
    if template == "threshold":  # P(X>k_i) is non-increasing along members
        return [
            ({idx[k + 1]: one, idx[k]: -one}, Fraction(0), frozenset({f"{rid}:order[{k}]"}))
            for k in range(len(idx) - 1)
        ]
    if template == "implication":  # P(A) <= P(B)
        return [({idx[0]: one, idx[1]: -one}, Fraction(0), frozenset({rid + ":A<=B"}))]
    return []


def _box(lo: list[Fraction], hi: list[Fraction], ids: list[str]) -> list[Row]:
    rows: list[Row] = []
    for i, (a, b) in enumerate(zip(lo, hi, strict=True)):
        rows.append(({i: Fraction(1)}, b, frozenset({f"quote:{ids[i]}:ask"})))
        rows.append(({i: Fraction(-1)}, -a, frozenset({f"quote:{ids[i]}:bid"})))
    return rows


def _infeasible(rel: list[Row], lo, hi, ids) -> bool | None:
    return _fm_infeasible(rel + _box(lo, hi, ids), len(ids))


def _min_subset(rel: list[Row], lo, hi, ids) -> list[str]:
    rows = rel + _box(lo, hi, ids)
    labels = sorted({o for r in rows for o in r[2]})
    keep = list(labels)
    for lab in labels:
        trial = [lab2 for lab2 in keep if lab2 != lab]
        sub = [r for r in rows if r[2] <= set(trial)]
        if _fm_infeasible(sub, len(ids)) is True:
            keep = trial
    return keep


def _magnitude(rel: list[Row], lo, hi, ids) -> Decimal:
    """Smallest uniform widening of every band that restores feasibility."""
    a, b = Fraction(0), Fraction(1)
    for _ in range(40):
        m = (a + b) / 2
        if _infeasible(rel, [x - m for x in lo], [x + m for x in hi], ids):
            a = m
        else:
            b = m
    return (Decimal(b.numerator) / Decimal(b.denominator)).quantize(Decimal("0.000001"))


def _fr(d: Decimal) -> Fraction:
    return Fraction(d)


def analyze(req: ConsistencyRequest) -> ConsistencyResult:
    by_id = {m.market_id: m for m in req.markets}
    rel = req.relation
    members = [by_id[i] for i in rel.members]
    chain = [
        f"relation {rel.template} declared over {len(members)} markets",
        "probabilities must lie inside each market's quoted bid/ask band",
        "midpoints are display only and never treated as fills",
    ]
    reasons: list[str] = []

    def done(cls, mag=None, mis=None, viol=None, loss=None) -> ConsistencyResult:
        return ConsistencyResult(
            relation_id=rel.relation_id,
            template=rel.template,
            classification=cls,
            abstained=bool(reasons),
            abstain_reasons=reasons,
            violation_magnitude=mag,
            minimal_inconsistent_subset=mis or [],
            violated_constraint=viol,
            assumption_chain=chain,
            one_leg_loss_bound_per_share=loss,
            as_of=req.as_of,
            source_ids=[f"{m.market_id}/{m.token_id}" for m in members],
            rules_hashes={m.market_id: m.rules_hash for m in members},
            rules_for_review={m.market_id: m.rules_text or m.question for m in members},
            telemetry={"markets": len(members), "outcome": cls, "abstentions": len(reasons)},
        )

    if not rel.verified:
        reasons.append("RULES_NOT_VERIFIED")
    if len({m.oracle for m in members}) > 1 or len({m.settlement_venue for m in members}) > 1:
        reasons.append("ORACLE_OR_SETTLEMENT_MISMATCH")
    if len({m.end_time for m in members}) > 1:
        reasons.append("CUTOFF_MISMATCH")
    if rel.template == "complement":
        if len(members) != 2:
            reasons.append("COMPLEMENT_NEEDS_TWO_MARKETS")
        elif members[0].condition_id != members[1].condition_id:
            reasons.append("WRONG_CONDITION_JOIN")
        elif members[0].token_id == members[1].token_id:
            reasons.append("SAME_TOKEN")
        if len({m.rules_hash for m in members}) > 1:
            reasons.append("RULES_HASH_MISMATCH")
    if rel.template == "partition" and rel.partition_kind == "overlapping":
        reasons.append("OVERLAPPING_OUTCOMES")
    if rel.template == "implication" and len(members) != 2:
        reasons.append("IMPLICATION_NEEDS_TWO_MARKETS")
    if any(m.bid is None or m.ask is None for m in members):
        reasons.append("MISSING_QUOTE")
    if any(m.bid is not None and m.ask is not None and m.bid > m.ask for m in members):
        reasons.append("CROSSED_BOOK")
    if any(m.snapshot_at > req.as_of for m in members):
        reasons.append("LOOK_AHEAD_SNAPSHOT")
    elif any(req.as_of - m.snapshot_at > timedelta(seconds=MAX_QUOTE_AGE_SECONDS) for m in members):
        reasons.append("STALE_QUOTE")
    snaps = [m.snapshot_at for m in members]
    if max(snaps) - min(snaps) > timedelta(seconds=MAX_SNAPSHOT_SKEW_SECONDS):
        reasons.append("NON_SYNCHRONOUS_BOOKS")
    if reasons:
        return done("executable_opportunity_not_established")

    ids = [m.market_id for m in members]
    idx = list(range(len(members)))
    lo = [_fr(m.bid) for m in members]  # type: ignore[arg-type]
    hi = [_fr(m.ask) for m in members]  # type: ignore[arg-type]
    rows = _relation_rows(rel.template, rel.partition_kind, idx)
    res = _infeasible(rows, lo, hi, ids)
    if res is None:
        reasons.append("SOLVER_LIMIT")
        return done("executable_opportunity_not_established")
    if res:
        mag = _magnitude(rows, lo, hi, ids)
        mis = _min_subset(rows, lo, hi, ids)
        viol = next((m for m in mis if m.startswith("relation:")), None)
        depth_ok = all(
            (m.ask_size or Decimal(0)) >= req.min_basket_size
            and (m.bid_size or Decimal(0)) >= req.min_basket_size
            for m in members
        )
        # delayed or partial leg: first leg fills at its ask, the rest never does
        loss = max(m.ask for m in members if m.ask is not None)
        if not depth_ok:
            reasons.append("INSUFFICIENT_BASKET_DEPTH")
            chain.append("violation exists in quotes but displayed depth is below min size")
            return done("executable_opportunity_not_established", mag, mis, viol, loss)
        chain.append("no probability inside the quoted bands satisfies the relation")
        return done("logical_inconsistency", mag, mis, viol, loss)
    mids = [(a + b) / 2 for a, b in zip(lo, hi, strict=True)]
    if _infeasible(rows, mids, mids, ids):
        chain.append("only midpoint display prices violate the relation; the bands admit a fit")
        return done("pricing_anomaly", _magnitude(rows, mids, mids, ids))
    chain.append("relation is satisfiable inside the quoted bands")
    return done("executable_opportunity_not_established")
