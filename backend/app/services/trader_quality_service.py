"""
Trader Quality Scoring Service.

Computes a composite quality score (0-100) for each trader based on:
  1. Win Rate (25%) — percentage of profitable trades
  2. PnL Consistency (25%) — stability across 24h/7d/30d periods
  3. Risk-Adjusted Returns (25%) — total PnL relative to volume (efficiency)
  4. Activity & Freshness (25%) — trade count, market diversity, recency

Inspired by dexorynlabs/polymarket-agents and MrFadiAi scoring concepts.
"""
import logging
import math
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional

from sqlalchemy.orm import Session

from app.models.winner import Winner
from app.utils.time import utc_now

logger = logging.getLogger(__name__)

TIER_THRESHOLDS = {
    "S": 85,
    "A": 70,
    "B": 55,
    "C": 40,
    "D": 0,
}


def _win_rate_score(win_rate: float) -> float:
    """Score from 0-100 based on win rate."""
    if win_rate is None or win_rate <= 0:
        return 0.0
    if win_rate >= 80:
        return 100.0
    if win_rate >= 70:
        return 85.0 + (win_rate - 70) * 1.5
    if win_rate >= 60:
        return 70.0 + (win_rate - 60) * 1.5
    if win_rate >= 50:
        return 50.0 + (win_rate - 50) * 2.0
    return max(0, win_rate * 1.0)


def _consistency_score(pnl_24h: float, pnl_7d: float, pnl_30d: float, total_pnl: float) -> float:
    """Measure PnL consistency across time periods."""
    periods = [pnl_24h or 0, pnl_7d or 0, pnl_30d or 0]
    total = total_pnl or 0
    positive_periods = sum(1 for p in periods if p > 0)
    total_positive = 1 if total > 0 else 0

    if all(p > 0 for p in periods) and total > 0:
        base = 80.0
    elif positive_periods >= 2 and total > 0:
        base = 60.0
    elif positive_periods >= 1 and total > 0:
        base = 40.0
    elif total > 0:
        base = 25.0
    else:
        positivity_ratio = (positive_periods + total_positive) / 4.0
        base = max(0, positivity_ratio * 30)

    if pnl_7d and pnl_30d and pnl_30d > 0:
        weekly_share = pnl_7d / pnl_30d
        if 0.15 <= weekly_share <= 0.35:
            base += 15
        elif 0.05 <= weekly_share <= 0.5:
            base += 8

    return min(100.0, max(0.0, base))


def _risk_adjusted_score(total_pnl: float, volume: float) -> float:
    """Risk-adjusted return approximation: PnL / Volume ratio."""
    if not volume or volume <= 0:
        return 0.0
    if not total_pnl:
        return 0.0
    efficiency = total_pnl / volume
    if efficiency <= 0:
        return max(0, 20 + efficiency * 200)
    if efficiency >= 0.15:
        return 100.0
    elif efficiency >= 0.10:
        return 85.0 + (efficiency - 0.10) * 300
    elif efficiency >= 0.05:
        return 70.0 + (efficiency - 0.05) * 300
    elif efficiency >= 0.02:
        return 50.0 + (efficiency - 0.02) * 666
    elif efficiency >= 0.01:
        return 35.0 + (efficiency - 0.01) * 1500
    else:
        return efficiency * 3500


def _activity_score(
    trade_count: int, markets_traded: int, volume_24h: float,
    last_trade_time: Optional[datetime],
) -> float:
    """Activity & freshness score."""
    score = 0.0
    if trade_count >= 500:
        score += 30
    elif trade_count >= 200:
        score += 25
    elif trade_count >= 100:
        score += 20
    elif trade_count >= 50:
        score += 15
    elif trade_count >= 20:
        score += 10
    else:
        score += min(10, trade_count * 0.5)

    if markets_traded >= 50:
        score += 25
    elif markets_traded >= 20:
        score += 20
    elif markets_traded >= 10:
        score += 15
    elif markets_traded >= 5:
        score += 10
    else:
        score += markets_traded * 2

    if volume_24h and volume_24h > 0:
        if volume_24h >= 10000:
            score += 20
        elif volume_24h >= 1000:
            score += 15
        elif volume_24h >= 100:
            score += 10
        else:
            score += 5

    if last_trade_time:
        now = datetime.now(timezone.utc)
        age = now - last_trade_time
        if age < timedelta(days=1):
            score += 25
        elif age < timedelta(days=3):
            score += 20
        elif age < timedelta(days=7):
            score += 15
        elif age < timedelta(days=14):
            score += 10
        elif age < timedelta(days=30):
            score += 5
    elif volume_24h and volume_24h > 0:
        score += 15

    return min(100.0, score)


def compute_quality_score(winner: Winner) -> Dict[str, Any]:
    """Compute composite quality score for a trader."""
    wr_score = _win_rate_score(winner.win_rate or 0)
    cons_score = _consistency_score(
        winner.pnl_24h or 0, winner.pnl_7d or 0,
        winner.pnl_30d or 0, winner.total_pnl or 0,
    )
    ra_score = _risk_adjusted_score(winner.total_pnl or 0, winner.volume or 0)
    act_score = _activity_score(
        winner.trade_count or 0, winner.markets_traded or 0,
        winner.volume_24h or 0, winner.last_trade_time,
    )
    composite = (wr_score * 0.25 + cons_score * 0.25 + ra_score * 0.25 + act_score * 0.25)
    composite = round(min(100.0, max(0.0, composite)), 1)

    tier = "D"
    for t, threshold in sorted(TIER_THRESHOLDS.items(), key=lambda x: -x[1]):
        if composite >= threshold:
            tier = t
            break

    return {
        "quality_score": composite,
        "consistency_score": round(cons_score, 1),
        "risk_adjusted_score": round(ra_score, 1),
        "activity_score": round(act_score, 1),
        "win_rate_score": round(wr_score, 1),
        "quality_tier": tier,
    }


def score_all_traders(db: Session, min_trades: int = 5) -> int:
    """Recompute quality scores for all winners with enough trades."""
    winners = db.query(Winner).filter(Winner.trade_count >= min_trades).all()
    count = 0
    now = utc_now()
    for w in winners:
        scores = compute_quality_score(w)
        w.quality_score = scores["quality_score"]
        w.consistency_score = scores["consistency_score"]
        w.risk_adjusted_score = scores["risk_adjusted_score"]
        w.activity_score = scores["activity_score"]
        w.quality_tier = scores["quality_tier"]
        w.quality_updated_at = now
        count += 1
    db.commit()
    logger.info("Quality scoring complete: %d traders scored", count)
    return count


def get_trader_quality(db: Session, wallet_address: str) -> Optional[Dict[str, Any]]:
    """Get quality score for a specific trader. Computes on-the-fly if stale."""
    winner = db.query(Winner).filter(
        Winner.wallet_address == wallet_address.lower()
    ).first()
    if not winner:
        return None
    if (
        winner.quality_score is None
        or winner.quality_updated_at is None
        or (utc_now() - winner.quality_updated_at).total_seconds() > 3600
    ):
        scores = compute_quality_score(winner)
        winner.quality_score = scores["quality_score"]
        winner.consistency_score = scores["consistency_score"]
        winner.risk_adjusted_score = scores["risk_adjusted_score"]
        winner.activity_score = scores["activity_score"]
        winner.quality_tier = scores["quality_tier"]
        winner.quality_updated_at = utc_now()
        db.commit()
        return scores
    return {
        "quality_score": winner.quality_score,
        "consistency_score": winner.consistency_score,
        "risk_adjusted_score": winner.risk_adjusted_score,
        "activity_score": winner.activity_score,
        "quality_tier": winner.quality_tier,
    }
