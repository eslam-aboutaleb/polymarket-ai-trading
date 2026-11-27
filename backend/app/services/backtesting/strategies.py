"""Declarative strategy deciders for the API. No user code is ever executed."""

from decimal import Decimal
from typing import Literal

from app.services.backtesting.contracts import Frozen, OrderIntent
from app.services.backtesting.event_replay_engine import Decider, DecisionContext


class StrategySpec(Frozen):
    name: Literal["follow_signals"] = "follow_signals"
    # Follower's own size. The leader's size and P&L never size or score a copy.
    follower_notional_usdc: Decimal
    version: str = "follow-signals-v1"


def build_decider(spec: StrategySpec) -> Decider:
    notional = spec.follower_notional_usdc

    def decide(context: DecisionContext) -> list:
        event = context.event
        if event.kind != "SIGNAL":
            return []
        return [
            OrderIntent(
                intent_id=f"copy-{event.event_id}",
                token_id=event.token_id,
                side=event.side,
                amount=notional,
                units="USDC",
                limit_price=event.limit_price,
            )
        ]

    return decide
