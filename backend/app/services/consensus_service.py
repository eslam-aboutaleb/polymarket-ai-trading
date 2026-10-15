"""Consensus forecasting core (plan 02).

An ensemble of human probability estimates, weighted by each
forecaster's *measured* calibration, blended with the AI probability the
``Assessment`` leg already stores.

The three ideas that make this more than a vote counter:

1. **Weights come from evidence, not reputation.**
   ``w = 1 + SKILL_WEIGHT_K · skill`` where ``skill`` is the Brier skill
   of the user's own forecasts against the 0.25 baseline that plan 04's
   drift service uses (``BASELINE_BRIER`` is imported from it so the two
   services cannot drift apart), clipped at zero. A user with fewer than
   ``MIN_RESOLVED_FOR_SKILL`` resolved forecasts gets weight exactly
   ``1.0``: a newcomer must be able to participate, and one lucky market
   must not buy double weight. Skill is measured over *every* vote a
   user made, private ones included, so making a forecast private cannot
   launder a bad one out of the score.
2. **Only public votes are pooled.** Visibility defaults to private and
   a consensus read selects ``public`` rows only. A wallet-linked vote
   is linkable to a wallet, so publishing one is a choice the forecaster
   makes — the UI states that before it happens.
3. **The output is a description, not an instruction.** The payload
   carries the AI probability, the consensus, the market price and the
   disagreement *gap* between them. Nothing here is a trade signal and
   no route in this module places an order.

Abuse controls live in the write path: votes are rate-limited per user,
markets resolving within ``VOTE_CLOSE_GUARD_SECONDS`` are closed to new
votes, and ``get_consensus(exclude_self=...)`` drops a viewer's own vote
so a portfolio view is never an argument with itself.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.grpc_clients.analysis_client import _load_prompts
from app.models.assessment import Assessment
from app.models.collaboration import (
    VISIBILITY_PRIVATE,
    VISIBILITY_PUBLIC,
    ForecastVote,
    MarketAnnotation,
)
from app.models.market import Market
from app.models.user import User
from app.services.forecast_resolution import brier_score
from app.services.model_drift import BASELINE_BRIER
from app.utils.cache import get_cache
from app.utils.time import utc_now

logger = logging.getLogger(__name__)

#: Skill multiplier for the ensemble weight. ``w = 1 + k · skill`` with
#: ``skill ∈ [0, 0.25]``, so ``k = 4`` bounds every forecaster at twice
#: a newcomer's weight: a perfect forecaster (Brier 0) counts double,
#: never tenfold. A larger ``k`` would let one skilled account dominate
#: a crowd of newcomers — exactly the herding failure the weights exist
#: to prevent.
SKILL_WEIGHT_K = 4.0

#: Resolved forecasts a user needs before their skill is trusted at all.
#: Below this the weight is the newcomer default of 1.0.
MIN_RESOLVED_FOR_SKILL = 3

#: Rolling window for the forecaster's own accuracy (days).
DEFAULT_WINDOW_DAYS = 30

#: Consensus snapshots are cached for this long and dropped on a vote.
CONSENSUS_CACHE_TTL_SECONDS = 60

#: How many revisions one user may record across all markets per hour.
MAX_VOTES_PER_HOUR = 30
VOTE_RATE_WINDOW_SECONDS = 3600

#: A market this close to its end date is closed to new votes: the last
#: hour is where an already-known outcome is worth the most to a
#: manipulator and the least to a forecaster.
VOTE_CLOSE_GUARD_SECONDS = 3600

#: Local market statuses treated as open. Anything else is refused.
OPEN_MARKET_STATUSES = {"active", "open"}

#: Cap on the contributor list returned with a consensus, so one
#: crowded market cannot turn the panel into a directory.
MAX_CONTRIBUTORS = 25

# One process-wide consensus cache. ``get_cache`` is a factory — it hands
# back a new in-memory (or Redis) client per call — so a consensus read
# and the vote that must invalidate it have to share a single instance.
_consensus_cache = get_cache("consensus")


class VoteRejected(ValueError):
    """A vote the service refuses to write, with a machine-readable code.

    The route maps ``code`` to a status; the service stays free of HTTP
    concerns.
    """

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


# ── coercion helpers ────────────────────────────────────────────


def _coerce_probability(value: Any) -> float | None:
    """Coerce to a float strictly inside ``(0, 1)``; ``None`` otherwise.

    Mirrors ``forecast_recorder._coerce_probability`` so a human vote and
    an AI verdict agree on what a usable forecast is. A "certainty" is
    rejected rather than clamped: clamping 1.0 to 0.99 would fabricate a
    belief the forecaster did not state.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if 0.0 < number < 1.0 else None


def _as_utc(value: Any) -> Any:
    """Treat a naive datetime as UTC (SQLite drops the offset on read)."""
    if value is None or getattr(value, "tzinfo", None) is not None:
        return value
    return value.replace(tzinfo=utc_now().tzinfo)


# ── forecaster accuracy ─────────────────────────────────────────


def _condition_resolutions(db: Session, condition_ids: list[str]) -> dict[str, str]:
    """Latest resolved outcome per condition id.

    Resolution is written once per market by the nightly job on the
    ``Assessment`` row, so a human forecast is scored with exactly the
    outcome the AI's own forecast on that market was scored with — the
    two legs of the ensemble are graded by the same grader.
    """
    if not condition_ids:
        return {}
    rows = (
        db.query(Assessment.condition_id, Assessment.resolved_outcome, Assessment.created_at)
        .filter(
            Assessment.condition_id.in_(condition_ids),
            Assessment.resolved_outcome.isnot(None),
        )
        .order_by(Assessment.created_at.asc())
        .all()
    )
    return {row.condition_id: row.resolved_outcome for row in rows}


def forecaster_skill(
    db: Session,
    user_id: int,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> dict[str, Any]:
    """Brier skill of one user's forecasts over the rolling window.

    Returns the mean Brier score of the user's *resolved* forecasts, the
    skill as ``baseline - mean`` clipped at zero, and how many forecasts
    the number is based on. The skill is only meaningful once
    ``sufficient_samples`` is true; until then callers must use the
    newcomer weight of 1.0 rather than an optimistic estimate.
    """
    window_days = max(1, int(window_days))
    cutoff = utc_now() - timedelta(days=window_days)
    # Window-scoped so a very active forecaster cannot turn a consensus
    # read into a scan of every forecast they ever cast.
    latest = latest_votes_for_users(db, [user_id], since=cutoff)
    condition_ids = sorted({vote.condition_id for vote in latest})
    resolutions = _condition_resolutions(db, condition_ids)

    scores = []
    for vote in latest:
        outcome = resolutions.get(vote.condition_id)
        if outcome is None:
            continue
        scores.append(brier_score(float(vote.probability), outcome))

    resolved = len(scores)
    mean_brier = (sum(scores) / resolved) if resolved else None
    skill = 0.0
    if mean_brier is not None:
        skill = max(0.0, BASELINE_BRIER - mean_brier)
    return {
        "user_id": user_id,
        "window_days": window_days,
        "baseline_brier": BASELINE_BRIER,
        "votes": len(latest),
        "resolved": resolved,
        "mean_brier": round(mean_brier, 4) if mean_brier is not None else None,
        "skill": round(skill, 4),
        "sufficient_samples": resolved >= MIN_RESOLVED_FOR_SKILL,
    }


def vote_weight(skill: dict[str, Any]) -> float:
    """Ensemble weight for one forecaster.

    Never below 1.0: a user with no measurable track record still
    participates at full newcomer weight, and a user who is worse than
    the coin-flip baseline is *not* negatively weighted — silencing a bad
    forecaster is a moderation decision, not an arithmetic one.
    """
    if not skill.get("sufficient_samples"):
        return 1.0
    return 1.0 + SKILL_WEIGHT_K * max(0.0, float(skill.get("skill") or 0.0))


# ── vote writes ─────────────────────────────────────────────────


def _assert_market_open(db: Session, condition_id: str, now: Any) -> None:
    """Refuse votes on closed or imminently-resolving markets.

    The local ``markets`` table is a cache of public venue data, so an
    absent row proves nothing either way: the vote is allowed and the
    guard applies to the markets the cache happens to know about.
    """
    market = db.query(Market).filter(Market.id == condition_id).first()
    if market is None:
        return
    status = (market.status or "").strip().lower()
    if status and status not in OPEN_MARKET_STATUSES:
        raise VoteRejected(
            "market_closed",
            f"Market {condition_id} is {status or 'closed'} and no longer accepts votes",
        )
    end_date = _as_utc(market.end_date)
    if end_date is None:
        return
    if end_date <= now + timedelta(seconds=VOTE_CLOSE_GUARD_SECONDS):
        raise VoteRejected(
            "market_resolving",
            "Market resolves within an hour; votes are closed to prevent "
            "voting on an outcome that is already known",
        )


def _assert_within_vote_rate(db: Session, user_id: int, now: Any) -> None:
    """Per-user rate limit on vote revisions (the IP limit is separate)."""
    window_start = now - timedelta(seconds=VOTE_RATE_WINDOW_SECONDS)
    recent = (
        db.query(func.count(ForecastVote.id))
        .filter(
            ForecastVote.user_id == user_id,
            ForecastVote.created_at >= window_start,
        )
        .scalar()
        or 0
    )
    if int(recent) >= MAX_VOTES_PER_HOUR:
        raise VoteRejected(
            "rate_limited",
            f"Vote limit reached: at most {MAX_VOTES_PER_HOUR} votes per user per hour",
        )


def submit_vote(
    db: Session,
    user_id: int,
    condition_id: str,
    probability: Any,
    *,
    account_id: int | None = None,
    visibility: str = VISIBILITY_PRIVATE,
    now: Any = None,
) -> ForecastVote:
    """Record a forecast, appending the next revision of the user's vote.

    One active vote per ``(user, condition_id)`` is enforced here by
    allocating ``max(vote_seq) + 1``; the unique index on that triple
    turns a lost race into an error instead of two "latest" votes.

    Raises:
        VoteRejected: the probability is outside ``(0, 1)``, the market
            is closed or resolving within an hour, or the per-user vote
            rate limit is exhausted.
    """
    market_key = (condition_id or "").strip()
    if not market_key:
        raise VoteRejected("invalid_market", "A condition_id is required")

    value = _coerce_probability(probability)
    if value is None:
        raise VoteRejected("invalid_probability", "probability must be strictly inside (0, 1)")

    if visibility not in (VISIBILITY_PRIVATE, VISIBILITY_PUBLIC):
        raise VoteRejected("invalid_visibility", "visibility must be 'private' or 'public'")

    moment = now or utc_now()
    _assert_market_open(db, market_key, moment)
    _assert_within_vote_rate(db, user_id, moment)

    last_seq = (
        db.query(func.max(ForecastVote.vote_seq))
        .filter(ForecastVote.user_id == user_id, ForecastVote.condition_id == market_key)
        .scalar()
        or 0
    )
    vote = ForecastVote(
        user_id=user_id,
        account_id=account_id,
        condition_id=market_key,
        probability=value,
        vote_seq=int(last_seq) + 1,
        visibility=visibility,
        created_at=moment,
    )
    db.add(vote)
    db.commit()
    db.refresh(vote)
    # A new revision changes every consensus that pooled the old one.
    invalidate_consensus(market_key, user_id)
    return vote


# ── consensus reads ─────────────────────────────────────────────


def latest_votes(
    db: Session,
    condition_id: str,
    *,
    visibility: str = VISIBILITY_PUBLIC,
) -> list[ForecastVote]:
    """The active vote of every user on one market, oldest revision first.

    "Active" is the highest ``vote_seq`` per user, so a forecaster who
    revised their estimate is counted exactly once.
    """
    rows = (
        db.query(ForecastVote)
        .filter(ForecastVote.condition_id == condition_id, ForecastVote.visibility == visibility)
        .order_by(ForecastVote.vote_seq.asc())
        .all()
    )
    latest: dict[int, ForecastVote] = {}
    for row in rows:
        latest[row.user_id] = row
    return list(latest.values())


def latest_votes_for_users(
    db: Session,
    user_ids: list[int],
    *,
    visibility: str | None = None,
    since: Any = None,
) -> list[ForecastVote]:
    """The active vote of each given user on every market they voted on.

    Args:
        since: Optional lower bound on ``created_at``. Passed by the
            accuracy read so a window-scoped board never scans a
            forecaster's entire history.
    """
    if not user_ids:
        return []
    query = db.query(ForecastVote).filter(ForecastVote.user_id.in_(user_ids))
    if since is not None:
        query = query.filter(ForecastVote.created_at >= since)
    if visibility is not None:
        query = query.filter(ForecastVote.visibility == visibility)
    rows = query.order_by(ForecastVote.vote_seq.asc()).all()
    latest: dict[tuple[int, str], ForecastVote] = {}
    for row in rows:
        latest[(row.user_id, row.condition_id)] = row
    return list(latest.values())


def _contributor_handles(db: Session, user_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Display handles for the contributor list (the wallet is the handle)."""
    if not user_ids:
        return {}
    users = db.query(User).filter(User.id.in_(user_ids)).all()
    return {
        user.id: {
            "user_id": user.id,
            "display_name": user.display_name,
            "wallet_address": user.wallet_address,
        }
        for user in users
    }


def _ai_leg(db: Session, condition_id: str) -> tuple[float | None, float | None]:
    """Latest AI probability and the market price it was taken against."""
    row = (
        db.query(Assessment)
        .filter(
            Assessment.condition_id == condition_id,
            Assessment.ai_probability.isnot(None),
        )
        .order_by(Assessment.created_at.desc())
        .first()
    )
    ai_probability = float(row.ai_probability) if row is not None else None
    market_price = None
    if row is not None and row.market_price_at_assessment is not None:
        market_price = float(row.market_price_at_assessment)
    else:
        market = db.query(Market.current_price).filter(Market.id == condition_id).first()
        if market is not None and market.current_price is not None:
            market_price = float(market.current_price)
    return ai_probability, market_price


def _ensemble(
    votes: list[ForecastVote],
    weights: dict[int, float],
) -> tuple[float | None, float | None]:
    """Weighted mean and the spread of the pooled estimates.

    The spread is how much the crowd disagrees with itself, which is the
    number that tells a reader whether the consensus means anything.
    """
    if not votes:
        return None, None
    total_weight = sum(weights.get(vote.user_id, 1.0) for vote in votes)
    if total_weight <= 0:
        # Unreachable by construction (every weight is >= 1.0), but a
        # zero denominator must never become a division by zero.
        return None, None
    total = sum(weights.get(vote.user_id, 1.0) * float(vote.probability) for vote in votes)
    spread = max(float(vote.probability) for vote in votes) - min(
        float(vote.probability) for vote in votes
    )
    return total / total_weight, spread


def get_consensus(
    db: Session,
    condition_id: str,
    *,
    viewer_user_id: int | None = None,
    exclude_self: bool = True,
    window_days: int = DEFAULT_WINDOW_DAYS,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Public consensus for one market, blended with the AI probability.

    Args:
        db: Active session.
        condition_id: Market condition id.
        viewer_user_id: Caller, when authenticated.
        exclude_self: Drop the caller's own vote from the pool. This is
            the portfolio-view rule: a consensus a user cannot disagree
            with is not information, and seeing only your own opinion
            reflected back reads as confirmation.
        window_days: Accuracy window the weights are measured over.
        use_cache: Serve the 60s snapshot when present.

    Returns:
        ``consensus`` (weighted mean, or ``None`` with no public votes),
        the AI leg, the market price, the disagreement gaps and the
        contributor list. ``gap`` is consensus vs market price (the crowd
        against the price), ``ai_gap`` is consensus vs the AI.
    """
    market_key = (condition_id or "").strip()
    viewer_key = "shared"
    if exclude_self and viewer_user_id is not None:
        viewer_key = str(viewer_user_id)

    if use_cache:
        cached = _consensus_cache.get(_cache_key(market_key, viewer_key))
        if cached is not None:
            payload = dict(cached)
            payload["cached"] = True
            return payload

    votes = [
        vote
        for vote in latest_votes(db, market_key, visibility=VISIBILITY_PUBLIC)
        if not (exclude_self and viewer_user_id is not None and vote.user_id == viewer_user_id)
    ]
    skills = {vote.user_id: forecaster_skill(db, vote.user_id, window_days) for vote in votes}
    weights = {user_id: vote_weight(skill) for user_id, skill in skills.items()}
    consensus, spread = _ensemble(votes, weights)
    ai_probability, market_price = _ai_leg(db, market_key)

    gap = None
    if consensus is not None and market_price is not None:
        gap = consensus - market_price
    ai_gap = None
    if consensus is not None and ai_probability is not None:
        ai_gap = consensus - ai_probability

    handles = _contributor_handles(db, [vote.user_id for vote in votes])
    contributors = [
        {
            **handles.get(vote.user_id, {"user_id": vote.user_id}),
            "probability": round(float(vote.probability), 4),
            "weight": round(weights[vote.user_id], 4),
            "skill": skills[vote.user_id]["skill"],
            "resolved": skills[vote.user_id]["resolved"],
            "vote_seq": vote.vote_seq,
        }
        for vote in sorted(votes, key=lambda v: -weights[v.user_id])
    ][:MAX_CONTRIBUTORS]

    payload: dict[str, Any] = {
        "condition_id": market_key,
        "generated_at": utc_now().isoformat(),
        "cached": False,
        # Informational only: a consensus is a description of what other
        # forecasters believe, never a recommendation to trade.
        "advisory": "informational",
        "window_days": window_days,
        "n_votes": len(votes),
        "n_contributors": len(votes),
        "consensus": round(consensus, 4) if consensus is not None else None,
        "ai_probability": round(ai_probability, 4) if ai_probability is not None else None,
        "market_price": round(market_price, 4) if market_price is not None else None,
        "gap": round(gap, 4) if gap is not None else None,
        "ai_gap": round(ai_gap, 4) if ai_gap is not None else None,
        "disagreement": round(spread, 4) if spread is not None else None,
        "excluded_self": bool(exclude_self and viewer_user_id is not None),
        "contributors": contributors,
    }

    if use_cache:
        _consensus_cache.set(
            _cache_key(market_key, viewer_key),
            payload,
            CONSENSUS_CACHE_TTL_SECONDS,
        )
    return payload


def _cache_key(condition_id: str, viewer_key: str) -> str:
    """Cache key: one entry per market per viewer variant."""
    return f"{condition_id}:{viewer_key}"


def reset_consensus_cache() -> None:
    """Drop every cached consensus snapshot.

    The test seam (mirroring ``prompt_version.reset_prompt_version``):
    the cache is a process-wide singleton, so a test that mutates votes
    needs a way to start from an empty one.
    """
    _consensus_cache.clear()


def invalidate_consensus(condition_id: str, user_id: int | None = None) -> None:
    """Drop the cached consensus snapshot for one market.

    Both the shared entry and the voting user's own view are dropped:
    their view changed by definition (their vote moved), and the shared
    one is what every other reader sees. Other viewers' entries expire
    within the 60s TTL rather than being reachable by key pattern, which
    is the trade-off that keeps the cache backend-agnostic.
    """
    _consensus_cache.delete(_cache_key(condition_id, "shared"))
    if user_id is not None:
        _consensus_cache.delete(_cache_key(condition_id, str(user_id)))


# ── forecaster board ────────────────────────────────────────────


def _active_votes(db: Session, cutoff: Any) -> list[ForecastVote]:
    """Every active vote inside the window (latest revision per user/market)."""
    rows = (
        db.query(ForecastVote)
        .filter(ForecastVote.created_at >= cutoff)
        .order_by(ForecastVote.vote_seq.asc())
        .all()
    )
    latest: dict[tuple[int, str], ForecastVote] = {}
    for row in rows:
        latest[(row.user_id, row.condition_id)] = row
    return list(latest.values())


def forecaster_leaderboard(
    db: Session,
    window_days: int = DEFAULT_WINDOW_DAYS,
    *,
    scope: str = "user",
    limit: int = 100,
) -> dict[str, Any]:
    """Per-forecaster accuracy, best-first, over the rolling window.

    ``scope=user`` (default) is the public board: only forecasters who
    published at least one vote appear, ranked by Brier skill against the
    0.25 baseline, then by how many forecasts that skill is based on.
    ``scope=global`` adds forecasters whose votes are all private and
    reports their private participation counts, which is why it is
    admin-only — the same rule ``calibration`` applies to its global
    scope.

    The board is window-scoped: a vote outside the window is neither
    scored nor counted, so the whole query is bounded by the window.
    """
    window_days = max(1, int(window_days))
    cutoff = utc_now() - timedelta(days=window_days)
    votes = _active_votes(db, cutoff)
    if scope != "global":
        public_users = {vote.user_id for vote in votes if vote.visibility == VISIBILITY_PUBLIC}
        votes = [vote for vote in votes if vote.user_id in public_users]
    if not votes:
        return {
            "window_days": window_days,
            "baseline_brier": BASELINE_BRIER,
            "scope": scope,
            "count": 0,
            "forecasters": [],
        }

    by_user: dict[int, list[ForecastVote]] = {}
    for vote in votes:
        by_user.setdefault(vote.user_id, []).append(vote)
    resolutions = _condition_resolutions(db, sorted({vote.condition_id for vote in votes}))
    handles = _contributor_handles(db, sorted(by_user))

    forecasters = []
    for user_id, user_votes in by_user.items():
        scores = [
            brier_score(float(vote.probability), resolutions.get(vote.condition_id))
            for vote in user_votes
            if resolutions.get(vote.condition_id) is not None
        ]
        resolved = len(scores)
        mean_brier = (sum(scores) / resolved) if resolved else None
        skill = 0.0
        if mean_brier is not None:
            skill = max(0.0, BASELINE_BRIER - mean_brier)
        public_votes = sum(1 for vote in user_votes if vote.visibility == VISIBILITY_PUBLIC)
        skill_row = {
            "skill": skill,
            "resolved": resolved,
            "sufficient_samples": resolved >= MIN_RESOLVED_FOR_SKILL,
        }
        forecasters.append(
            {
                **handles.get(user_id, {"user_id": user_id}),
                "skill": round(skill, 4),
                "mean_brier": round(mean_brier, 4) if mean_brier is not None else None,
                "resolved": resolved,
                "public_votes": public_votes,
                "total_votes": len(user_votes),
                "weight": round(vote_weight(skill_row), 4),
            }
        )
    forecasters.sort(key=lambda r: (-r["skill"], -r["resolved"], -r["public_votes"], r["user_id"]))
    return {
        "window_days": window_days,
        "baseline_brier": BASELINE_BRIER,
        "scope": scope,
        "count": len(forecasters),
        "forecasters": forecasters[: max(1, int(limit))],
    }


# ── annotations ─────────────────────────────────────────────────


def get_annotation(
    db: Session,
    user_id: int,
    condition_id: str,
) -> MarketAnnotation | None:
    """The caller's own note on a market (their row, whatever visibility)."""
    return (
        db.query(MarketAnnotation)
        .filter(
            MarketAnnotation.user_id == user_id,
            MarketAnnotation.condition_id == condition_id,
        )
        .first()
    )


def list_annotations(
    db: Session,
    condition_id: str,
    user_id: int,
    limit: int = 50,
) -> list[MarketAnnotation]:
    """The caller's note plus every *public* note on the market.

    A private note never leaves its author's own response, so a reader
    cannot even tell one exists.
    """
    rows = (
        db.query(MarketAnnotation)
        .filter(
            MarketAnnotation.condition_id == condition_id,
            MarketAnnotation.visibility == VISIBILITY_PUBLIC,
        )
        .order_by(MarketAnnotation.updated_at.desc())
        .limit(limit)
        .all()
    )
    own = get_annotation(db, user_id, condition_id)
    if own is not None and not any(row.user_id == user_id for row in rows):
        rows.insert(0, own)
    return rows


def save_annotation(
    db: Session,
    user_id: int,
    condition_id: str,
    *,
    body: str,
    probability: Any = None,
    visibility: str = VISIBILITY_PRIVATE,
    account_id: int | None = None,
) -> MarketAnnotation:
    """Create or update the caller's single note on a market.

    Raises:
        VoteRejected: the body is empty, the optional probability is
            outside ``(0, 1)``, or the visibility is not recognised.
    """
    market_key = (condition_id or "").strip()
    text = (body or "").strip()
    if not text:
        raise VoteRejected("empty_body", "An annotation needs a body")
    if len(text) > 20000:
        raise VoteRejected("body_too_long", "Annotation body is limited to 20000 characters")
    if visibility not in (VISIBILITY_PRIVATE, VISIBILITY_PUBLIC):
        raise VoteRejected("invalid_visibility", "visibility must be 'private' or 'public'")

    value = _coerce_probability(probability) if probability is not None else None
    if probability is not None and value is None:
        raise VoteRejected("invalid_probability", "probability must be strictly inside (0, 1)")

    annotation = get_annotation(db, user_id, market_key)
    if annotation is None:
        annotation = MarketAnnotation(
            user_id=user_id,
            account_id=account_id,
            condition_id=market_key,
            body=text,
            probability=value,
            visibility=visibility,
        )
        db.add(annotation)
    else:
        annotation.body = text
        annotation.probability = value
        annotation.visibility = visibility
        if account_id is not None:
            annotation.account_id = account_id
    db.commit()
    db.refresh(annotation)
    return annotation


def delete_annotation(db: Session, user_id: int, condition_id: str) -> bool:
    """Withdraw the caller's own note (never somebody else's)."""
    annotation = get_annotation(db, user_id, condition_id)
    if annotation is None:
        return False
    db.delete(annotation)
    db.commit()
    return True


# ── optional/degradable narration ───────────────────────────────

# ``collaborative.consensus_summary`` (CHEAP tier: a paraphrase of
# three numbers, not a judgment) narrates the disagreement between the
# AI probability, the consensus and the market price. Optional by
# design — a missing or malformed template falls back to the
# deterministic sentence below, so the panel never fails because a
# prompt is absent.


def _consensus_summary_template() -> str:
    """``collaborative.consensus_summary`` from the prompt store, or ``""``."""
    prompts = _load_prompts()
    return prompts.get("collaborative", {}).get("consensus_summary", "")


def _fmt(value: float | None, signed: bool = False) -> str:
    """Render one panel number, or ``n/a`` when the leg is absent."""
    if value is None:
        return "n/a"
    return f"{value:+.4f}" if signed else f"{value:.4f}"


def render_consensus_prompt(
    condition_id: str,
    *,
    ai_probability: float | None = None,
    consensus: float | None = None,
    market_price: float | None = None,
    gap: float | None = None,
    n_votes: int = 0,
    disagreement: float | None = None,
) -> str:
    """Render the consensus-summary prompt for one market.

    The kwargs must match the template's placeholders exactly — the
    prompt-contract test enforces that statically, and a drift would
    raise ``KeyError`` inside a live panel render.

    Returns the rendered prompt, or a deterministic sentence when the
    template is absent: the narration is a convenience, so a missing
    prompt degrades the header instead of failing the request.
    """
    template = _consensus_summary_template()
    if template:
        try:
            return template.format(
                condition_id=condition_id,
                ai_probability=_fmt(ai_probability),
                consensus=_fmt(consensus),
                market_price=_fmt(market_price),
                gap=_fmt(gap, signed=True),
                n_votes=n_votes,
                disagreement=_fmt(disagreement),
            )
        except (KeyError, IndexError, ValueError):
            logger.warning("collaborative.consensus_summary render failed for %s", condition_id)
    return (
        f"Market {condition_id}: AI says {_fmt(ai_probability)}, "
        f"{n_votes} public forecasts average {_fmt(consensus)}, "
        f"the market prices {_fmt(market_price)} "
        f"(gap {_fmt(gap, signed=True)}, spread {_fmt(disagreement)})."
    )
