"""Paired leader/follower replay of one copy-trade signal (issue #6).

The leader's own fill and PnL are context only. The follower's result comes
from replaying the signal through the existing paper ledger
(``replay.follower``) at the follower's delay and size, on captured depth.
A delayed fill therefore changes the result, a size the book cannot fill is
flagged ``unfilled`` or ``partial``, and the leader's PnL is never copied.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Annotated, Any

from pydantic import AwareDatetime, Field

from app.services.analytics.replay.follower import (
    Book,
    FollowerExperiment,
    FrozenInput,
    Identifier,
    Price,
    replay,
)

MODEL_VERSION = "follower-paired-v1"


class PairedLeaderTrade(FrozenInput):
    trade_id: Identifier
    wallet: str = Field(pattern=r"^0x[a-fA-F0-9]{40}$")
    token_id: Identifier
    market_id: Identifier
    category: str = Field(default="uncategorized", min_length=1, max_length=60)
    leader_price: Price
    first_seen_at: AwareDatetime
    source_at: AwareDatetime | None = None
    # Context only. Never feeds the follower result.
    leader_realized_pnl: Annotated[Decimal, Field(max_digits=24, decimal_places=8)] | None = None


def follow_trade(
    trade: PairedLeaderTrade,
    books: list[Book],
    *,
    delay_seconds: int,
    notional: Decimal,
    fee_bps: Decimal,
    settlement_payout: Decimal | None = None,
    settlement_at: AwareDatetime | None = None,
    funding: Decimal = Decimal(1000),
) -> dict[str, Any]:
    """Replay one BUY signal for a follower. Entries only; exits are a separate signal."""
    leader = {
        "price": str(trade.leader_price),
        "realized_pnl_context_only": (
            None if trade.leader_realized_pnl is None else str(trade.leader_realized_pnl)
        ),
    }
    base = {
        "model_version": MODEL_VERSION,
        "trade_id": trade.trade_id,
        "wallet": trade.wallet.lower(),
        "delay_seconds": delay_seconds,
        "leader": leader,
    }
    if trade.source_at is None:
        return {
            **base,
            "follower": {"status": "non_tradable", "reason": "missing_source_timestamp"},
            "follower_pnl": None,
        }
    own = [b for b in books if b.token_id == trade.token_id]
    horizon = max(
        [trade.first_seen_at + timedelta(hours=1)]
        + [b.observed_at for b in own]
        + ([settlement_at] if settlement_at else [])
    )
    dataset: dict[str, Any] = {
        "source": "paired copy-trade replay",
        "signals": [
            {
                "event_id": trade.trade_id,
                "token_id": trade.token_id,
                "first_seen_at": trade.first_seen_at,
                "side": "BUY",
                "source_at": trade.source_at,
            }
        ],
        "books": [b.model_dump() for b in own[:250]],
        "settlements": [],
    }
    if settlement_payout is not None and settlement_at is not None:
        dataset["settlements"].append(
            {
                "event_id": trade.trade_id,
                "token_id": trade.token_id,
                "observed_at": settlement_at,
                "payout": settlement_payout,
            }
        )
    experiment = FollowerExperiment.model_validate(
        {
            "wallet": trade.wallet,
            "start_at": trade.first_seen_at,
            "end_at": horizon,
            "funding": funding,
            "order_notional": notional,
            "fee_bps": fee_bps,
            "delays_seconds": [delay_seconds],
            "dataset": dataset,
        }
    )
    scenario = replay(experiment)["scenarios"][0]
    outcome = scenario["outcomes"][0]
    filled = outcome["status"] in ("filled", "partial")
    pnl = scenario["valuation"]["net_pnl"] if filled else None
    follower: dict[str, Any] = {
        "status": outcome["status"],
        "reason": outcome.get("reason"),
        "shares": outcome["shares"],
        "pnl_basis": "settled" if settlement_payout is not None else "marked_indicative",
    }
    if filled:
        follower["vwap"] = outcome["vwap"]
        follower["slippage_vs_leader"] = str(Decimal(outcome["vwap"]) - trade.leader_price)
        follower["notional"] = outcome["notional"]
    return {**base, "follower": follower, "follower_pnl": pnl}
