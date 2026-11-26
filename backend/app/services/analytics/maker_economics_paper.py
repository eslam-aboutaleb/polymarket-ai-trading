"""Adapter: paper maker journal rows -> maker-economics quotes (issue #23).

Everything read here is modeled. Paper fills are always ``source="modeled"``, so
the report keeps realized P&L empty and shows them only as counterfactual. Marks
are the frozen indicative midpoint drift at 5/30/60 seconds; the mid at fill time
is not stored, so effective spread is reported as unavailable, not zero.
"""

import json
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.trading.market_maker_config import MarketMakerConfig
from app.models.trading.paper_maker_order import (
    PaperMakerFill,
    PaperMakerMark,
    PaperMakerOrder,
)
from app.services.analytics.maker_economics import Fill, Quote, analyze
from app.services.trading.paper_maker_lifecycle import MakerOrder
from app.utils.time import utc_now

PAPER_HORIZONS = (5, 30, 60)
STATE = {
    "CREATED": "resting",
    "RESTING": "resting",
    "CANCEL_REQUESTED": "resting",
    "PARTIAL": "partial",
    "FILLED": "filled",
    "CANCELLED": "cancelled",
    "EXPIRED": "expired",
    "UNKNOWN_DATA": "unknown",
}


def quotes_from_paper(db: Session, config: MarketMakerConfig, limit: int = 200) -> list[Quote]:
    rows = (
        db.query(PaperMakerOrder)
        .filter_by(config_id=config.id)
        .order_by(PaperMakerOrder.created_at.desc())
        .limit(limit)
        .all()
    )
    quotes = []
    for row in rows:
        order = MakerOrder.model_validate_json(row.payload_json)
        sign = 1 if order.side == "BUY" else -1
        fills = []
        for fill in db.query(PaperMakerFill).filter_by(order_id=row.id).all():
            body = json.loads(fill.payload_json)
            mids: dict[int, Decimal | None] = {}
            for mark in db.query(PaperMakerMark).filter_by(fill_id=fill.id).all():
                value = json.loads(mark.payload_json).get("markout_per_share")
                mids[mark.horizon_seconds] = (
                    None if value is None else order.limit_price + sign * Decimal(str(value))
                )
            fills.append(
                Fill(
                    fill_id=fill.id,
                    occurred_at=fill.observed_at,
                    price=Decimal(str(body.get("price", order.limit_price))),
                    shares=Decimal(str(body["shares"])),
                    source="modeled",
                    fee=Decimal(str(body["fee_usdc"])) if "fee_usdc" in body else None,
                    mid_after=mids,
                )
            )
        quotes.append(
            Quote(
                quote_id=row.id,
                token_id=order.token_id,
                condition_id=order.condition_id,
                side=order.side,
                limit_price=order.limit_price,
                shares=order.shares,
                final_state=STATE.get(row.state, "unknown"),
                evidence_age_seconds=None,
                fills=tuple(fills),
                queue_ahead_recorded=order.queue_ahead,
            )
        )
    return quotes


def economics_for_config(db: Session, config: MarketMakerConfig) -> dict:
    quotes = quotes_from_paper(db, config)
    return analyze(
        quotes,
        as_of=utc_now(),
        horizons=PAPER_HORIZONS,
        source_ids=tuple(sorted({q.quote_id for q in quotes}))[:50],
    )
