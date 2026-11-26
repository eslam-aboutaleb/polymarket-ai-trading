"""Read-only whale signal quality, from receipt-time evidence (issue #11).

Signals are informational until validated. Settlement captures are not stored, so
evaluations here use quote markouts and abstain from any trade suggestion.
"""

import json
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.routes.auth.auth import get_current_user_from_token
from app.models.market_data.replay_capture import ReplayCapture
from app.models.market_data.whale_event import WhaleEvent
from app.services.analytics.wallet_clustering import clusters_from_events
from app.services.intelligence.whale_signal_evaluator import Quote, WhaleSignal, evaluate
from app.utils.database import get_db

router = APIRouter(prefix="/api/v1/intelligence", tags=["whale signal quality"])
SIDES = {"buy": "BUY", "open": "BUY", "sell": "SELL", "close": "SELL"}
MAX_EVENTS = 2000


def _aware(moment: datetime) -> datetime:
    from datetime import UTC

    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _load(db: Session, wallet: str | None):
    query = db.query(WhaleEvent).order_by(WhaleEvent.detected_at, WhaleEvent.id)
    if wallet:
        query = query.filter(WhaleEvent.wallet == wallet.lower())
    rows = query.limit(MAX_EVENTS).all()
    signals, skipped = [], 0
    for row in rows:
        side = SIDES.get((row.side or "").lower())
        if side is None or not row.token_id:
            skipped += 1
            continue
        signals.append(
            WhaleSignal(
                event_id=f"{row.tx_hash}:{row.log_index}:{row.id}",
                wallet=row.wallet.lower(),
                market_id=row.market_id or row.token_id,
                token_id=row.token_id,
                side=side,
                event_time=_aware(row.block_ts) if row.block_ts else None,
                observed_at=_aware(row.detected_at),
            )
        )
    events = [
        {
            "wallet": r.wallet,
            "market_id": r.market_id,
            "side": r.side,
            "detected_at": _aware(r.detected_at),
            "notional": r.notional,
        }
        for r in rows
    ]
    tokens = sorted({s.token_id for s in signals})
    quotes: list[Quote] = []
    if tokens:
        captures = (
            db.query(ReplayCapture)
            .filter(ReplayCapture.kind == "book", ReplayCapture.token_id.in_(tokens))
            .order_by(ReplayCapture.observed_at)
            .limit(5000)
            .all()
        )
        for capture in captures:
            book = json.loads(capture.payload_json)["book"]
            quotes.append(
                Quote(
                    token_id=capture.token_id,
                    observed_at=_aware(capture.observed_at),
                    bid=Decimal(book["bids"][0]["price"]) if book.get("bids") else None,
                    ask=Decimal(book["asks"][0]["price"]) if book.get("asks") else None,
                )
            )
    return signals, quotes, events, skipped


def _report(db: Session, wallet: str | None, inverse: bool, split_at: datetime | None):
    signals, quotes, events, skipped = _load(db, wallet)
    if not signals:
        return {
            "informational_only": True,
            "trade_suggestion": None,
            "abstain_reasons": ["missing_evidence"],
            "events_considered": 0,
            "skipped_event_types": skipped,
        }
    clusters = clusters_from_events(events, window_hours=0, now=max(s.observed_at for s in signals))
    ordered = sorted(s.observed_at for s in signals)
    split = _aware(split_at) if split_at else ordered[len(ordered) // 2]
    result = evaluate(signals, quotes, [], clusters, split_at=split, inverse=inverse)
    result.update(events_considered=len(signals), skipped_event_types=skipped)
    return result


@router.get("/wallets/{wallet}/signal-quality")
def wallet_signal_quality(
    wallet: str,
    split_at: datetime | None = Query(None),
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    del current_user
    if not wallet.startswith("0x") or len(wallet) != 42:
        raise HTTPException(422, "wallet must be a 0x address")
    return _report(db, wallet, False, split_at)


@router.get("/inverse/validation")
def inverse_validation(
    split_at: datetime | None = Query(None),
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    del current_user
    return _report(db, None, True, split_at)
