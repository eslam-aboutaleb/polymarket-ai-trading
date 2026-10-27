"""Bounded native capture. Receipt clocks never become historical trade clocks."""

import hashlib
import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.market_data.replay_capture import ReplayCapture
from app.services.analytics.replay.follower import Book, Dataset, Level, Signal
from app.services.analytics.replay.ledger import ACCOUNTING_CONTEXT
from app.services.intelligence.market_context import build_market_context
from app.services.market_data.polymarket_service import (
    POLYMARKET_CLOB_API,
    POLYMARKET_DATA_API,
    POLYMARKET_GAMMA_API,
)
from app.utils.time import utc_now


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def aware(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def native_active(clob: dict, gamma: dict) -> bool:
    return (
        clob.get("active") is True
        and clob.get("closed") is False
        and clob.get("accepting_orders") is True
        and gamma.get("active") is True
        and gamma.get("closed") is False
        and gamma.get("enableOrderBook") is not False
    )


def append_capture(
    db: Session,
    *,
    kind: str,
    source_key: str,
    raw: dict,
    observed_at: datetime,
    condition_id=None,
    token_id=None,
    wallet=None,
    source_at=None,
    rule_hash=None,
    stable_id=None,
) -> ReplayCapture:
    content = digest(raw)
    identity = stable_id or digest([kind, source_key, content, observed_at.isoformat()])
    existing = db.get(ReplayCapture, identity)
    if existing:
        if existing.content_hash != content:
            raise ValueError("capture identity conflict")
        return existing
    row = ReplayCapture(
        id=identity,
        kind=kind,
        source_key=source_key,
        condition_id=condition_id,
        token_id=token_id,
        wallet=wallet,
        observed_at=observed_at,
        source_at=source_at,
        content_hash=content,
        rule_hash=rule_hash,
        payload_json=json.dumps(raw, sort_keys=True),
    )
    if stable_id is None:
        db.add(row)
        db.flush()
        return row
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError as exc:
        existing = db.get(ReplayCapture, identity)
        if existing is None:
            raise
        if kind == "signal" and existing.kind == kind and existing.source_key == source_key:
            return existing  # The first committed receipt owns this immutable identity.
        if existing.content_hash != content:
            raise ValueError("capture identity conflict") from exc
        return existing
    return row


class PublicVenueCapture:
    """Uses native public URLs only, with no credentials or user-provided URLs."""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def get(self, url, params=None):
        async with self.client.stream(
            "GET", url, params=params, timeout=12, follow_redirects=False
        ) as response:
            if response.status_code != 200:
                raise ValueError("native endpoint did not return HTTP 200")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > 1_000_000:
                    raise ValueError("venue response exceeds capture bound")
                body.extend(chunk)
            # Retain decimal lexemes without binary-float rounding in source receipts.
            return json.loads(body, parse_float=str)

    async def market(self, condition_id):
        if not isinstance(condition_id, str) or not re.fullmatch(
            r"0x[0-9a-fA-F]{64}", condition_id
        ):
            raise ValueError("invalid canonical condition identity")
        condition_id = condition_id.lower()
        path_id = quote(condition_id, safe="")
        clob = await self.get(f"{POLYMARKET_CLOB_API}/markets/{path_id}")
        gamma = await self.get(f"{POLYMARKET_GAMMA_API}/markets", {"condition_ids": condition_id})
        if (
            not isinstance(gamma, list)
            or len(gamma) != 1
            or not isinstance(gamma[0], dict)
            or str(gamma[0].get("conditionId", "")).lower() != condition_id
        ):
            raise ValueError("Gamma condition identity mismatch")
        source = gamma[0]
        outcomes = source.get("outcomes")
        token_ids = source.get("clobTokenIds")
        outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
        token_ids = json.loads(token_ids) if isinstance(token_ids, str) else token_ids
        from app.services.market_data.binary_identity import canonical_binary_metadata

        mapped_clob, mapped_gamma = canonical_binary_metadata(clob, source)
        context = build_market_context(condition_id, mapped_clob, utc_now())
        outcomes = mapped_gamma["outcomes"]
        outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
        if (
            not isinstance(outcomes, list)
            or not isinstance(token_ids, list)
            or len(outcomes) != 2
            or len(token_ids) != 2
        ):
            raise ValueError("unsupported Gamma outcome map")
        mapping = {
            str(label).upper(): str(token) for label, token in zip(outcomes, token_ids, strict=True)
        }
        if mapping != {outcome.outcome: outcome.token_id for outcome in context.outcomes}:
            raise ValueError("Gamma/CLOB token mapping conflict")
        return clob, source, context

    async def book(self, token_id, condition_id, receipt=None):
        if not isinstance(token_id, str) or not re.fullmatch(r"[0-9]{1,120}", token_id):
            raise ValueError("invalid native token identity")
        if not isinstance(condition_id, str) or not re.fullmatch(r"0x[0-9a-f]{64}", condition_id):
            raise ValueError("invalid canonical condition identity")
        raw = await self.get(f"{POLYMARKET_CLOB_API}/book", {"token_id": token_id})
        receipt = receipt or utc_now()
        if (
            not isinstance(raw, dict)
            or str(raw.get("asset_id")) != token_id
            or str(raw.get("market", "")).lower() != condition_id
        ):
            raise ValueError("book token/condition mismatch")
        try:
            source_at = datetime.fromtimestamp(int(raw["timestamp"]) / 1000, UTC)
            if source_at > receipt:
                raise ValueError("book source clock is ahead of receipt")
            sides = {}
            for name, reverse in (("bids", True), ("asks", False)):
                levels = raw.get(name)
                if not isinstance(levels, list) or len(levels) > 1000:
                    raise ValueError("invalid book depth")
                parsed = [
                    Level(price=Decimal(str(level["price"])), shares=Decimal(str(level["size"])))
                    for level in levels
                ]
                sides[name] = sorted(
                    [level for level in parsed if level.shares > 0],
                    key=lambda level: level.price,
                    reverse=reverse,
                )[:10]
            book = Book(
                snapshot_id=digest([raw, receipt.isoformat()]),
                # A snapshot cannot prove replenishment. Native runs consume a
                # cumulative visible-depth budget by token, side and price.
                liquidity_id=digest([condition_id, token_id, "visible-depth-budget-v1"]),
                token_id=token_id,
                observed_at=receipt,
                source_at=source_at,
                **sides,
            )
        except (InvalidOperation, OverflowError, OSError) as exc:
            raise ValueError("invalid native book numbers or clock") from exc
        return raw, book


async def get_capture_venue():
    async with httpx.AsyncClient() as client:
        yield PublicVenueCapture(client)


def market_fingerprint(clob, gamma, context):
    condition = context.condition_id
    return digest(
        {
            "condition_id": condition,
            "clob_rules": clob.get("description"),
            "clob_question": clob.get("question"),
            "clob_end": clob.get("end_date_iso"),
            "gamma_rules": gamma.get("description"),
            "gamma_question": gamma.get("question"),
            "gamma_end": gamma.get("endDate"),
            "gamma_source": gamma.get("resolutionSource"),
            "gamma_events": [
                {
                    key: event.get(key)
                    for key in (
                        "title",
                        "description",
                        "resolutionSource",
                        "endDate",
                        "clarification",
                    )
                }
                for event in (gamma.get("events") or [])
                if isinstance(event, dict)
            ],
            "gamma_clarifications": gamma.get("clarification"),
            "outcomes": [(o.outcome, o.token_id) for o in context.outcomes],
        }
    )


async def capture_wallet(db: Session, wallet: str, venue: PublicVenueCapture) -> dict:
    if not isinstance(wallet, str) or not re.fullmatch(r"0x[0-9a-f]{40}", wallet):
        raise ValueError("invalid wallet identity")
    activity = await venue.get(
        f"{POLYMARKET_DATA_API}/activity", {"user": wallet, "type": "TRADE", "limit": 50}
    )
    activity_received = utc_now()
    if not isinstance(activity, list) or len(activity) > 50:
        raise ValueError("invalid activity response")
    saved, failures = [], []
    markets = {}
    attempted_conditions = set()
    captured_tokens = set()
    for raw in activity:
        if not isinstance(raw, dict):
            failures.append("malformed_activity")
            continue
        if (
            len(attempted_conditions) >= 5
            and str(raw.get("conditionId", "")).lower() not in attempted_conditions
        ):
            failures.append("condition_limit")
            continue
        try:
            if raw.get("type") != "TRADE" or str(raw.get("proxyWallet", "")).lower() != wallet:
                raise ValueError("activity wallet/type mismatch")
            condition = str(raw["conditionId"]).lower()
            token = str(raw["asset"])
            if not re.fullmatch(r"0x[0-9a-f]{64}", condition) or not re.fullmatch(
                r"[0-9]{1,120}", token
            ):
                raise ValueError("invalid activity identity")
            if not re.fullmatch(r"0x[0-9a-fA-F]{64}", str(raw.get("transactionHash", ""))):
                raise ValueError("activity has no stable transaction identity")
            if condition not in markets:
                if condition in attempted_conditions:
                    raise ValueError("native market metadata unavailable")
                attempted_conditions.add(condition)
                clob, gamma, context = await venue.market(condition)
                fingerprint = market_fingerprint(clob, gamma, context)
                markets[condition] = (context, fingerprint, native_active(clob, gamma))
                append_capture(
                    db,
                    kind="gamma",
                    source_key=condition,
                    raw=gamma,
                    observed_at=context.observed_at,
                    condition_id=condition,
                    rule_hash=fingerprint,
                )
                append_capture(
                    db,
                    kind="clob_market",
                    source_key=condition,
                    raw=clob,
                    observed_at=context.observed_at,
                    condition_id=condition,
                    rule_hash=fingerprint,
                )
            context, rule_hash, active = markets[condition]
            if not active:
                failures.append("market_not_active")
                continue
            if token not in {outcome.token_id for outcome in context.outcomes}:
                raise ValueError("activity token not in canonical market")
            receipt = activity_received
            source_at = datetime.fromtimestamp(int(raw["timestamp"]), UTC)
            if source_at > receipt:
                raise ValueError("activity source clock ahead of receipt")
            source_size, source_price = Decimal(str(raw["size"])), Decimal(str(raw["price"]))
            if (
                not source_size.is_finite()
                or not source_price.is_finite()
                or not 0 < source_size <= 1000000000
                or not 0 < source_price <= 1
            ):
                raise ValueError("invalid source trade size or price")
            Level(price=source_price, shares=source_size)
            identity = digest(
                [
                    wallet,
                    raw["transactionHash"].lower(),
                    token,
                    raw["side"],
                    str(source_size.normalize(ACCOUNTING_CONTEXT)),
                    str(source_price.normalize(ACCOUNTING_CONTEXT)),
                    source_at.isoformat(),
                ]
            )
            signal = Signal(
                event_id=identity,
                token_id=token,
                first_seen_at=receipt,
                source_at=source_at,
                side=raw["side"],
            )
            # Exact repeat observations preserve the original first-seen clock.
            previous = db.get(ReplayCapture, identity)
            if previous is None:
                append_capture(
                    db,
                    kind="signal",
                    source_key=identity,
                    raw={"signal": signal.model_dump(mode="json"), "venue": raw},
                    observed_at=receipt,
                    source_at=source_at,
                    wallet=wallet,
                    token_id=token,
                    condition_id=condition,
                    rule_hash=rule_hash,
                    stable_id=identity,
                )
            if token in captured_tokens:
                continue
            raw_book, book = await venue.book(token, condition)
            captured_tokens.add(token)
            saved.append(
                append_capture(
                    db,
                    kind="book",
                    source_key=book.snapshot_id,
                    raw={"book": book.model_dump(mode="json"), "venue": raw_book},
                    observed_at=book.observed_at,
                    source_at=book.source_at,
                    token_id=token,
                    condition_id=condition,
                    rule_hash=rule_hash,
                ).id
            )
        except (
            ValueError,
            KeyError,
            TypeError,
            InvalidOperation,
            OverflowError,
            OSError,
            httpx.HTTPError,
        ):
            failures.append("invalid_or_unavailable_venue_evidence")
    db.commit()
    return {
        "captured_at": utc_now().isoformat(),
        "books_captured": len(saved),
        "capture_ids": saved,
        "failures": failures,
        "history_quality": "native_receipt_time_capture",
        "historical_backfill": False,
    }


def frozen_dataset(
    db: Session, wallet: str, start: datetime, end: datetime
) -> tuple[Dataset, list[str]]:
    signals = (
        db.query(ReplayCapture)
        .filter(
            ReplayCapture.kind == "signal",
            ReplayCapture.wallet == wallet,
            ReplayCapture.observed_at >= start,
            ReplayCapture.observed_at <= end,
        )
        .order_by(ReplayCapture.observed_at, ReplayCapture.id)
        .limit(51)
        .all()
    )
    if len(signals) > 50:
        raise ValueError("window exceeds 50 captured signals; narrow the window")
    condition_versions = {(row.condition_id, row.rule_hash) for row in signals}
    if len({condition for condition, _ in condition_versions}) != len(condition_versions):
        raise ValueError("rule change inside replay window")
    if len({row.token_id for row in signals}) != len(
        {(row.condition_id, row.token_id) for row in signals}
    ):
        raise ValueError("token appears under multiple conditions")
    identities = {(row.token_id, row.rule_hash) for row in signals}
    candidates = (
        db.query(ReplayCapture)
        .filter(
            ReplayCapture.kind == "book",
            ReplayCapture.condition_id.in_([condition for condition, _ in condition_versions]),
            ReplayCapture.token_id.in_([token for token, _ in identities]),
            ReplayCapture.observed_at >= start,
            ReplayCapture.observed_at <= end,
        )
        .order_by(ReplayCapture.observed_at, ReplayCapture.id)
        .limit(251)
        .all()
    )
    if len(candidates) > 250:
        raise ValueError("window exceeds 250 captured books; narrow the window")
    conditions = {condition for condition, _ in condition_versions}
    market_rows = (
        db.query(ReplayCapture)
        .filter(
            ReplayCapture.kind == "clob_market",
            ReplayCapture.condition_id.in_(conditions),
            ReplayCapture.observed_at >= start,
            ReplayCapture.observed_at <= end,
        )
        .order_by(ReplayCapture.observed_at, ReplayCapture.id)
        .limit(501)
        .all()
    )
    if len(market_rows) > 500:
        raise ValueError("window exceeds 500 market observations; narrow the window")
    for condition in sorted(conditions):
        baseline = (
            db.query(ReplayCapture)
            .filter(
                ReplayCapture.kind == "clob_market",
                ReplayCapture.condition_id == condition,
                ReplayCapture.observed_at < start,
            )
            .order_by(ReplayCapture.observed_at.desc(), ReplayCapture.id.desc())
            .first()
        )
        if baseline:
            market_rows.append(baseline)

    # Bind the frozen replay to every source version received during its window.
    gamma_rows = (
        db.query(ReplayCapture)
        .filter(
            ReplayCapture.kind == "gamma",
            ReplayCapture.condition_id.in_(conditions),
            ReplayCapture.observed_at >= start,
            ReplayCapture.observed_at <= end,
        )
        .order_by(ReplayCapture.observed_at, ReplayCapture.id)
        .limit(501)
        .all()
    )
    if len(gamma_rows) > 500:
        raise ValueError("window exceeds 500 rule observations; narrow the window")
    if any(
        (row.condition_id, row.rule_hash) not in condition_versions
        for row in market_rows
        if aware(row.observed_at) >= start
    ):
        raise ValueError("rule change inside replay window")

    def market_at(condition, when):
        eligible = [
            row
            for row in market_rows
            if row.condition_id == condition and aware(row.observed_at) <= when
        ]
        return max(eligible, key=lambda row: (aware(row.observed_at), row.id)) if eligible else None

    def gamma_at(condition, when):
        eligible = [
            row
            for row in gamma_rows
            if row.condition_id == condition and aware(row.observed_at) <= when
        ]
        return max(eligible, key=lambda row: (aware(row.observed_at), row.id)) if eligible else None

    def active_at(book):
        market = market_at(book.condition_id, aware(book.observed_at))
        if market is None or market.rule_hash != book.rule_hash:
            return False
        raw = json.loads(market.payload_json)
        gamma = gamma_at(book.condition_id, aware(book.observed_at))
        return (
            gamma is not None
            and gamma.rule_hash == book.rule_hash
            and native_active(raw, json.loads(gamma.payload_json))
        )

    versions = identities | {(row.token_id, row.rule_hash) for row in candidates}
    if len({token for token, _ in versions}) != len(versions):
        raise ValueError("rule change inside replay window")
    books = [
        row for row in candidates if (row.token_id, row.rule_hash) in identities and active_at(row)
    ]
    # A token with multiple rule versions in one window cannot be replayed under one mapping.
    if len({token for token, _ in identities}) != len(identities):
        raise ValueError("rule change inside replay window")
    for condition in sorted(conditions):
        market = market_at(condition, end)
        raw = json.loads(market.payload_json) if market else {}
        gamma = gamma_at(condition, end)
        if gamma is None or not native_active(raw, json.loads(gamma.payload_json)):
            raise ValueError(
                "market closed or inactive; verified settlement capture is unavailable"
            )
    if any(row.source_at is None for row in signals):
        raise ValueError("native signal has no source clock")
    referenced = sorted(
        signals + books + market_rows + gamma_rows,
        key=lambda row: (aware(row.observed_at), row.kind, row.id),
    )
    if any(digest(json.loads(row.payload_json)) != row.content_hash for row in referenced):
        raise ValueError("captured source integrity check failed")
    return Dataset(
        source="native receipt-time capture",
        signals=[
            Signal.model_validate(
                {**json.loads(row.payload_json)["signal"], "source_at": aware(row.source_at)}
            )
            for row in signals
        ],
        books=[Book.model_validate(json.loads(row.payload_json)["book"]) for row in books],
        settlements=[],
    ), list(dict.fromkeys(row.id for row in referenced))


def capture_manifest(db: Session, capture_ids: list[str]) -> list[dict]:
    """Stable native-source identity, separate from normalized replay inputs."""
    rows = db.query(ReplayCapture).filter(ReplayCapture.id.in_(capture_ids)).all()
    if len(rows) != len(set(capture_ids)):
        raise ValueError("captured source manifest is incomplete")
    return [
        {
            "id": row.id,
            "kind": row.kind,
            "source_key": row.source_key,
            "condition_id": row.condition_id,
            "token_id": row.token_id,
            "wallet": row.wallet,
            "content_hash": row.content_hash,
            "rule_hash": row.rule_hash,
            "observed_at": aware(row.observed_at).isoformat(),
            "source_at": aware(row.source_at).isoformat() if row.source_at else None,
        }
        for row in sorted(rows, key=lambda row: row.id)
    ]


async def capture_market(db: Session, condition_id: str, venue: PublicVenueCapture) -> dict:
    """Bounded public metadata/book receipt for a paper account's existing market."""
    clob, gamma, context = await venue.market(condition_id)
    fingerprint = market_fingerprint(clob, gamma, context)
    rows = []
    for kind, raw in (("gamma", gamma), ("clob_market", clob)):
        rows.append(
            append_capture(
                db,
                kind=kind,
                source_key=condition_id,
                raw=raw,
                observed_at=context.observed_at,
                condition_id=condition_id,
                rule_hash=fingerprint,
            ).id
        )
    if native_active(clob, gamma):
        for outcome in context.outcomes:
            raw, book = await venue.book(outcome.token_id, condition_id)
            rows.append(
                append_capture(
                    db,
                    kind="book",
                    source_key=book.snapshot_id,
                    raw={"book": book.model_dump(mode="json"), "venue": raw},
                    observed_at=book.observed_at,
                    source_at=book.source_at,
                    token_id=outcome.token_id,
                    condition_id=condition_id,
                    rule_hash=fingerprint,
                ).id
            )
    return {"capture_ids": rows, "condition_id": condition_id, "active": native_active(clob, gamma)}
