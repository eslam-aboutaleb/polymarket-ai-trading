"""Immutable receipt-time archive, not an inferred order-book reconstruction."""

import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy.exc import IntegrityError

from app.models.market_data.venue_observation import VenueObservation
from app.services.market_data.replay_capture import aware, digest

SOURCE = "polymarket-clob-ws"
VERSION = "venue-observation-v1"


def normalize(raw):
    kind = str(raw.get("event_type") or raw.get("type") or "unknown")
    flags = ["CONTINUITY_UNVERIFIED"]
    value = {"version": VERSION, "kind": kind}
    try:
        for side in ("bids", "asks") if kind == "book" else ():
            levels = raw.get(side)
            if not isinstance(levels, list) or not levels or len(levels) > 1000:
                raise ValueError("missing depth")
            parsed = []
            for level in levels:
                price, size = Decimal(str(level["price"])), Decimal(str(level["size"]))
                if not price.is_finite() or not size.is_finite() or not 0 < price <= 1 or size < 0:
                    raise ValueError("invalid depth")
                parsed.append({"price": str(price), "shares": str(size)})
            value[side] = parsed
        if kind != "book":
            flags.append("INSUFFICIENT_DEPTH")
            if kind == "last_trade_price":
                for field in ("price", "size"):
                    number = Decimal(str(raw[field]))
                    if not number.is_finite() or number < 0:
                        raise ValueError("invalid trade")
                    value[field] = str(number)
                value["side"] = raw.get("side")
                flags.append("AGGRESSOR_SEMANTICS_UNVERIFIED")
    except (KeyError, TypeError, ValueError, InvalidOperation):
        value = {"version": VERSION, "kind": kind}
        flags.append("INVALID_OR_MISSING_DEPTH")
    return value, flags


def append_event(db, raw, *, received_at, connection_id):
    """Caller owns transaction. Duplicate identity preserves first committed receipt."""
    received_at = aware(received_at)
    token = str(raw.get("asset_id") or raw.get("token_id") or "")
    content = digest(raw)
    identity = digest([SOURCE, token, content])
    existing = db.get(VenueObservation, identity)
    if existing:
        return existing
    normalized, flags = normalize(raw)
    previous = (
        db.query(VenueObservation)
        .filter_by(token_id=token)
        .order_by(VenueObservation.received_at.desc(), VenueObservation.id.desc())
        .first()
    )
    if previous:
        if previous.connection_id != connection_id:
            flags.append("CONNECTION_CHANGED_GAP_POSSIBLE")
        seq = raw.get("sequence")
        if seq is not None and previous.sequence is not None:
            try:
                delta = int(seq) - int(previous.sequence)
                if delta > 1:
                    flags.append("SEQUENCE_GAP")
                elif delta <= 0:
                    flags.append("SEQUENCE_REORDER_OR_CONFLICT")
            except (ValueError, TypeError):
                flags.append("SEQUENCE_NOT_ORDERABLE")

    try:
        event_at = datetime.fromtimestamp(int(raw["timestamp"]) / 1000, UTC)
        if event_at > received_at:
            flags.append("SOURCE_CLOCK_AHEAD")
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        event_at = None
        flags.append("SOURCE_CLOCK_UNKNOWN")
    if previous and event_at and previous.event_at and event_at < aware(previous.event_at):
        flags.append("LATE_SOURCE_EVENT")
    row = VenueObservation(
        id=identity,
        source=SOURCE,
        connection_id=connection_id,
        token_id=token,
        condition_id=raw.get("market"),
        kind=normalized["kind"],
        sequence=str(raw["sequence"]) if raw.get("sequence") is not None else None,
        event_at=event_at,
        received_at=received_at,
        stored_at=datetime.now(UTC),
        content_hash=content,
        payload_json=json.dumps(raw, sort_keys=True),
        normalized_json=json.dumps(normalized, sort_keys=True),
        quality_json=json.dumps(flags),
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        existing = db.get(VenueObservation, identity)
        if existing is None:
            raise
        return existing
    return row


def serialize(row):
    return {
        "snapshot_id": row.id,
        "source": row.source,
        "token_id": row.token_id,
        "condition_id": row.condition_id,
        "connection_id": row.connection_id,
        "event_at": aware(row.event_at).isoformat() if row.event_at else None,
        "received_at": aware(row.received_at).isoformat(),
        "stored_at": aware(row.stored_at).isoformat(),
        "sequence": row.sequence,
        "content_hash": row.content_hash,
        "quality": json.loads(row.quality_json),
        "normalized": json.loads(row.normalized_json),
        "raw": json.loads(row.payload_json),
    }


def history(db, token_id, as_of, limit=100):
    return (
        db.query(VenueObservation)
        .filter(
            VenueObservation.token_id == token_id,
            VenueObservation.received_at <= aware(as_of),
        )
        .order_by(VenueObservation.received_at.desc(), VenueObservation.id.desc())
        .limit(limit)
        .all()
    )


def persist_message(raw, received_at, connection_id):
    """Own session off the event loop. An unavailable archive never becomes a fill."""
    from app.utils.database import SessionLocal

    with SessionLocal.begin() as db:
        append_event(db, raw, received_at=received_at, connection_id=connection_id)
        if raw.get("event_type") == "price_change":
            changes = raw.get("price_changes", [])
            if not isinstance(changes, list) or len(changes) > 1000:
                raise ValueError("invalid price change batch")
            for token in {str(c.get("asset_id", "")) for c in changes if isinstance(c, dict)}:
                if token:
                    append_event(
                        db,
                        {**raw, "asset_id": token},
                        received_at=received_at,
                        connection_id=connection_id,
                    )
