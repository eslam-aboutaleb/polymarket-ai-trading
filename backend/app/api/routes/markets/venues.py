"""Cross-venue market API — cached venue rows, adapter health, mismatches.

Read-only surface for plan 05 Phase A/B. Every response is built from the
``venue_markets`` cache the refresh loop writes, so the UI and the AI
services see exactly the same rows, and an adapter outage shows up as
stale ``fetched_at`` values rather than an error.

Signal-only: no endpoint here places, cancels or sizes an order. The
cross-venue groups carry both venues' questions so a reviewer can
confirm a match before acting on it, and the mismatch endpoint does the
same for the fuzzy matches it reports — a wrong match is the main
fake-arb risk, so both questions always travel verbatim.
"""

from __future__ import annotations

import hmac
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.routes.auth.auth import get_current_user_from_token
from app.config import get_settings
from app.services.analytics.cross_venue_arb import (
    DEFAULT_MIN_EDGE,
    DEFAULT_MIN_LIQUIDITY,
    find_mismatches,
    get_intra_polymarket_signals,
)
from app.services.market_data.venue_market_service import (
    DEFAULT_QUERY_LIMIT,
    MAX_QUERY_LIMIT,
    get_adapter_health,
    get_cross_venue_groups,
    get_venue_market_metrics,
    get_venue_markets,
    serialize_venue_market,
)
from app.utils.database import get_db
from app.utils.time import utc_now

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/venues", tags=["venues"])


class VenueMarketResponse(BaseModel):
    venue: str
    market_id: str
    question: str
    yes_price: float | None = None
    no_price: float | None = None
    outcomes: list[str] = ["Yes", "No"]
    volume_24h: float = 0.0
    liquidity: float = 0.0
    end_date: datetime | None = None
    url: str = ""
    is_real_money: bool = True
    fetched_at: datetime | None = None


class VenueMarketsResponse(BaseModel):
    markets: list[VenueMarketResponse]
    total: int


class VenueAdapterHealth(BaseModel):
    name: str
    enabled: bool
    last_fetch_at: str | None = None
    last_error: str | None = None
    markets: int = 0


class VenueAdaptersResponse(BaseModel):
    adapters: list[VenueAdapterHealth]
    scheduler: dict


class CrossVenueGroup(BaseModel):
    key: str
    venues: list[str]
    questions: dict[str, str]
    rows: list[VenueMarketResponse]
    yes_min: float | None = None
    yes_max: float | None = None
    edge: float | None = None
    max_liquidity: float = 0.0


class CrossVenueResponse(BaseModel):
    query: str
    groups: list[CrossVenueGroup]


class VenueMismatchLeg(BaseModel):
    """One venue's side of a mismatch — question verbatim."""

    venue: str
    market_id: str
    question: str
    yes_price: float | None = None
    no_price: float | None = None
    liquidity: float = 0.0
    url: str = ""
    is_real_money: bool = True
    fetched_at: datetime | None = None


class VenueMismatch(BaseModel):
    """The same question priced differently on two venues.

    ``executable_signal`` is False whenever either leg is play money, so
    a caller cannot mistake a Manifold sentiment price for a real crack.
    """

    venues: list[str]
    match_type: str
    match_score: float
    cheap: VenueMismatchLeg
    rich: VenueMismatchLeg
    questions: dict[str, str]
    edge: float
    edge_pct: float
    low_price: float | None = None
    high_price: float | None = None
    min_liquidity: float = 0.0
    executable_signal: bool = False
    # A horizon gap usually means the two questions are not the same
    # market, so it is the cheapest wrong-match signal to surface.
    end_date_delta_days: float | None = None
    horizon_mismatch: bool = False
    play_money_venues: list[str] = []
    play_money_only: bool = False
    play_money_reason: str | None = None
    newest_fetched_at: datetime | None = None


class VenueMismatchResponse(BaseModel):
    mismatches: list[VenueMismatch]
    count: int
    min_edge: float = DEFAULT_MIN_EDGE
    min_liquidity: float = DEFAULT_MIN_LIQUIDITY
    include_play_money: bool = False
    # Intra-Polymarket complement/spread spots, read from the existing
    # detector so cross-venue and single-venue signals share one surface.
    intra_polymarket: list[dict] = []
    signal_only: bool = True
    generated_at: datetime


async def require_venue_reader(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict:
    """Authenticate the cross-venue read for a user or an AI service.

    The user path is ``get_current_user_from_token`` verbatim. The
    service path is opt-in: it only exists when ``venue_api_token`` is
    configured, it accepts that exact bearer token, and it is the only
    way the AI services (which hold no user session) can read the same
    cached rows. With the setting empty — the default — the route
    behaves exactly like the other venue routes.
    """
    token = (authorization or "").removeprefix("Bearer ").strip()
    service_token = str(getattr(get_settings(), "venue_api_token", "") or "")
    if token and service_token and hmac.compare_digest(token, service_token):
        return {"service": True}
    return get_current_user_from_token(request, authorization)


@router.get("/markets", response_model=VenueMarketsResponse)
async def list_venue_markets(
    venue: str | None = Query(default=None, description="Filter by venue name"),
    q: str | None = Query(default=None, description="Question substring filter"),
    limit: int = Query(DEFAULT_QUERY_LIMIT, ge=1, le=MAX_QUERY_LIMIT),
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user_from_token),
):
    """Latest cached rows for one venue (or all), most liquid first."""
    rows = get_venue_markets(db, venue=venue, q=q, limit=limit)
    return {"markets": [serialize_venue_market(row) for row in rows], "total": len(rows)}


@router.get("/adapters", response_model=VenueAdaptersResponse)
async def list_venue_adapters(
    _user: dict = Depends(get_current_user_from_token),
):
    """Per-adapter health: flag state, last fetch and last error."""
    return {"adapters": get_adapter_health(), "scheduler": get_venue_market_metrics()}


@router.get("/markets/cross", response_model=CrossVenueResponse)
async def list_cross_venue_markets(
    q: str = Query(..., description="Question substring to match across venues"),
    limit: int = Query(DEFAULT_QUERY_LIMIT, ge=1, le=MAX_QUERY_LIMIT),
    db: Session = Depends(get_db),
    _reader: dict = Depends(require_venue_reader),
):
    """Rows grouped by normalized question across every venue that has them."""
    groups = get_cross_venue_groups(db, q, limit=limit)
    return {"query": q, "groups": groups}


@router.get("/mismatches", response_model=VenueMismatchResponse)
async def list_venue_mismatches(
    min_edge: float = Query(DEFAULT_MIN_EDGE, ge=0.0, le=0.5),
    min_liquidity: float = Query(DEFAULT_MIN_LIQUIDITY, ge=0.0, le=1e12),
    include_play_money: bool = Query(
        False,
        description="Also return play-money pairs, flagged as non-executable",
    ),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user_from_token),
):
    """Cross-venue price gaps for the same question — signal-only.

    Both questions are returned verbatim so a reviewer can confirm the
    match; a play-money leg is only ever returned with
    ``include_play_money=true`` and is never ``executable_signal``.
    """
    mismatches = find_mismatches(
        db,
        min_edge=min_edge,
        min_liquidity=min_liquidity,
        include_play_money=include_play_money,
        limit=limit,
    )
    return {
        "mismatches": mismatches,
        "count": len(mismatches),
        "min_edge": min_edge,
        "min_liquidity": min_liquidity,
        "include_play_money": include_play_money,
        "intra_polymarket": get_intra_polymarket_signals(),
        "signal_only": True,
        "generated_at": utc_now(),
    }
