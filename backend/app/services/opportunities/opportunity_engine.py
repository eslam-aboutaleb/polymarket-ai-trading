"""Executable net-edge opportunity evaluation and ranking (#5).

Pure and side-effect free. Pricing is the #7 estimator walking a #18 validated book;
this module only joins a forecast to that executable price and states what is and is
not verified. It never creates, signs or submits an order.
"""

import hashlib
import json
from datetime import timedelta
from decimal import Decimal, localcontext
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.services.analytics.replay.ledger import ACCOUNTING_CONTEXT
from app.services.market_data.binary_book import BinaryBook, Contract, MarketSpec
from app.services.trading.execution.execution_estimator import (
    EstimateRequest,
    estimate_execution,
)

VERSION = "opportunity-v1"
Status = Literal["INDICATIVE", "VERIFIED", "STALE", "INELIGIBLE"]
UNMODELED_COSTS = ["exit_haircut", "capital_lockup_cost", "resolution_and_settlement_risk"]


class ForecastInput(Contract):
    forecast_id: str = Field(min_length=1, max_length=80)
    condition_id: str = Field(min_length=1, max_length=200)
    source: Literal["ai_forecast", "copy_signal", "arbitrage"] = "ai_forecast"
    # Probability that the YES outcome resolves true. None or abstained is never ranked.
    yes_probability: Decimal | None = Field(default=None, ge=0, le=1)
    abstained: bool = False
    interval_low: Decimal | None = Field(default=None, ge=0, le=1)
    interval_high: Decimal | None = Field(default=None, ge=0, le=1)
    forecast_at: AwareDatetime
    calibrated: bool = False
    rule_hash: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def coherent(self):
        low, high, p = self.interval_low, self.interval_high, self.yes_probability
        if (low is None) != (high is None):
            raise ValueError("interval needs both bounds")
        if low is not None and (low > high or (p is not None and not low <= p <= high)):
            raise ValueError("probability outside its interval")
        return self


class CandidateRequest(Contract):
    outcome: Literal["YES", "NO"]
    shares: Decimal = Field(gt=0, le=1_000_000_000, decimal_places=8)
    fee_bps: Decimal | None = Field(default=None, ge=0, le=10000)
    fee_version: str | None = Field(default=None, min_length=1, max_length=80)
    max_forecast_age_seconds: int = Field(default=3600, ge=1, le=86400)


class Opportunity(Contract):
    version: Literal["opportunity-v1"] = VERSION
    opportunity_id: str | None = None
    market_id: str
    token_id: str
    side: Literal["BUY"] = "BUY"
    outcome: Literal["YES", "NO"]
    status: Status
    reasons: list[str] = Field(default_factory=list)
    snapshot_id: str | None = None
    forecast_id: str
    source: str
    size: Decimal
    executable_vwap: Decimal | None = None
    predicted_probability: Decimal | None = None
    uncertainty_interval: tuple[Decimal, Decimal] | None = None
    fees: Decimal | None = None
    slippage: Decimal | None = None
    capital_required: Decimal | None = None
    net_ev: Decimal | None = None
    net_ev_per_dollar: Decimal | None = None
    conservative_net_ev: Decimal | None = None
    components: dict[str, str | None] = Field(default_factory=dict)
    book_age_ms: int | None = None
    expiry: AwareDatetime | None = None
    unmodeled_costs: list[str] = Field(default_factory=lambda: list(UNMODELED_COSTS))
    mode: Literal["research_only_no_order"] = "research_only_no_order"
    guaranteed_profit: Literal[False] = False

    @model_validator(mode="after")
    def seal(self):
        if self.opportunity_id is None:
            body = self.model_dump(mode="json", exclude={"opportunity_id"})
            object.__setattr__(
                self,
                "opportunity_id",
                hashlib.sha256(
                    json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            )
        return self


def _out(spec, book, forecast, request, status, reasons, **extra):
    return Opportunity(
        market_id=spec.condition_id,
        token_id=book.token_id,
        outcome=request.outcome,
        status=status,
        reasons=reasons,
        forecast_id=forecast.forecast_id,
        source=forecast.source,
        size=request.shares,
        snapshot_id=book.book_hash,
        **extra,
    )


def evaluate_opportunity(
    spec: MarketSpec,
    book: BinaryBook,
    forecast: ForecastInput,
    request: CandidateRequest,
    now,
) -> Opportunity:
    """Binary-share EV = outcome probability - executable VWAP - per-share fee."""
    with localcontext(ACCOUNTING_CONTEXT):
        expected_token = spec.yes_token if request.outcome == "YES" else spec.no_token
        if forecast.condition_id != spec.condition_id or book.token_id != expected_token:
            return _out(
                spec, book, forecast, request, "INELIGIBLE", ["TOKEN_OR_CONDITION_MISMATCH"]
            )
        if forecast.abstained or forecast.yes_probability is None:
            return _out(spec, book, forecast, request, "INELIGIBLE", ["FORECAST_ABSTAINED"])
        if forecast.forecast_at > now:
            return _out(spec, book, forecast, request, "INELIGIBLE", ["FORECAST_FROM_FUTURE"])
        forecast_expiry = forecast.forecast_at + timedelta(seconds=request.max_forecast_age_seconds)
        if now > forecast_expiry:
            return _out(spec, book, forecast, request, "STALE", ["FORECAST_STALE"])
        if forecast.rule_hash is not None and forecast.rule_hash != spec.rule_hash:
            return _out(spec, book, forecast, request, "INELIGIBLE", ["RULE_HASH_MISMATCH"])
        estimate = estimate_execution(
            spec,
            book,
            EstimateRequest(
                order_intent_id=f"opp-{forecast.forecast_id}"[:80],
                mode="FOK",
                side="BUY",
                shares=request.shares,
                limit_price=1 - spec.tick,
                fee_bps=request.fee_bps,
                fee_version=request.fee_version,
            ),
            now,
        )
        if estimate.status != "modeled":
            stale = estimate.reason == "QUOTE_EXPIRED_OR_FROM_FUTURE" or book.grade == "STALE"
            return _out(
                spec,
                book,
                forecast,
                request,
                "STALE" if stale else "INELIGIBLE",
                [estimate.reason or "ESTIMATE_UNAVAILABLE"],
            )
        if estimate.fill_status != "FULL":
            return _out(
                spec, book, forecast, request, "INELIGIBLE", ["INSUFFICIENT_DISPLAYED_DEPTH"]
            )
        yes = forecast.yes_probability
        p = yes if request.outcome == "YES" else 1 - yes
        interval = None
        if forecast.interval_low is not None:
            lo, hi = forecast.interval_low, forecast.interval_high
            interval = (lo, hi) if request.outcome == "YES" else (1 - hi, 1 - lo)
        fee_ps = estimate.fees / request.shares
        net_ps = p - estimate.executable_vwap - fee_ps
        net_ev = net_ps * request.shares
        capital = estimate.net_cash
        conservative = (
            None
            if interval is None
            else (interval[0] - estimate.executable_vwap - fee_ps) * request.shares
        )
        reasons = []
        if net_ev <= 0:
            return _out(
                spec,
                book,
                forecast,
                request,
                "INELIGIBLE",
                ["NO_NET_EDGE_AFTER_EXECUTABLE_COST"],
                executable_vwap=estimate.executable_vwap,
                predicted_probability=p,
                fees=estimate.fees,
                capital_required=capital,
                net_ev=net_ev,
            )
        if interval is None:
            reasons.append("NO_UNCERTAINTY_INTERVAL")
        elif conservative <= 0:
            reasons.append("CONSERVATIVE_EDGE_NOT_POSITIVE")
        if not forecast.calibrated:
            reasons.append("FORECAST_NOT_CALIBRATED")
        if forecast.rule_hash is None:
            reasons.append("RULES_NOT_VERIFIED")
        status = "VERIFIED" if not reasons else "INDICATIVE"
        return _out(
            spec,
            book,
            forecast,
            request,
            status,
            reasons,
            executable_vwap=estimate.executable_vwap,
            predicted_probability=p,
            uncertainty_interval=interval,
            fees=estimate.fees,
            slippage=estimate.expected_price_impact,
            capital_required=capital,
            net_ev=net_ev,
            net_ev_per_dollar=net_ev / capital,
            conservative_net_ev=conservative,
            book_age_ms=estimate.book_age_ms,
            expiry=min(estimate.quote_expiry, forecast_expiry),
            components={
                "predicted_probability": str(p),
                "executable_vwap": str(estimate.executable_vwap),
                "fee_per_share": str(fee_ps),
                "slippage_vs_touch": str(estimate.expected_price_impact),
                "net_ev_per_share": str(net_ps),
            },
        )


def size_sensitivity(spec, book, forecast, request, sizes, now) -> list[Opportunity]:
    """Same candidate at several sizes: VWAP rises with size and may flip the verdict."""
    return [
        evaluate_opportunity(spec, book, forecast, request.model_copy(update={"shares": s}), now)
        for s in sizes
    ]


_TIER = {"VERIFIED": 0, "INDICATIVE": 1}


def rank_opportunities(opportunities: list[Opportunity], min_edge: Decimal | None = None):
    """Rank only VERIFIED/INDICATIVE rows; everything else is returned with its reasons.

    Order: status tier, net EV per dollar (desc), capital at risk (asc), book age (asc),
    id. The components stay visible on each row; there is no opaque composite score.
    """
    ranked, filtered = [], []
    for item in opportunities:
        eligible = item.status in _TIER and item.net_ev_per_dollar is not None
        if eligible and min_edge is not None and item.net_ev_per_dollar < min_edge:
            filtered.append((item, "BELOW_MIN_EDGE"))
        elif eligible:
            ranked.append(item)
        else:
            filtered.append((item, item.reasons[0] if item.reasons else item.status))
    ranked.sort(
        key=lambda o: (
            _TIER[o.status],
            -o.net_ev_per_dollar,
            o.capital_required,
            o.book_age_ms if o.book_age_ms is not None else 1 << 60,
            o.opportunity_id,
        )
    )
    return ranked, filtered
