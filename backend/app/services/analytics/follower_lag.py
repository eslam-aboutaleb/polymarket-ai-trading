"""Follower lag: what a delayed follower pays versus the leader (issue #158).

For each leader trade the follower is simulated at a delay ``d``: the first
captured book at or after ``first_seen_at + d`` is walked at the follower's own
size, so the fill price, slippage and unfilled outcomes come from the book, never
from the leader's fill. The follower's return is derived from that fill and the
token's settlement payout, never from the leader's PnL.

Also holds the configurable skip rule: do not copy once the price has moved
against the follower by more than a fraction of the estimated edge.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from statistics import median
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.services.analytics.replay.follower import Book, Identifier, Price
from app.services.analytics.replay.ledger import ACCOUNTING_CONTEXT

MODEL_VERSION = "follower-lag-v1"
ZERO = Decimal(0)


class LeaderTrade(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    trade_id: Identifier
    token_id: Identifier
    side: Literal["BUY", "SELL"]
    leader_price: Price
    first_seen_at: AwareDatetime
    payout: Annotated[Decimal, Field(ge=0, le=1, max_digits=9, decimal_places=8)] | None = None


class LagSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    order_notional: Annotated[
        Decimal, Field(gt=0, le=1_000_000, max_digits=18, decimal_places=8)
    ] = Decimal(10)
    fee_bps: Annotated[Decimal, Field(ge=0, le=10000)] = Decimal(0)
    max_book_age_seconds: int = Field(default=60, ge=0, le=3600)
    max_wait_seconds: int = Field(default=60, ge=0, le=3600)


def simulate_follower_fill(
    trade: LeaderTrade, books: list[Book], delay_seconds: int, settings: LagSettings
) -> dict[str, Any]:
    """One delayed follower fill from captured depth. Missing evidence stays unfilled."""
    row: dict[str, Any] = {
        "trade_id": trade.trade_id,
        "side": trade.side,
        "delay_seconds": delay_seconds,
        "leader_price": str(trade.leader_price),
        "status": "unfilled",
        "reason": None,
    }
    eligible = trade.first_seen_at + timedelta(seconds=delay_seconds)
    window_end = eligible + timedelta(seconds=settings.max_wait_seconds)
    candidates = sorted(
        (
            b
            for b in books
            if b.token_id == trade.token_id and eligible <= b.observed_at <= window_end
        ),
        key=lambda b: (b.observed_at, b.snapshot_id),
    )
    if not candidates:
        row["reason"] = "missing_eligible_book"
        return row
    book = candidates[0]
    row["snapshot_id"] = book.snapshot_id
    if (book.observed_at - book.source_at).total_seconds() > settings.max_book_age_seconds:
        row["reason"] = "stale_book"
        return row
    levels = book.asks if trade.side == "BUY" else book.bids
    with localcontext(ACCOUNTING_CONTEXT):
        remaining = settings.order_notional
        shares = ZERO
        cost = ZERO
        for level in levels:
            if remaining <= 0:
                break
            take = min(level.shares, remaining / level.price)
            if take <= 0:
                continue
            shares += take
            cost += take * level.price
            remaining -= take * level.price
        if shares == 0:
            row["reason"] = "missing_depth"
            return row
        vwap = cost / shares
        fee = cost * settings.fee_bps / 10000
        slippage = vwap - trade.leader_price if trade.side == "BUY" else trade.leader_price - vwap
        row.update(
            status="filled" if remaining <= ZERO else "partial",
            reason="modeled_depth_fill",
            shares=str(shares),
            vwap=str(vwap.quantize(Decimal("0.00000001"), ROUND_HALF_EVEN)),
            cost=str(cost),
            fee=str(fee),
            slippage=str(slippage.quantize(Decimal("0.00000001"), ROUND_HALF_EVEN)),
        )
        if trade.side == "BUY" and trade.payout is not None:
            pnl = shares * trade.payout - cost - fee
            row.update(follower_pnl=str(pnl), follower_return=str(pnl / (cost + fee)))
    return row


def lag_report(
    trades: list[LeaderTrade],
    books: list[Book],
    delays_seconds: list[int],
    settings: LagSettings | None = None,
) -> dict[str, Any]:
    """Per-delay median slippage and net follower return across leader trades."""
    settings = settings or LagSettings()
    seen: set[str] = set()
    unique = []
    for trade in trades:
        if trade.trade_id not in seen:
            seen.add(trade.trade_id)
            unique.append(trade)
    scenarios = []
    for delay in delays_seconds:
        rows = [simulate_follower_fill(t, books, delay, settings) for t in unique]
        filled = [r for r in rows if r["status"] != "unfilled"]
        scored = [r for r in filled if "follower_return" in r]
        pooled_cost = sum((Decimal(r["cost"]) + Decimal(r["fee"]) for r in scored), ZERO)
        pooled_pnl = sum((Decimal(r["follower_pnl"]) for r in scored), ZERO)
        reasons: dict[str, int] = {}
        for r in rows:
            if r["status"] == "unfilled":
                reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
        scenarios.append(
            {
                "delay_seconds": delay,
                "signals": len(rows),
                "filled": len(filled),
                "fill_rate": str(Decimal(len(filled)) / len(rows)) if rows else None,
                "unfilled_reasons": dict(sorted(reasons.items())),
                "median_slippage": (
                    str(median(Decimal(r["slippage"]) for r in filled)) if filled else None
                ),
                "settled_trades": len(scored),
                "median_net_follower_return": (
                    str(median(Decimal(r["follower_return"]) for r in scored)) if scored else None
                ),
                "pooled_net_follower_return": (
                    str(pooled_pnl / pooled_cost) if pooled_cost > 0 else None
                ),
                "trades": rows,
            }
        )
    return {
        "model_version": MODEL_VERSION,
        "quality": "modeled_from_supplied_books_not_exchange_fills",
        "scenarios": scenarios,
    }


def estimated_edge(shrunk_net_return: float | None, leader_price: Decimal) -> Decimal:
    """Edge per share: the trader's shrunk net return per dollar times the price paid."""
    if shrunk_net_return is None or shrunk_net_return <= 0:
        return ZERO
    return Decimal(str(shrunk_net_return)) * leader_price


def copy_skip_decision(
    *,
    side: str,
    leader_price: Decimal,
    current_price: Decimal,
    edge: Decimal,
    max_edge_fraction: Decimal,
) -> dict[str, Any]:
    """Skip when the adverse price move exceeds ``max_edge_fraction`` of the edge.

    A non-positive edge means there is nothing to protect, so the copy is skipped.
    A favourable move never causes a skip.
    """
    if side not in ("BUY", "SELL"):
        raise ValueError("side must be BUY or SELL")
    if max_edge_fraction < 0:
        raise ValueError("max_edge_fraction must be non-negative")
    adverse = current_price - leader_price if side == "BUY" else leader_price - current_price
    if edge <= 0:
        return {"skip": True, "reason": "no_estimated_edge", "adverse_move": str(adverse)}
    allowed = edge * max_edge_fraction
    if adverse > allowed:
        return {
            "skip": True,
            "reason": "price_moved_beyond_edge_fraction",
            "adverse_move": str(adverse),
            "allowed_move": str(allowed),
        }
    return {
        "skip": False,
        "reason": "within_edge_fraction",
        "adverse_move": str(adverse),
        "allowed_move": str(allowed),
    }
