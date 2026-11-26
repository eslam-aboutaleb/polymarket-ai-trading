"""Inventory-aware, volatility-aware quote policy (issue #8).

Pure and side-effect free: it turns an observed market state into a versioned
:class:`QuotePlan`. It never signs, submits or cancels anything. Order
submission stays behind ``live_trading_policy`` / ``pre_trade_gate`` and the
durable execution gateway; the plan only proposes the same order dictionaries
the band baseline already emits.

Model (all prices are YES-token prices in [0, 1]):

* reservation price ``r = mid - risk_coefficient * inventory`` (long YES lowers
  both quotes so the next fill is more likely a sale; short raises them);
* half-spread of band ``i`` = ``min_spread + step * i + vol_widening * sigma``
  where ``sigma`` is the realized short-term midpoint volatility;
* quotes never cross the midpoint by more than one tick, so inventory skew
  cannot create a marketable (taker) order from a ``post_only`` quote;
* a side that would add inventory beyond ``max_inventory_shares`` is dropped;
* no quotes from a stale or missing book;
* worst-case exposure counts the old resting orders PLUS the replacement set,
  because a cancel is not proven until confirmed.

Maker rebates are counted only when the caller marks them confirmed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

POLICY_VERSION = "maker-quote-policy-v1"
ZERO = Decimal(0)


def _d(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


@dataclass(frozen=True)
class PolicyParams:
    """Tunable policy inputs. Defaults are conservative and shadow-only."""

    min_spread: Decimal
    max_spread: Decimal
    num_bands: int
    band_order_size: Decimal
    max_collateral: Decimal
    min_price: Decimal = Decimal("0.01")
    max_price: Decimal = Decimal("0.99")
    tick_size: Decimal = Decimal("0.01")
    risk_coefficient: Decimal = Decimal("0.0005")  # price shift per share of inventory
    max_inventory_shares: Decimal = Decimal("200")  # per-market cap, either direction
    vol_widening: Decimal = Decimal("1")  # spread added per unit of realized sigma
    max_book_age_seconds: Decimal = Decimal("10")
    rebate_confirmed: bool = False
    rebate_per_share: Decimal = ZERO


@dataclass(frozen=True)
class MarketState:
    """Observed inputs. ``None`` means unknown, never zero."""

    midpoint: Decimal | None
    inventory_shares: Decimal = ZERO  # signed YES-token inventory
    realized_sigma: Decimal | None = None
    book_age_seconds: Decimal | None = None
    resting_notional: Decimal = ZERO  # old orders that may still be live


@dataclass
class QuotePlan:
    version: str
    plan_hash: str
    shadow_only: bool
    abstained: bool
    reason_codes: list[str]
    reservation_price: str | None
    spread_add_on: str | None
    orders: list[dict[str, Any]]
    worst_case_exposure: str
    max_collateral: str
    expected_net_spread: str | None
    rebate_included: bool
    inputs: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _quant(value: Decimal, tick: Decimal, *, up: bool) -> Decimal:
    steps = (value / tick).to_integral_value(ROUND_CEILING if up else ROUND_FLOOR)
    return steps * tick


def _canonical(params: PolicyParams, state: MarketState) -> dict[str, Any]:
    def encode(obj: Any) -> dict[str, Any]:
        return {k: (str(v) if v is not None else None) for k, v in asdict(obj).items()}

    return {"version": POLICY_VERSION, "params": encode(params), "state": encode(state)}


def _hash(payload: dict[str, Any], orders: list[dict[str, Any]], reasons: list[str]) -> str:
    body = json.dumps(
        {"inputs": payload, "orders": orders, "reasons": reasons},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode()).hexdigest()


def _abstain(
    params: PolicyParams, state: MarketState, payload: dict[str, Any], reasons: list[str]
) -> QuotePlan:
    return QuotePlan(
        version=POLICY_VERSION,
        plan_hash=_hash(payload, [], reasons),
        shadow_only=True,
        abstained=True,
        reason_codes=reasons,
        reservation_price=None,
        spread_add_on=None,
        orders=[],
        worst_case_exposure=str(state.resting_notional),
        max_collateral=str(params.max_collateral),
        expected_net_spread=None,
        rebate_included=False,
        inputs=payload,
    )


def build_quote_plan(params: PolicyParams, state: MarketState, token_id: str) -> QuotePlan:
    """Return a deterministic quote plan, or an abstention with reason codes."""
    payload = _canonical(params, state)
    mid = state.midpoint
    if mid is None or mid <= 0 or mid >= 1:
        return _abstain(params, state, payload, ["NO_MIDPOINT"])
    # A missing age is unknown evidence, not a fresh book.
    if state.book_age_seconds is None or state.book_age_seconds > params.max_book_age_seconds:
        return _abstain(params, state, payload, ["STALE_BOOK"])
    if params.num_bands < 1:
        return _abstain(params, state, payload, ["NO_BANDS"])

    reasons: list[str] = []
    inventory = state.inventory_shares
    reservation = mid - params.risk_coefficient * inventory
    if inventory != 0:
        reasons.append("INVENTORY_SKEW")
    sigma = state.realized_sigma
    if sigma is None:
        add_on = ZERO
        reasons.append("VOL_UNKNOWN")
    else:
        add_on = max(ZERO, params.vol_widening * sigma)
        if add_on > 0:
            reasons.append("VOL_WIDENED")

    step = (
        (params.max_spread - params.min_spread) / (params.num_bands - 1)
        if params.num_bands > 1
        else ZERO
    )
    no_buy = inventory >= params.max_inventory_shares
    no_sell = inventory <= -params.max_inventory_shares
    if no_buy:
        reasons.append("INVENTORY_CAP_LONG")
    if no_sell:
        reasons.append("INVENTORY_CAP_SHORT")

    tick = params.tick_size
    # Old resting orders count first: their cancel is unconfirmed until proven.
    exposure = state.resting_notional
    orders: list[dict[str, Any]] = []
    trimmed = False

    def add(side: str, price: Decimal, band: int) -> None:
        nonlocal exposure, trimmed
        if exposure + params.band_order_size > params.max_collateral:
            trimmed = True
            return
        exposure += params.band_order_size
        orders.append(
            {
                "side": side,
                "price": float(price),
                "size": float(params.band_order_size),
                "token_id": token_id,
                "band": band,
                "policy": POLICY_VERSION,
            }
        )

    for band in range(params.num_bands):
        half = params.min_spread + step * band + add_on
        bid = _quant(min(reservation - half, mid - tick), tick, up=False)
        ask = _quant(max(reservation + half, mid + tick), tick, up=True)
        if not no_buy and params.min_price <= bid < mid:
            add("BUY", bid, band)
        if not no_sell and mid < ask <= params.max_price:
            add("SELL", ask, band)
    if trimmed:
        reasons.append("EXPOSURE_TRIMMED")
    if not orders:
        reasons.append("NO_QUOTES")

    net = None
    if orders:
        bids = [o["price"] for o in orders if o["side"] == "BUY"]
        asks = [o["price"] for o in orders if o["side"] == "SELL"]
        if bids and asks:
            # Inside pair spread. Maker fee is zero under taker-only fees; a
            # rebate is added only when the caller confirmed eligibility.
            net = _d(min(asks)) - _d(max(bids))
            if params.rebate_confirmed:
                net += 2 * params.rebate_per_share
    return QuotePlan(
        version=POLICY_VERSION,
        plan_hash=_hash(payload, orders, reasons),
        shadow_only=True,
        abstained=not orders,
        reason_codes=reasons,
        reservation_price=str(reservation),
        spread_add_on=str(add_on),
        orders=orders,
        worst_case_exposure=str(exposure),
        max_collateral=str(params.max_collateral),
        expected_net_spread=None if net is None else str(net),
        rebate_included=bool(params.rebate_confirmed and net is not None),
        inputs=payload,
    )
