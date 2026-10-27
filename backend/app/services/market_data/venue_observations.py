"""Immutable receipt-time archive, not an inferred order-book reconstruction."""

import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy.exc import IntegrityError

from app.models.market_data.venue_observation import (
    ObservationDataset,
    ObservationDatasetMember,
    VenueObservation,
)
from app.services.market_data.replay_capture import aware, digest

SOURCE = "polymarket-clob-ws"
REST_SOURCE = "polymarket-native-rest"
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
    if kind == "source_status":
        value["status"] = raw.get("status")
    if (
        kind == "book"
        and value.get("bids")
        and value.get("asks")
        and max(Decimal(v["price"]) for v in value["bids"])
        >= min(Decimal(v["price"]) for v in value["asks"])
    ):
        flags.append("CROSSED_BOOK")
    return value, flags


def ensure_transaction(db):
    # pysqlite does not begin a transaction for SELECT. A SAVEPOINT must not
    # become the outer transaction and commit before the caller commits.
    connection = db.connection()
    if (
        connection.dialect.name == "sqlite"
        and not connection.connection.driver_connection.in_transaction
    ):
        connection.exec_driver_sql("BEGIN")


def append_event(db, raw, *, received_at, connection_id, source=SOURCE):
    """Caller owns transaction. Duplicate identity preserves first committed receipt."""
    if source not in (SOURCE, REST_SOURCE):
        raise ValueError("unsupported capture source")
    ensure_transaction(db)
    received_at = aware(received_at)
    token = str(raw.get("asset_id") or raw.get("token_id") or "")
    payload = json.dumps(raw, sort_keys=True, allow_nan=False)
    content = digest(raw)
    identity = digest([source, token, content])
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
        source=source,
        connection_id=connection_id,
        token_id=token,
        condition_id=raw.get("market"),
        kind=normalized["kind"],
        sequence=str(raw["sequence"]) if raw.get("sequence") is not None else None,
        event_at=event_at,
        received_at=received_at,
        stored_at=datetime.now(UTC),
        content_hash=content,
        payload_json=payload,
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
    raw = json.loads(row.payload_json)
    normalized, _ = normalize(raw)
    if (
        row.source not in (SOURCE, REST_SOURCE)
        or digest(raw) != row.content_hash
        or digest([row.source, row.token_id, row.content_hash]) != row.id
        or normalized != json.loads(row.normalized_json)
    ):
        raise ValueError("observation integrity mismatch")
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


def history(db, token_id, as_of, limit=100, before=None):
    from sqlalchemy import and_, or_

    query = db.query(VenueObservation)
    if before:
        cursor = db.get(VenueObservation, before)
        if (
            cursor is None
            or cursor.token_id != token_id
            or aware(cursor.received_at) > aware(as_of)
        ):
            raise ValueError("invalid observation cursor")
        query = query.filter(
            or_(
                VenueObservation.received_at < cursor.received_at,
                and_(
                    VenueObservation.received_at == cursor.received_at,
                    VenueObservation.id < cursor.id,
                ),
            )
        )
    return (
        query.filter(
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


def freeze_dataset(db, token_id, as_of, limit=100, snapshot_ids=None):
    ensure_transaction(db)
    if snapshot_ids is None:
        rows = list(reversed(history(db, token_id, as_of, limit)))
    else:
        if not 1 <= len(snapshot_ids) <= 100 or len(set(snapshot_ids)) != len(snapshot_ids):
            raise ValueError("invalid snapshot selection")
        rows = (
            db.query(VenueObservation)
            .filter(
                VenueObservation.id.in_(snapshot_ids),
                VenueObservation.token_id == token_id,
                VenueObservation.received_at <= aware(as_of),
            )
            .order_by(VenueObservation.received_at, VenueObservation.id)
            .all()
        )
        if len(rows) != len(snapshot_ids):
            raise ValueError("selected snapshots unavailable at cutoff")
    manifest = {
        "version": VERSION,
        "token_id": token_id,
        "as_of": aware(as_of).isoformat(),
        "quality": "INSUFFICIENT_DATA",
        "rights_scope": "public endpoint; reuse permissions require operator verification",
        "source_contract": "polymarket-clob-market-channel",
        "selection": "explicit_ids" if snapshot_ids is not None else "receipt_window",
        "truncated": len(rows) == limit if snapshot_ids is None else False,
        "observations": [
            {
                "snapshot_id": row.id,
                "content_hash": serialize(row)["content_hash"],
                "receipt_hash": digest(serialize(row)),
            }
            for row in rows
        ],
    }
    identity = digest(manifest)
    if db.get(ObservationDataset, identity) is None:
        try:
            with db.begin_nested():
                db.add(
                    ObservationDataset(
                        id=identity,
                        manifest_json=json.dumps(manifest, sort_keys=True),
                        created_at=datetime.now(UTC),
                    )
                )
                for row in rows:
                    db.add(ObservationDatasetMember(dataset_id=identity, observation_id=row.id))
                db.flush()
        except IntegrityError:
            if db.get(ObservationDataset, identity) is None:
                raise
    return {"dataset_id": identity, **manifest}


def replay_dataset(db, identity):
    dataset = db.get(ObservationDataset, identity)
    if dataset is None:
        raise LookupError("dataset unavailable")
    manifest = json.loads(dataset.manifest_json)
    if digest(manifest) != identity:
        raise ValueError("dataset integrity mismatch")
    observations = []
    for reference in manifest["observations"]:
        row = db.get(VenueObservation, reference["snapshot_id"])
        if row is None or row.content_hash != reference["content_hash"]:
            raise ValueError("dataset observation unavailable")
        value = serialize(row)
        if digest(value) != reference["receipt_hash"]:
            raise ValueError("dataset receipt integrity mismatch")
        if aware(row.received_at) > datetime.fromisoformat(manifest["as_of"]):
            raise ValueError("dataset observation after cutoff")
        observations.append(value)
    return {"dataset_id": identity, "manifest": manifest, "observations": observations}


def prune_unpinned(db, before, limit=1000):
    """Explicit operator action; frozen evidence is never deleted by retention."""
    pinned = (
        db.query(ObservationDatasetMember.observation_id)
        .filter(ObservationDatasetMember.observation_id == VenueObservation.id)
        .exists()
    )
    # Resting/fill audit references also pin their depth and rule receipts.
    from sqlalchemy import inspect, or_

    from app.models.trading.paper_maker_order import PaperMakerOrder

    if inspect(db.connection()).has_table("paper_maker_orders"):
        maker_pinned = (
            db.query(PaperMakerOrder.id)
            .filter(
                or_(
                    PaperMakerOrder.source_snapshot_id == VenueObservation.id,
                    PaperMakerOrder.metadata_snapshot_id == VenueObservation.id,
                )
            )
            .exists()
        )
        pinned = or_(pinned, maker_pinned)
    rows = (
        db.query(VenueObservation)
        .filter(
            VenueObservation.received_at < aware(before),
            ~pinned,
        )
        .order_by(VenueObservation.received_at, VenueObservation.id)
        .limit(limit)
        .all()
    )
    for row in rows:
        db.delete(row)
    return len(rows)
