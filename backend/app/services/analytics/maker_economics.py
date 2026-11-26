"""Maker economics (issue #23): what a quote really earned, never what it quoted.

Read-side and deterministic. It takes recorded quote lifecycles and fills and
returns realized economics, effective spread, post-fill markouts, adverse
selection, nonfills and cancel-race losses. It holds no keys, signs nothing,
submits or cancels nothing, and never marks a modeled fill as realized.

Rules enforced here:

* ``maker_realized_pnl = fill cashflow + closing cashflow - maker_fees
  + confirmed_rebates - holding_costs`` and only over ``venue_confirmed``
  fills. Modeled fills are reported in a separate ``counterfactual`` block.
* A quote with unresolved inventory, an unknown fee, an unknown holding cost
  or evidence older than the limit abstains with an explicit reason code; it
  is never filled with zero.
* Rebates count only when ``status == "confirmed"``; ``not_applicable`` and
  ``unconfirmed`` count zero and are flagged.
* No queue priority is assumed. Queue position appears only as recorded.
* Effective spread and markouts are fill-weighted and exclude fills whose mid
  is unknown (counted, not zero-filled).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

SKILL_VERSION = "maker-economics-v1"
ZERO = Decimal(0)
DEFAULT_HORIZONS = (1, 10, 60)

# Source: https://docs.polymarket.com/trading/fees and
# https://docs.polymarket.com/programs/maker-rebates , read 2026-11-25.
# Per-market archived fee metadata (Gamma feeSchedule) takes precedence over this table.
FEE_TABLE: dict[str, Any] = {
    "version": "polymarket-fees-2026-11-25",
    "source_urls": [
        "https://docs.polymarket.com/trading/fees",
        "https://docs.polymarket.com/programs/maker-rebates",
        "https://docs.polymarket.com/programs/fee-and-rebate-changes-november-2026",
    ],
    "effective_from": "2026-11-20",
    "retrieved_at": "2026-11-25",
    "formula": "taker_fee = shares * feeRate * p * (1 - p); makers are never charged fees",
    "rebate_rule": (
        "Paid daily from taker fees by maker tier; standard rebates start at $200,000 of "
        "30-day maker weighted volume; fee-free markets and combos earn none; $1 minimum payout"
    ),
    "categories": {
        "crypto_btc_5m_15m_updown": {"taker": "0.05", "maker": "0", "rebate_eligible": True},
        "crypto_other": {"taker": "0.01", "maker": "0", "rebate_eligible": True},
        "sports": {"taker": "0.05", "maker": "0", "rebate_eligible": True},
        "finance": {"taker": "0.05", "maker": "0", "rebate_eligible": True},
        "tech": {"taker": "0.05", "maker": "0", "rebate_eligible": True},
        "politics_elections": {"taker": "0.05", "maker": "0", "rebate_eligible": True},
        "politics_other": {"taker": "0.01", "maker": "0", "rebate_eligible": True},
        "economics": {"taker": "0.01", "maker": "0", "rebate_eligible": True},
        "culture": {"taker": "0.01", "maker": "0", "rebate_eligible": True},
        "weather": {"taker": "0.01", "maker": "0", "rebate_eligible": True},
        "mentions": {"taker": "0.01", "maker": "0", "rebate_eligible": True},
        "geopolitics": {"taker": "0", "maker": "0", "rebate_eligible": False},
        "combos": {"taker": "0.05", "maker": "0", "rebate_eligible": False},
        "other_general": {"taker": "0.05", "maker": "0", "rebate_eligible": True},
    },
}


class MakerEconomicsError(ValueError):
    """Structurally invalid input (negative size, price outside (0, 1), ...)."""


@dataclass(frozen=True)
class Fill:
    fill_id: str
    occurred_at: datetime
    price: Decimal
    shares: Decimal
    source: Literal["venue_confirmed", "modeled"]
    fee: Decimal | None  # absolute USDC maker fee; None = unknown
    mid_at_fill: Decimal | None = None
    # horizon seconds -> mid observed that long after the fill (None = unavailable)
    mid_after: dict[int, Decimal | None] = field(default_factory=dict)
    after_cancel_request: bool = False  # filled after the cancel was requested (race)


@dataclass(frozen=True)
class Rebate:
    status: Literal["confirmed", "unconfirmed", "not_applicable"]
    amount: Decimal = ZERO


@dataclass(frozen=True)
class Quote:
    quote_id: str
    token_id: str
    condition_id: str
    side: Literal["BUY", "SELL"]
    limit_price: Decimal
    shares: Decimal
    final_state: Literal[
        "resting", "filled", "partial", "cancelled", "expired", "rejected", "unknown"
    ]
    evidence_age_seconds: Decimal | None  # age of the book/trade evidence at last update
    fills: tuple[Fill, ...] = ()
    # Cashflow from closing inventory before settlement (sale proceeds, signed +in).
    exit_cashflow: Decimal | None = None
    # Token payout per share at settlement (0 or 1). None = unresolved.
    settlement_payout: Decimal | None = None
    holding_cost: Decimal | None = None  # inventory/hedge/settlement costs; None = unknown
    rebate: Rebate = Rebate("unconfirmed")
    queue_ahead_recorded: Decimal | None = None


def _check(quote: Quote) -> None:
    if not (0 < quote.limit_price < 1) or quote.shares <= 0:
        raise MakerEconomicsError(f"{quote.quote_id}: invalid price or size")
    for fill in quote.fills:
        if not (0 < fill.price < 1) or fill.shares <= 0:
            raise MakerEconomicsError(f"{fill.fill_id}: invalid fill price or size")
    if sum((f.shares for f in quote.fills), ZERO) > quote.shares:
        raise MakerEconomicsError(f"{quote.quote_id}: fills exceed quoted size")


def _sign(side: str) -> int:
    return 1 if side == "BUY" else -1


def _wmean(pairs: list[tuple[Decimal, Decimal]]) -> Decimal | None:
    weight = sum((w for w, _ in pairs), ZERO)
    return sum((w * v for w, v in pairs), ZERO) / weight if weight else None


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _quote_economics(quote: Quote, max_age: Decimal) -> dict[str, Any]:
    """Realized economics for one quote's venue-confirmed fills, or an abstention."""
    confirmed = [f for f in quote.fills if f.source == "venue_confirmed"]
    if not confirmed:
        return {"quote_id": quote.quote_id, "status": "no_confirmed_fill", "realized_pnl": None}
    reasons: list[str] = []
    if quote.evidence_age_seconds is None or quote.evidence_age_seconds > max_age:
        reasons.append("STALE_OR_MISSING_EVIDENCE")
    if any(f.fee is None for f in confirmed):
        reasons.append("UNKNOWN_MAKER_FEE")
    if quote.holding_cost is None:
        reasons.append("UNKNOWN_HOLDING_COST")
    sign = _sign(quote.side)
    inventory = sum((sign * f.shares for f in confirmed), ZERO)
    cashflow = sum((-sign * f.price * f.shares for f in confirmed), ZERO)
    if quote.exit_cashflow is not None:
        closing = quote.exit_cashflow
        unresolved = ZERO
    elif quote.settlement_payout is not None:
        closing = inventory * quote.settlement_payout
        unresolved = ZERO
    else:
        closing = None
        unresolved = inventory
        reasons.append("UNRESOLVED_INVENTORY")
    result: dict[str, Any] = {
        "quote_id": quote.quote_id,
        "status": "abstain" if reasons else "realized",
        "reasons": reasons,
        "fill_cashflow": str(cashflow),
        "closing_cashflow": _s(closing),
        "unrealized_inventory_shares": str(unresolved),
        "rebate_status": quote.rebate.status,
        "realized_pnl": None,
    }
    if not reasons and closing is not None and quote.holding_cost is not None:
        rebate = quote.rebate.amount if quote.rebate.status == "confirmed" else ZERO
        fees = sum((f.fee for f in confirmed if f.fee is not None), ZERO)
        result["maker_fees"] = str(fees)
        result["rebate_counted"] = str(rebate)
        result["realized_pnl"] = str(cashflow + closing - fees + rebate - quote.holding_cost)
    return result


def _fill_stats(quotes: list[Quote], source: str, horizons: tuple[int, ...]) -> dict[str, Any]:
    spread: list[tuple[Decimal, Decimal]] = []
    marks: dict[int, list[tuple[Decimal, Decimal]]] = {h: [] for h in horizons}
    adverse = adverse_n = no_mid = 0
    cancel_race = 0
    cancel_race_loss = ZERO
    ref = horizons[min(1, len(horizons) - 1)]  # the middle horizon defines adverse selection
    for quote in quotes:
        sign = _sign(quote.side)
        for fill in (f for f in quote.fills if f.source == source):
            if fill.mid_at_fill is None:
                no_mid += 1
            else:
                spread.append((fill.shares, 2 * sign * (fill.mid_at_fill - fill.price)))
            for h in horizons:
                mid = fill.mid_after.get(h)
                if mid is not None:
                    marks[h].append((fill.shares, sign * (mid - fill.price)))
            ref_mid = fill.mid_after.get(ref)
            if ref_mid is not None:
                adverse_n += 1
                adverse += sign * (ref_mid - fill.price) < 0
            if fill.after_cancel_request:
                cancel_race += 1
                loss_mid = fill.mid_after.get(horizons[-1])
                if loss_mid is not None:
                    cancel_race_loss += fill.shares * min(ZERO, sign * (loss_mid - fill.price))
    return {
        "fills": sum(1 for q in quotes for f in q.fills if f.source == source),
        "shares": str(sum((f.shares for q in quotes for f in q.fills if f.source == source), ZERO)),
        "effective_spread_fill_weighted": _s(_wmean(spread)),
        "effective_spread_samples": len(spread),
        "fills_without_mid": no_mid,
        "markouts": {
            str(h): {"mean_per_share": _s(_wmean(marks[h])), "samples": len(marks[h])}
            for h in horizons
        },
        "adverse_selection_horizon_seconds": ref,
        "adverse_selection_rate": (
            str(Decimal(adverse) / Decimal(adverse_n)) if adverse_n else None
        ),
        "adverse_selection_samples": adverse_n,
        "cancel_race_fills": cancel_race,
        "cancel_race_markout_loss": str(cancel_race_loss),
    }


def analyze(
    quotes: list[Quote],
    *,
    as_of: datetime,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    max_evidence_age_seconds: Decimal = Decimal("30"),
    min_fills_for_recommendation: int = 30,
    markout_pause_threshold: Decimal = Decimal("0.005"),
    source_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Aggregate maker economics. Deterministic: same input gives the same output and hash."""
    if as_of.tzinfo is None:
        raise MakerEconomicsError("as_of must be timezone-aware UTC")
    if not horizons or any(h <= 0 for h in horizons):
        raise MakerEconomicsError("horizons must be positive seconds")
    for quote in quotes:
        _check(quote)
    per_quote = [_quote_economics(q, max_evidence_age_seconds) for q in quotes]
    realized = [r for r in per_quote if r["status"] == "realized"]
    abstained = [r for r in per_quote if r["status"] == "abstain"]
    pnl = sum((Decimal(r["realized_pnl"]) for r in realized), ZERO)

    resting_like = ("cancelled", "expired", "rejected", "resting")
    nonfills = [q for q in quotes if not q.fills and q.final_state in resting_like]
    live_stats = _fill_stats(quotes, "venue_confirmed", horizons)
    modeled_stats = _fill_stats(quotes, "modeled", horizons)

    ref = str(live_stats["adverse_selection_horizon_seconds"])
    live_mark = live_stats["markouts"][ref]
    mean = live_mark["mean_per_share"]
    if live_stats["fills"] < min_fills_for_recommendation or mean is None:
        recommendation = "INSUFFICIENT_CONFIRMED_FILLS"
    elif Decimal(mean) < -markout_pause_threshold:
        recommendation = "PAUSE_QUOTING"
    else:
        recommendation = "CONTINUE_SHADOW"

    body = {
        "schema_version": SKILL_VERSION,
        "execution_eligible": False,
        "evidence_status": "implemented_detector_not_validated",
        "as_of": as_of.isoformat(),
        "source_ids": list(source_ids),
        "condition_ids": sorted({q.condition_id for q in quotes}),
        "token_ids": sorted({q.token_id for q in quotes}),
        "fee_table_version": FEE_TABLE["version"],
        "horizons_seconds": list(horizons),
        "quotes": len(quotes),
        "realized": {
            "quotes_counted": len(realized),
            "pnl_usdc": str(pnl) if realized else None,
            "abstained_quotes": len(abstained),
            "abstain_reasons": sorted({x for r in abstained for x in r["reasons"]}),
            "unrealized_inventory_shares": str(
                sum((Decimal(r["unrealized_inventory_shares"]) for r in abstained), ZERO)
            ),
            "per_quote": per_quote,
        },
        "venue_confirmed": live_stats,
        "counterfactual_modeled": {
            **modeled_stats,
            "label": "modeled fills: never realized, never summed into realized pnl",
        },
        "nonfills": {
            "quotes": len(nonfills),
            "quoted_notional_usdc": str(sum((q.limit_price * q.shares for q in nonfills), ZERO)),
            "missed_fill_value": None,
            "missed_fill_value_reason": "not observable without venue queue data",
        },
        "queue_assumption": "none; queue position appears only where recorded",
        "rebates": {
            "counted": str(sum((Decimal(r.get("rebate_counted", "0")) for r in realized), ZERO)),
            "unconfirmed_or_not_applicable_quotes": sum(
                1 for q in quotes if q.rebate.status != "confirmed"
            ),
        },
        "kill_switch_recommendation": recommendation,
    }
    body["report_hash"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return body
