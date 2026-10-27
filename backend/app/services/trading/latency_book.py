"""Fail-closed live latency quote contract using the versioned native book adapter.

Only an explicit zero venue fee is supported for live authorization. Positive or
unknown fees need a venue-specific fee model; research flat fees are not evidence.
"""

import json
import re
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal, localcontext
from typing import Literal

import httpx
from pydantic import AwareDatetime, Field

from app.services.analytics.replay.ledger import ACCOUNTING_CONTEXT
from app.services.market_data.binary_book import (
    Contract,
    Level,
    WalkRequest,
    reconstruct,
    walk_depth,
)
from app.services.market_data.book_research import ResearchQuery, book_event, market_spec
from app.services.market_data.polymarket_service import POLYMARKET_CLOB_API
from app.services.market_data.replay_capture import PublicVenueCapture, digest
from app.utils.time import utc_now

MAX_AGE = timedelta(seconds=1)


class ExecutableBookEvidence(Contract):
    status: Literal["READY", "INSUFFICIENT_DEPTH", "UNKNOWN_COST", "INVALID_BOOK", "STALE"]
    reason: str
    condition_id: str
    token_id: str
    source: str = "polymarket-clob-rest-v1/binary-book-v1"
    received_at: AwareDatetime
    source_at: AwareDatetime | None = None
    expires_at: AwareDatetime | None = None
    receipt_hash: str | None = None
    bids: list[Level] = Field(default_factory=list)
    asks: list[Level] = Field(default_factory=list)
    quote_age_seconds: Decimal | None = None
    fee_usdc: Decimal | None = None
    tick_size: Decimal | None = None
    neg_risk: bool | None = None
    shares: Decimal = Decimal(0)
    limit_price: Decimal = Decimal(0)
    authorized_cash: Decimal = Decimal(0)
    worst_case_cash: Decimal = Decimal(0)
    vwap: Decimal | None = None


def unavailable(condition_id, token_id, reason, status="INVALID_BOOK"):
    return ExecutableBookEvidence(
        status=status,
        reason=reason,
        condition_id=str(condition_id or ""),
        token_id=str(token_id or ""),
        received_at=utc_now(),
    )


def authorize(raw, metadata, token_id, cash, fee, received_at, as_of, max_spread):
    with localcontext(ACCOUNTING_CONTEXT):
        return _authorize(raw, metadata, token_id, cash, fee, received_at, as_of, max_spread)


def _authorize(raw, metadata, token_id, cash, fee, received_at, as_of, max_spread):
    """Object-level native JSON only; money is USDC, depth and order size are shares."""
    try:
        cid = metadata["raw"]["clob"].get("condition_id", "")
    except (KeyError, TypeError, AttributeError):
        return unavailable("", token_id, "MALFORMED_MARKET_METADATA")
    base = {
        "condition_id": cid,
        "token_id": token_id,
        "received_at": received_at,
        "receipt_hash": digest([raw, metadata, str(fee), received_at.isoformat()]),
    }

    def reject(reason, status="INVALID_BOOK"):
        return ExecutableBookEvidence(**base, status=status, reason=reason)

    try:
        spec = market_spec(metadata, ResearchQuery(condition_id=cid))
        if not isinstance(raw, dict):
            return reject("MALFORMED_BOOK_BODY")
        if token_id not in (spec.yes_token, spec.no_token):
            return reject("TOKEN_MAPPING_MISMATCH")
        if raw.get("asset_id") != token_id or raw.get("market") != spec.condition_id:
            return reject("BOOK_IDENTITY_MISMATCH")
        if isinstance(raw.get("timestamp"), bool):
            return reject("SOURCE_CLOCK_UNKNOWN")
        source_at = datetime.fromtimestamp(int(raw["timestamp"]) / 1000, UTC)
        if source_at > received_at or received_at > as_of:
            return reject("SOURCE_CLOCK_UNKNOWN_OR_FUTURE", "STALE")
        if as_of - source_at > MAX_AGE or as_of - received_at > MAX_AGE:
            return reject("STALE_BOOK", "STALE")
        # bools must not be coerced to Decimal(1); no legacy tuple guessing.
        for side in ("bids", "asks"):
            if not isinstance(raw.get(side), list) or not 1 <= len(raw[side]) <= 1000:
                return reject("MISSING_BOOK_SIDE")
            for level in raw[side]:
                if not isinstance(level, dict) or any(
                    isinstance(level.get(k), bool) for k in ("price", "size")
                ):
                    return reject("MALFORMED_LEVEL")
        value = {
            "raw": {**raw, "event_type": "book"},
            "snapshot_id": base["receipt_hash"],
            "token_id": token_id,
            "received_at": received_at,
            "event_at": source_at,
            "connection_id": "latency-rest",
        }
        event = book_event(value, spec)
        if event is not None and event.kind == "disconnect":
            return reject("MALFORMED_BOOK_LEVELS")
        book = reconstruct(spec, token_id, [event] if event else [], as_of, 1)
        if book.grade != "VERIFIED_BOOK":
            return reject(
                book.reasons[0] if book.reasons else "INVALID_BOOK",
                "STALE" if book.grade == "STALE" else "INVALID_BOOK",
            )
        bid, ask = book.bids[0].price, book.asks[0].price
        if (ask - bid) / ((ask + bid) / 2) > Decimal(str(max_spread)):
            return reject("SPREAD_TOO_WIDE")
        fee_value = fee.get("base_fee") if isinstance(fee, dict) else None
        try:
            fee_decimal = Decimal(str(fee_value))
            known_zero_fee = (
                not isinstance(fee_value, bool) and fee_decimal.is_finite() and fee_decimal == 0
            )
        except (ValueError, ArithmeticError):
            known_zero_fee = False
        if not known_zero_fee:
            return reject("VENUE_FEE_MODEL_UNKNOWN", "UNKNOWN_COST")
        budget = Decimal(str(cash))
        if not budget.is_finite() or budget <= 0:
            return reject("INVALID_CASH_BUDGET")
        estimate = walk_depth(
            spec,
            book,
            WalkRequest(
                side="BUY",
                units="USDC",
                amount=budget,
                fee_bps=Decimal(0),
                fee_version="native-explicit-zero",
            ),
        )
        if estimate.status != "modeled" or not estimate.curve:
            return reject(estimate.reason or "INSUFFICIENT_DEPTH", "INSUFFICIENT_DEPTH")
        # FOK authorizes the worst price for every share, never VWAP as limit.
        limit = estimate.curve[-1].price
        shares = min(
            estimate.matched_shares,
            (budget / limit / spec.lot).to_integral_value(rounding=ROUND_DOWN) * spec.lot,
        )
        if shares < spec.minimum_shares:
            return reject("BELOW_MINIMUM_ORDER_SIZE", "INSUFFICIENT_DEPTH")
        checked = walk_depth(
            spec,
            book,
            WalkRequest(
                side="BUY",
                amount=shares,
                fee_bps=Decimal(0),
                fee_version="native-explicit-zero",
                limit_price=limit,
            ),
        )
        if checked.status != "modeled" or checked.partial or shares * limit > budget:
            return reject("INSUFFICIENT_DEPTH", "INSUFFICIENT_DEPTH")
        return ExecutableBookEvidence(
            **base,
            status="READY",
            reason="VALIDATED_DISPLAYED_DEPTH",
            source_at=source_at,
            expires_at=min(source_at, received_at) + MAX_AGE,
            shares=shares,
            limit_price=limit,
            authorized_cash=budget,
            worst_case_cash=shares * limit,
            vwap=checked.vwap,
            bids=book.bids,
            asks=book.asks,
            quote_age_seconds=Decimal(str((as_of - source_at).total_seconds())),
            fee_usdc=Decimal(0),
            tick_size=spec.tick,
            neg_risk=False,
        )
    except (KeyError, TypeError, ValueError, ArithmeticError, OverflowError, OSError):
        return reject("MALFORMED_BOOK_OR_METADATA")


class EvidenceError(ValueError):
    pass


class LiveVenue(PublicVenueCapture):
    async def get(self, url, params=None):
        async with self.client.stream(
            "GET", url, params=params, timeout=1, follow_redirects=False
        ) as response:
            if response.status_code != 200:
                raise EvidenceError(f"PROVIDER_HTTP_{response.status_code}")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > 1_000_000:
                    raise EvidenceError("PROVIDER_RESPONSE_TOO_LARGE")
                body.extend(chunk)
            try:
                return json.loads(body, parse_float=str)
            except ValueError as exc:
                raise EvidenceError("PROVIDER_INVALID_JSON") from exc


async def fetch_executable_book(condition_id, token_id, cash, max_spread):
    if (
        not isinstance(condition_id, str)
        or not isinstance(token_id, str)
        or not re.fullmatch(r"0x[0-9a-f]{64}", condition_id)
        or not re.fullmatch(r"[0-9]{1,120}", token_id)
    ):
        return unavailable(condition_id, token_id, "INVALID_MARKET_IDENTITY")
    try:
        async with httpx.AsyncClient() as client:
            venue = LiveVenue(client)
            clob, gamma, _ = await venue.market(condition_id)
            metadata_at = utc_now()
            metadata = {
                "raw": {"clob": clob, "gamma": gamma, "market": condition_id},
                "received_at": metadata_at.isoformat(),
                "snapshot_id": digest([clob, gamma]),
            }
            fee = await venue.get(f"{POLYMARKET_CLOB_API}/fee-rate", {"token_id": token_id})
            raw = await venue.get(f"{POLYMARKET_CLOB_API}/book", {"token_id": token_id})
            received_at = utc_now()
            return authorize(raw, metadata, token_id, cash, fee, received_at, utc_now(), max_spread)
    except httpx.TimeoutException:
        reason = "PROVIDER_TIMEOUT"
    except httpx.RequestError:
        reason = "PROVIDER_CONNECTION_FAILED"
    except EvidenceError as exc:
        reason = str(exc)
    except (KeyError, TypeError, ValueError, ArithmeticError):
        reason = "MALFORMED_BOOK_OR_METADATA"
    return unavailable(condition_id, token_id, reason)
