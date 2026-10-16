"""Collaborative forecasting API (plan 02).

Routes under ``/api/collaboration``: submit and read probability votes,
read the blended consensus, edit research annotations, follow other
forecasters, and the forecasting-accuracy board.

Every handler authenticates with ``get_current_user_from_token`` (the
same cookie/bearer dependency the rest of the app uses; there is no
bearer token in the frontend) and most also take the active-account
seam, so a vote written while an account is selected is attributed to
that account.

Privacy contract enforced here, not in the UI:

* annotations and votes are private unless the caller says otherwise, and
* ``GET /annotations/{condition_id}`` returns the caller's own row plus
  public rows only — another user's private note is invisible, not
  redacted.

``GET /leaderboard`` mirrors the calibration route's scoping rule: the
default board shows public forecasters, and ``scope=global`` (which
additionally exposes private participation counts) requires an admin.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_active_account
from app.api.routes.auth.auth import get_current_user_from_token
from app.models.intelligence.collaboration import VISIBILITY_VALUES
from app.models.users.account import Account
from app.security.rate_limit import limiter
from app.services.intelligence import consensus_service
from app.services.intelligence.consensus_service import (
    DEFAULT_WINDOW_DAYS,
    VoteRejected,
    delete_annotation,
    forecaster_leaderboard,
    forecaster_skill,
    get_consensus,
    list_annotations,
    render_consensus_prompt,
    save_annotation,
    submit_vote,
)
from app.services.intelligence.forecaster_follow_service import (
    FollowRejected,
    follow_user,
    following_forecasts,
    list_following,
    unfollow_user,
)
from app.utils.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/collaboration", tags=["collaboration"])

#: Service rejection codes mapped to HTTP statuses. A malformed forecast
#: is the caller's error; a closed market or an exhausted rate limit is a
#: conflict with server state.
_REJECTION_STATUS = {
    "invalid_probability": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "invalid_market": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "invalid_visibility": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "empty_body": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "body_too_long": status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
    "market_closed": status.HTTP_409_CONFLICT,
    "market_resolving": status.HTTP_409_CONFLICT,
    "rate_limited": status.HTTP_429_TOO_MANY_REQUESTS,
    "self_follow": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "unknown_user": status.HTTP_404_NOT_FOUND,
}


@limiter.limit("60/minute")
def _collaboration_read_rate_limit(request: Request) -> None:
    """Rate-limit collaboration reads: 60/minute per IP."""


@limiter.limit("20/minute")
def _collaboration_write_rate_limit(request: Request) -> None:
    """Rate-limit collaboration writes: 20/minute per IP.

    The per-user vote budget (``consensus_service.MAX_VOTES_PER_HOUR``)
    is the abuse control that matters; this is the coarse outer bound.
    """


# ── Schemas ─────────────────────────────────────────────────────


class VoteRequest(BaseModel):
    """One probability estimate. ``(0, 1)`` strictly — no certainties."""

    probability: float = Field(..., gt=0.0, lt=1.0)
    visibility: str = Field(default="private", pattern=f"^({'|'.join(VISIBILITY_VALUES)})$")


class AnnotationRequest(BaseModel):
    """A research note, optionally carrying a stated probability."""

    body: str = Field(..., min_length=1, max_length=20000)
    probability: float | None = Field(default=None, gt=0.0, lt=1.0)
    visibility: str = Field(default="private", pattern=f"^({'|'.join(VISIBILITY_VALUES)})$")


def _account_id(active_account: Account) -> int | None:
    """Attribute the row to the selected account when one is resolved."""
    return active_account.id if active_account is not None else None


def _reject(exc: VoteRejected | FollowRejected) -> HTTPException:
    """Translate a service rejection into the mapped HTTP status."""
    return HTTPException(
        status_code=_REJECTION_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail=exc.detail,
    )


# ── Votes ───────────────────────────────────────────────────────


@router.put("/votes/{condition_id}", dependencies=[Depends(_collaboration_write_rate_limit)])
def put_vote(
    condition_id: str,
    body: VoteRequest,
    current_user: dict = Depends(get_current_user_from_token),
    active_account: Account = Depends(get_active_account),
    db: Session = Depends(get_db),
):
    """Record or revise the caller's forecast on one market.

    Revising appends the next ``vote_seq`` rather than overwriting, so a
    consensus read always pools exactly one vote per forecaster and the
    previous estimate stays auditable.

    The body's ``visibility`` is the caller's decision: it starts at
    ``private`` and only a ``public`` vote enters the consensus and the
    accuracy board. Because the wallet is the public handle, the UI
    states that link before a vote is made public.
    """
    try:
        vote = submit_vote(
            db,
            current_user["user_id"],
            condition_id,
            body.probability,
            account_id=_account_id(active_account),
            visibility=body.visibility,
        )
    except VoteRejected as exc:
        raise _reject(exc) from exc
    return {
        "id": vote.id,
        "condition_id": vote.condition_id,
        "probability": vote.probability,
        "vote_seq": vote.vote_seq,
        "visibility": vote.visibility,
        "created_at": vote.created_at.isoformat() if vote.created_at else None,
        "advisory": "informational",
    }


# ── Consensus ───────────────────────────────────────────────────


@router.get("/consensus/batch", dependencies=[Depends(_collaboration_read_rate_limit)])
def get_consensus_batch(
    condition_ids: str = Query(..., description="Comma-separated market condition ids"),
    exclude_self: bool = Query(default=True),
    window_days: int = Query(default=DEFAULT_WINDOW_DAYS, ge=1, le=365),
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Consensus for several markets at once (one cache read each).

    Declared before ``/consensus/{condition_id}`` so ``batch`` is not
    captured as a condition id.
    """
    keys = [key.strip() for key in (condition_ids or "").split(",") if key.strip()]
    if not keys:
        raise HTTPException(status_code=422, detail="No condition ids supplied")
    if len(keys) > 50:
        raise HTTPException(status_code=422, detail="At most 50 condition ids per batch")
    return {
        "count": len(keys),
        "consensus": {
            key: get_consensus(
                db,
                key,
                viewer_user_id=current_user["user_id"],
                exclude_self=exclude_self,
                window_days=window_days,
            )
            for key in keys
        },
    }


@router.get("/consensus/{condition_id}", dependencies=[Depends(_collaboration_read_rate_limit)])
def get_market_consensus(
    condition_id: str,
    exclude_self: bool = Query(default=True),
    window_days: int = Query(default=DEFAULT_WINDOW_DAYS, ge=1, le=365),
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Public consensus for one market, blended with the AI probability.

    Returns the AI probability, the weighted consensus, the market price,
    the disagreement gap between them and the contributor list. It is a
    description of what other forecasters believe — not a
    recommendation, and nothing here places an order.

    Args:
        exclude_self: Drop the caller's own vote from the pool. Default
            true, because a portfolio view that echoes only the reader's
            own opinion is confirmation, not information.
    """
    consensus = get_consensus(
        db,
        condition_id,
        viewer_user_id=current_user["user_id"],
        exclude_self=exclude_self,
        window_days=window_days,
    )
    consensus["narrative"] = render_consensus_prompt(
        condition_id,
        ai_probability=consensus["ai_probability"],
        consensus=consensus["consensus"],
        market_price=consensus["market_price"],
        gap=consensus["gap"],
        n_votes=consensus["n_votes"],
        disagreement=consensus["disagreement"],
    )
    return consensus


# ── Annotations ─────────────────────────────────────────────────


@router.get("/annotations/{condition_id}", dependencies=[Depends(_collaboration_read_rate_limit)])
def get_market_annotations(
    condition_id: str,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """The caller's own note plus the market's public notes.

    A private note belongs to its author alone: another user's row is
    never returned, and is not counted or hinted at either.
    """
    rows = list_annotations(db, condition_id, current_user["user_id"])

    def _payload(row):
        return {
            "id": row.id,
            "user_id": row.user_id,
            "condition_id": row.condition_id,
            "body": row.body,
            "probability": row.probability,
            "visibility": row.visibility,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "mine": row.user_id == current_user["user_id"],
        }

    return {
        "condition_id": condition_id,
        "count": len(rows),
        "annotations": [_payload(row) for row in rows],
    }


@router.put("/annotations/{condition_id}", dependencies=[Depends(_collaboration_write_rate_limit)])
def put_market_annotation(
    condition_id: str,
    body: AnnotationRequest,
    current_user: dict = Depends(get_current_user_from_token),
    active_account: Account = Depends(get_active_account),
    db: Session = Depends(get_db),
):
    """Create or replace the caller's single note on a market."""
    try:
        annotation = save_annotation(
            db,
            current_user["user_id"],
            condition_id,
            body=body.body,
            probability=body.probability,
            visibility=body.visibility,
            account_id=_account_id(active_account),
        )
    except VoteRejected as exc:
        raise _reject(exc) from exc
    return {
        "id": annotation.id,
        "condition_id": annotation.condition_id,
        "body": annotation.body,
        "probability": annotation.probability,
        "visibility": annotation.visibility,
        "updated_at": annotation.updated_at.isoformat() if annotation.updated_at else None,
    }


@router.delete(
    "/annotations/{condition_id}",
    dependencies=[Depends(_collaboration_write_rate_limit)],
)
def delete_market_annotation(
    condition_id: str,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Withdraw the caller's own note."""
    if not delete_annotation(db, current_user["user_id"], condition_id):
        raise HTTPException(status_code=404, detail="No annotation to withdraw")
    return {"deleted": True}


# ── Forecaster graph ────────────────────────────────────────────


@router.post("/follow/{user_id}", dependencies=[Depends(_collaboration_write_rate_limit)])
def post_follow(
    user_id: int,
    current_user: dict = Depends(get_current_user_from_token),
    active_account: Account = Depends(get_active_account),
    db: Session = Depends(get_db),
):
    """Follow a forecaster (idempotent).

    Following surfaces their *public* forecasts; it never mirrors trades
    and never reveals a private note or vote.
    """
    try:
        row = follow_user(
            db,
            current_user["user_id"],
            user_id,
            account_id=_account_id(active_account),
        )
    except FollowRejected as exc:
        raise _reject(exc) from exc
    return {
        "follower_user_id": row.follower_user_id,
        "followee_user_id": row.followee_user_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.delete("/follow/{user_id}", dependencies=[Depends(_collaboration_write_rate_limit)])
def delete_follow(
    user_id: int,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Stop following a forecaster."""
    if not unfollow_user(db, current_user["user_id"], user_id):
        raise HTTPException(status_code=404, detail="Not following that forecaster")
    return {"unfollowed": True}


@router.get("/forecasters", dependencies=[Depends(_collaboration_read_rate_limit)])
def get_followed_forecasters(
    limit: int = Query(default=50, ge=1, le=200),
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Who the caller follows, plus their latest public forecasts.

    Each forecast carries the forecaster's measured skill so the feed
    shows a track record next to the estimate.
    """
    following = list_following(db, current_user["user_id"])
    forecasts = following_forecasts(db, current_user["user_id"], limit=limit)
    return {
        "count": len(following),
        "following": following,
        "forecasts": forecasts,
    }


# ── Forecaster accuracy board ───────────────────────────────────


@router.get("/leaderboard", dependencies=[Depends(_collaboration_read_rate_limit)])
def get_forecaster_leaderboard(
    window_days: int = Query(default=DEFAULT_WINDOW_DAYS, ge=1, le=365),
    scope: str = Query(default="user", pattern="^(user|global)$"),
    limit: int = Query(default=100, ge=1, le=500),
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Forecasting accuracy, best-first: skill, resolved count, public votes.

    ``scope=user`` (default) is the public board. ``scope=global``
    additionally includes forecasters with only private votes and their
    participation counts, and therefore requires an admin — the same rule
    ``/api/analysis/calibration`` applies to its global scope.
    """
    if scope == "global" and not current_user.get("is_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="scope=global requires an admin account",
        )
    board = forecaster_leaderboard(db, window_days, scope=scope, limit=limit)
    board["scope"] = scope
    return board


@router.get("/skill/{user_id}", dependencies=[Depends(_collaboration_read_rate_limit)])
def get_forecaster_skill(
    user_id: int,
    window_days: int = Query(default=DEFAULT_WINDOW_DAYS, ge=1, le=365),
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """One forecaster's measured accuracy (their own row or an admin's view).

    The skill is computed from every forecast the user recorded, private
    ones included, so making a forecast private cannot hide a bad one;
    the *list* of benchmarks stays out of this response, and a stranger
    cannot read another user's row.
    """
    if user_id != current_user["user_id"] and not current_user.get("is_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the forecaster or an admin can read this skill",
        )
    skill = forecaster_skill(db, user_id, window_days)
    skill["skill_weight_k"] = consensus_service.SKILL_WEIGHT_K
    skill["weight"] = consensus_service.vote_weight(skill)
    return skill
