"""Read-only analytical book tooling; only public capture and private audit writes."""

import asyncio
import logging

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.routes.auth.auth import get_current_user_from_token
from app.config import get_settings
from app.models import Account
from app.models.analytics.book_research_run import BookResearchRun
from app.security.rate_limit import limiter
from app.services.market_data.book_research import (
    ResearchQuery,
    capture_market_books,
    load_analysis,
    replay_research,
    save_research,
)
from app.services.market_data.replay_capture import PublicVenueCapture, get_capture_venue
from app.services.users.account_service import ensure_personal_account, get_owned_account
from app.utils.database import get_db

router = APIRouter(
    prefix="/api/microstructure",
    tags=["binary book research"],
    dependencies=[Depends(get_current_user_from_token)],
)
logger = logging.getLogger(__name__)


def enabled():
    if not get_settings().binary_book_research_enabled:
        raise HTTPException(503, "Binary book research disabled")


def owned_account(
    x_account_id: str | None = Header(None, alias="X-Account-Id"),
    user=Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    if x_account_id is None:
        return ensure_personal_account(db, user["user_id"])
    if not x_account_id.isascii() or not x_account_id.isdigit() or int(x_account_id) <= 0:
        raise HTTPException(400, "Invalid account header")
    try:
        account = get_owned_account(db, user["user_id"], int(x_account_id))
    except ValueError as exc:
        raise HTTPException(403, "Account unavailable") from exc
    if account is None:
        raise HTTPException(403, "Account unavailable")
    return account


@router.post("/analyze", dependencies=[Depends(enabled)])
def analyze(query: ResearchQuery, db: Session = Depends(get_db)):
    try:
        result, _ = load_analysis(db, query)
        return result
    except (ValueError, ArithmeticError) as exc:
        raise HTTPException(409, "Captured book evidence failed validation") from exc


@router.get("/book", dependencies=[Depends(enabled)])
def book(query: ResearchQuery = Depends(), db: Session = Depends(get_db)):
    return analyze(query, db)


@router.post("/capture", dependencies=[Depends(enabled)])
@limiter.limit("6/minute")
async def capture(
    request: Request,
    query: ResearchQuery,
    db: Session = Depends(get_db),
    venue: PublicVenueCapture = Depends(get_capture_venue),
):
    settings = get_settings()
    if not settings.observation_archive_enabled or not settings.native_capture_enabled:
        raise HTTPException(503, "Native archive capture disabled")
    try:
        async with asyncio.timeout(30):
            result = await capture_market_books(db, query.condition_id.lower(), venue)
        db.commit()
        logger.info("Book resnapshot condition=%s status=%s", query.condition_id, result["status"])
        return result
    except (ValueError, httpx.HTTPError, TimeoutError, SQLAlchemyError) as exc:
        db.rollback()
        raise HTTPException(503, "Public book capture unavailable") from exc


@router.post("/runs", dependencies=[Depends(enabled)])
def freeze(
    query: ResearchQuery, account: Account = Depends(owned_account), db: Session = Depends(get_db)
):
    try:
        result = save_research(db, query, account)
        db.commit()
        return result
    except (ValueError, SQLAlchemyError) as exc:
        db.rollback()
        raise HTTPException(409, "Book research could not be frozen") from exc


@router.get("/runs/{run_id}", dependencies=[Depends(enabled)])
def replay(run_id: str, account: Account = Depends(owned_account), db: Session = Depends(get_db)):
    row = (
        db.query(BookResearchRun)
        .filter_by(id=run_id, user_id=account.owner_user_id, account_id=account.id)
        .first()
    )
    if row is None:
        raise HTTPException(404, "Book research not found")
    try:
        return replay_research(db, row)
    except (ValueError, LookupError) as exc:
        raise HTTPException(409, "Book research integrity check failed") from exc
