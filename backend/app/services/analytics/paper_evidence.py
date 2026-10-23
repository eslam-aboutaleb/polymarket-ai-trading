"""Read-only evidence boundary for persistent paper accounting; never a signer."""

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.market_data.replay_capture import ReplayCapture
from app.services.analytics.replay.follower import Book
from app.services.market_data.replay_capture import aware, digest, native_active


@dataclass(frozen=True)
class PaperBook:
    capture_id: str
    condition_id: str
    rule_hash: str
    book: Book
    tick_size: Decimal
    minimum_shares: Decimal


class EvidenceUnavailable(ValueError):
    pass


def latest(db: Session, kind: str, as_of: datetime, **identity) -> ReplayCapture | None:
    return (
        db.query(ReplayCapture)
        .filter_by(kind=kind, **identity)
        .filter(ReplayCapture.observed_at <= as_of)
        .order_by(ReplayCapture.observed_at.desc(), ReplayCapture.id.desc())
        .first()
    )


def payload(row: ReplayCapture) -> dict:
    raw = json.loads(row.payload_json)
    if digest(raw) != row.content_hash:
        raise EvidenceUnavailable("CAPTURE_INTEGRITY_FAILURE")
    return raw


def market_rows(db: Session, condition: str, as_of: datetime, rule_hash: str):
    clob = latest(db, "clob_market", as_of, condition_id=condition)
    gamma = latest(db, "gamma", as_of, condition_id=condition)
    if (
        not clob
        or not gamma
        or not rule_hash
        or clob.rule_hash != rule_hash
        or gamma.rule_hash != rule_hash
    ):
        raise EvidenceUnavailable("MARKET_IDENTITY_OR_RULES_CHANGED")
    return payload(clob), payload(gamma)


def _current_book(
    db: Session,
    token_id: str,
    as_of: datetime,
    *,
    max_age: int = 60,
    require_size_rules: bool = True,
) -> PaperBook:
    row = latest(db, "book", as_of, token_id=token_id)
    if not row:
        raise EvidenceUnavailable("MISSING_BOOK")
    raw = payload(row)
    book = Book.model_validate(raw["book"])
    if (
        book.token_id != token_id
        or book.observed_at != aware(row.observed_at)
        or book.source_at != aware(row.source_at)
    ):
        raise EvidenceUnavailable("BOOK_IDENTITY_OR_CLOCK_MISMATCH")
    if (
        not 0 <= (as_of - book.source_at).total_seconds() <= max_age
        or not 0 <= (as_of - book.observed_at).total_seconds() <= max_age
    ):
        raise EvidenceUnavailable("STALE_BOOK")
    clob, gamma = market_rows(db, row.condition_id, as_of, row.rule_hash)
    if not native_active(clob, gamma):
        raise EvidenceUnavailable("MARKET_NOT_ACTIVE")
    tokens = {str(t.get("token_id")) for t in clob.get("tokens", [])}
    if token_id not in tokens:
        raise EvidenceUnavailable("TOKEN_MAPPING_CHANGED")
    venue = raw.get("venue", {})
    if (
        str(venue.get("asset_id")) != token_id
        or str(venue.get("market", "")).lower() != row.condition_id
    ):
        raise EvidenceUnavailable("BOOK_TOKEN_CONDITION_MISMATCH")
    try:
        tick = Decimal(str(venue["tick_size"]))
        minimum = Decimal(str(venue["min_order_size"]))
        if not tick.is_finite() or not minimum.is_finite() or not 0 < tick <= 1 or minimum <= 0:
            raise ValueError("invalid venue size rules")
    except (KeyError, ValueError, ArithmeticError):
        if require_size_rules:
            raise EvidenceUnavailable("VENUE_SIZE_RULES_UNKNOWN") from None
        tick, minimum = Decimal(1), Decimal(0)  # Unused for marks, never fill defaults.
    return PaperBook(row.id, row.condition_id, row.rule_hash, book, tick, minimum)


def _final_payout(db: Session, token_id: str, condition: str, rule_hash: str, as_of: datetime):
    """Require paired finalized binary venue evidence, not a Binance expiry price."""
    clob, gamma = market_rows(db, condition, as_of, rule_hash)
    if (
        clob.get("closed") is not True
        or gamma.get("closed") is not True
        or gamma.get("umaResolutionStatus") != "resolved"
    ):
        raise EvidenceUnavailable("SETTLEMENT_NOT_FINAL")
    outcomes, prices, tokens = (
        gamma.get("outcomes"),
        gamma.get("outcomePrices"),
        gamma.get("clobTokenIds"),
    )
    outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
    prices = json.loads(prices) if isinstance(prices, str) else prices
    tokens = json.loads(tokens) if isinstance(tokens, str) else tokens
    if not all(isinstance(v, list) and len(v) == 2 for v in (outcomes, prices, tokens)):
        raise EvidenceUnavailable("SETTLEMENT_MAPPING_UNAVAILABLE")
    mapping = {
        str(t): (str(o).upper(), Decimal(str(p)))
        for t, o, p in zip(tokens, outcomes, prices, strict=True)
    }
    if (
        token_id not in mapping
        or {m[0] for m in mapping.values()} != {"YES", "NO"}
        or sorted(m[1] for m in mapping.values()) != [Decimal(0), Decimal(1)]
    ):
        raise EvidenceUnavailable("SETTLEMENT_DISPUTED_VOID_OR_UNSUPPORTED")
    clob_tokens = clob.get("tokens", [])
    if len(clob_tokens) != 2 or {str(t.get("token_id")) for t in clob_tokens} != set(mapping):
        raise EvidenceUnavailable("SETTLEMENT_TOKEN_CONFLICT")
    for token in clob_tokens:
        outcome, payout = mapping[str(token["token_id"])]
        if (
            str(token.get("outcome", "")).upper() != outcome
            or type(token.get("winner")) is not bool
            or token["winner"] != bool(payout)
        ):
            raise EvidenceUnavailable("SETTLEMENT_PROVIDER_DISAGREEMENT")
    return mapping[token_id][1]


def current_book(*args, **kwargs):
    try:
        return _current_book(*args, **kwargs)
    except EvidenceUnavailable:
        raise
    except (ValueError, TypeError, KeyError, ArithmeticError, AttributeError) as exc:
        raise EvidenceUnavailable("INVALID_BOOK_EVIDENCE") from exc


def final_payout(*args, **kwargs):
    try:
        return _final_payout(*args, **kwargs)
    except EvidenceUnavailable:
        raise
    except (ValueError, TypeError, KeyError, ArithmeticError, AttributeError) as exc:
        raise EvidenceUnavailable("INVALID_SETTLEMENT_EVIDENCE") from exc
