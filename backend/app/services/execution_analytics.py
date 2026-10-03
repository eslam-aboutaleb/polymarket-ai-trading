"""Execution analytics & Edge Score (plan 03).

Measures execution quality per strategy over a rolling window
(default 30 days): slippage, fee drag, fill rate, win rate and
the Edge Score.

Data sources
------------
- ``UserTrade.expected_price`` / ``expected_size`` — the quoted
  price/size recorded at order submission.
- ``UserTrade.filled_price`` / ``filled_size`` / ``fee_paid`` /
  ``latency_ms`` — captured when a fill is observed (trade_monitor
  WS events or the fill reconciliation job).
- ``UserTrade.pnl`` — realized PnL written by
  ``app.services.pnl_reconciliation`` (FIFO lot matching). This
  module only reads it; GET endpoints never mutate.

Historical trades (recorded before the analytics columns existed)
carry NULL expected/filled data. They are excluded from the
per-strategy metrics and surfaced as ``legacy_trades`` in the
summary's data-quality block, so operators can see how much of the
history is unmeasurable. Metrics therefore accumulate from deployment
onward. Missing fee data is treated as 0 and counted in
``missing_fee`` so data quality stays visible.

Edge Score
----------
Per strategy::

    expectancy = win_rate × avg_win − loss_rate × avg_loss − avg_fee
    edge_raw   = expectancy / avg_risk_per_trade
    score      = 50 + 50 × tanh(edge_raw)      # → (0, 100)

``avg_loss`` is the mean magnitude of losing trades (positive),
``avg_fee`` the mean fee per filled trade and ``avg_risk_per_trade``
the mean absolute realized PnL across closed trades (the average
outcome magnitude, i.e. the typical "R" risked). ``tanh`` maps the
R-multiple expectancy smoothly onto (0, 100): 50 is break-even,
>50 positive edge, <50 negative edge.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, timedelta
from statistics import median
from typing import Any

from sqlalchemy.orm import Session

from app.models.user_trade import UserTrade
from app.utils.time import utc_now

logger = logging.getLogger(__name__)

# Order-submission strategies that can appear in strategy_source.
STRATEGY_SOURCES = (
    "copy",
    "manual",
    "market_maker",
    "inverse",
    "stop_loss",
    "take_profit",
    "latency_arb",
)

# Only these statuses represent an order that actually reached the
# exchange. "rejected" (gate-rejected before submission) and
# "simulated" (paper mode) never hit the CLOB.
_SUBMITTED_STATUSES = ("pending", "executed", "filled", "failed", "cancelled")
_FILLED_STATUSES = ("executed", "filled")

DEFAULT_WINDOW_DAYS = 30


def compute_slippage_bps(expected_price: float | None, filled_price: float | None) -> float | None:
    """Signed slippage in basis points: (filled − expected) / expected × 10⁴.

    Returns None when either price is missing or the expected price
    is unusable (zero/negative), so pre-migration trades never
    produce a fake zero.
    """
    if expected_price is None or filled_price is None:
        return None
    try:
        expected = float(expected_price)
        filled = float(filled_price)
    except (TypeError, ValueError):
        return None
    if expected <= 0:
        return None
    return round((filled - expected) / expected * 10_000.0, 2)


def compute_latency_ms(
    submitted_at: Any,
    filled_at: Any,
) -> float | None:
    """Submission→fill latency in milliseconds.

    Returns None when either timestamp is missing.  A naive
    timestamp (SQLite round-trips drop tzinfo) is treated as
    UTC so mixed naive/aware values still compute.
    """
    if submitted_at is None or filled_at is None:
        return None
    try:
        submitted = _as_utc(submitted_at)
        filled = _as_utc(filled_at)
        return round((filled - submitted).total_seconds() * 1000.0, 1)
    except (AttributeError, TypeError, ValueError):
        return None


def _as_utc(moment: Any) -> Any:
    if getattr(moment, "tzinfo", None) is None:
        return moment.replace(tzinfo=UTC)
    return moment


@dataclass
class StrategyStats:
    """Rolling-window execution statistics for one strategy."""

    strategy: str
    submissions: int = 0
    filled: int = 0
    fill_rate: float | None = None
    avg_slippage_bps: float | None = None
    median_slippage_bps: float | None = None
    total_fees: float = 0.0
    gross_realized_pnl: float = 0.0
    net_realized_pnl: float = 0.0
    fee_drag_ratio: float | None = None
    win_rate: float | None = None
    avg_win: float | None = None
    avg_loss: float | None = None  # positive magnitude
    avg_fee: float | None = None
    avg_risk_per_trade: float | None = None
    edge_raw: float | None = None
    edge_score: float | None = None
    missing_fee: int = 0
    missing_expected_price: int = 0
    closed_trades: int = 0
    wins: int = 0
    losses: int = 0
    slippage_samples: list[float] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "submissions": self.submissions,
            "filled": self.filled,
            "fill_rate": _round_opt(self.fill_rate, 4),
            "avg_slippage_bps": _round_opt(self.avg_slippage_bps, 2),
            "median_slippage_bps": _round_opt(self.median_slippage_bps, 2),
            "total_fees": round(self.total_fees, 6),
            "gross_realized_pnl": round(self.gross_realized_pnl, 6),
            "net_realized_pnl": round(self.net_realized_pnl, 6),
            "fee_drag_ratio": _round_opt(self.fee_drag_ratio, 4),
            "win_rate": _round_opt(self.win_rate, 4),
            "avg_win": _round_opt(self.avg_win, 6),
            "avg_loss": _round_opt(self.avg_loss, 6),
            "avg_fee": _round_opt(self.avg_fee, 6),
            "avg_risk_per_trade": _round_opt(self.avg_risk_per_trade, 6),
            "edge_raw": _round_opt(self.edge_raw, 4),
            "edge_score": _round_opt(self.edge_score, 2),
            "missing_fee": self.missing_fee,
            "missing_expected_price": self.missing_expected_price,
            "closed_trades": self.closed_trades,
            "wins": self.wins,
            "losses": self.losses,
        }


def _round_opt(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def _normalize_edge_score(edge_raw: float) -> float:
    """Map an R-multiple expectancy onto (0, 100); 50 = break-even."""
    return 50.0 + 50.0 * math.tanh(edge_raw)


def _finalize(stats: _Accumulator) -> _Accumulator:
    """Derive the computed metrics from the raw accumulators."""
    if stats.submissions > 0:
        stats.fill_rate = stats.filled / stats.submissions
    if stats.slippage_samples:
        stats.avg_slippage_bps = sum(stats.slippage_samples) / len(stats.slippage_samples)
        stats.median_slippage_bps = median(stats.slippage_samples)
    if stats.filled > 0:
        stats.avg_fee = stats.total_fees / stats.filled
    else:
        # No fills with fee data: missing fees are treated
        # as 0, never as "unknown", so the Edge Score still
        # computes for strategies whose fills carried no fee.
        stats.avg_fee = 0.0
    if stats.gross_realized_pnl > 0:
        stats.fee_drag_ratio = stats.total_fees / stats.gross_realized_pnl
    stats.net_realized_pnl = stats.gross_realized_pnl - stats.total_fees

    closed = stats.wins + stats.losses
    stats.closed_trades = closed
    if closed > 0:
        stats.win_rate = stats.wins / closed
        loss_rate = 1.0 - stats.win_rate
        # A strategy with only wins (or only losses) has a
        # 0.0 mean on the missing side — not "unknown".
        stats.avg_win = _mean(stats._win_samples) if stats.wins else 0.0
        stats.avg_loss = _mean(stats._loss_samples) if stats.losses else 0.0
        # avg_risk_per_trade: mean absolute realized PnL (the typical R).
        risk_samples = stats._win_samples + stats._loss_samples
        stats.avg_risk_per_trade = _mean([abs(v) for v in risk_samples])
        if stats.avg_risk_per_trade and stats.avg_risk_per_trade > 0:
            expectancy = stats.win_rate * stats.avg_win - loss_rate * stats.avg_loss - stats.avg_fee
            stats.edge_raw = expectancy / stats.avg_risk_per_trade
            stats.edge_score = _normalize_edge_score(stats.edge_raw)
    return stats


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


@dataclass
class _Accumulator(StrategyStats):
    """StrategyStats plus the raw win/loss samples needed for means."""

    _win_samples: list[float] = field(default_factory=list)
    _loss_samples: list[float] = field(default_factory=list)


def _new_stats(strategy: str) -> _Accumulator:
    return _Accumulator(strategy=strategy)


def _absorb(stats: _Accumulator, row: UserTrade) -> None:
    """Fold one UserTrade row into a strategy accumulator."""
    stats.submissions += 1
    status = (row.status or "").lower()

    if status in _FILLED_STATUSES:
        stats.filled += 1
        fee = row.fee_paid
        if fee is None:
            stats.missing_fee += 1
        else:
            stats.total_fees += float(fee)
        slippage = compute_slippage_bps(row.expected_price, row.filled_price)
        if slippage is not None:
            stats.slippage_samples.append(slippage)
        elif row.expected_price is None:
            stats.missing_expected_price += 1

    pnl = row.pnl
    if pnl is not None:
        value = float(pnl)
        stats.gross_realized_pnl += value
        if value > 0:
            stats.wins += 1
            stats._win_samples.append(value)
        elif value < 0:
            stats.losses += 1
            stats._loss_samples.append(abs(value))


def get_strategy_stats(
    db: Session,
    user_id: int,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> dict[str, StrategyStats]:
    """Per-strategy rolling execution stats for a user.

    The cohort is every order the user submitted in the window
    (``created_at`` within ``window_days``), grouped by
    ``strategy_source``. Rows without a strategy source (pre-migration
    trades) are excluded — they cannot be attributed to a strategy.
    """
    cutoff = utc_now() - timedelta(days=max(1, int(window_days)))
    rows = (
        db.query(UserTrade)
        .filter(
            UserTrade.user_id == user_id,
            UserTrade.created_at >= cutoff,
            UserTrade.strategy_source.isnot(None),
        )
        .all()
    )

    by_strategy: dict[str, _Accumulator] = {}
    for row in rows:
        strategy = row.strategy_source or "unknown"
        stats = by_strategy.setdefault(strategy, _new_stats(strategy))
        _absorb(stats, row)

    return {strategy: _finalize(stats) for strategy, stats in sorted(by_strategy.items())}


def get_execution_summary(
    db: Session,
    user_id: int,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> dict[str, Any]:
    """Aggregate execution summary across all strategies.

    Includes per-strategy breakdowns, overall totals and a
    data-quality block (legacy trades without analytics columns,
    rows missing expected price, filled rows missing fee data).
    """
    cutoff = utc_now() - timedelta(days=max(1, int(window_days)))
    per_strategy = get_strategy_stats(db, user_id, window_days)

    # Data-quality counts over the whole cohort (including rows
    # without a strategy source, which are legacy by definition).
    cohort = (
        db.query(UserTrade)
        .filter(UserTrade.user_id == user_id, UserTrade.created_at >= cutoff)
        .all()
    )
    legacy = sum(1 for r in cohort if r.strategy_source is None)
    missing_expected = sum(
        1
        for r in cohort
        if r.strategy_source is not None
        and r.expected_price is None
        and (r.status or "").lower() in _FILLED_STATUSES
    )

    totals = _Accumulator(strategy="all")
    for stats in per_strategy.values():
        totals.submissions += stats.submissions
        totals.filled += stats.filled
        totals.total_fees += stats.total_fees
        totals.gross_realized_pnl += stats.gross_realized_pnl
        totals.wins += stats.wins
        totals.losses += stats.losses
        totals.slippage_samples.extend(stats.slippage_samples)
        totals.missing_fee += stats.missing_fee
        totals.missing_expected_price += stats.missing_expected_price
        totals._win_samples.extend(stats._win_samples)
        totals._loss_samples.extend(stats._loss_samples)
    _finalize(totals)

    return {
        "user_id": user_id,
        "window_days": max(1, int(window_days)),
        "generated_at": utc_now().isoformat(),
        "per_strategy": {s: st.to_dict() for s, st in per_strategy.items()},
        "totals": totals.to_dict(),
        "data_quality": {
            "legacy_trades": legacy,
            "missing_expected_price": missing_expected,
            "missing_fee": totals.missing_fee,
        },
    }


def get_edge_scores(
    db: Session,
    user_id: int,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> list[dict[str, Any]]:
    """Edge Score per strategy, sorted best-first.

    Strategies without enough closed-trade data to compute a score
    are still listed with ``edge_score: null`` so the UI can show
    "insufficient data" rather than hiding the strategy.
    """
    per_strategy = get_strategy_stats(db, user_id, window_days)
    rows = []
    for strategy, stats in per_strategy.items():
        rows.append(
            {
                "strategy": strategy,
                "edge_score": _round_opt(stats.edge_score, 2),
                "edge_raw": _round_opt(stats.edge_raw, 4),
                "win_rate": _round_opt(stats.win_rate, 4),
                "avg_win": _round_opt(stats.avg_win, 6),
                "avg_loss": _round_opt(stats.avg_loss, 6),
                "avg_fee": _round_opt(stats.avg_fee, 6),
                "avg_risk_per_trade": _round_opt(stats.avg_risk_per_trade, 6),
                "filled": stats.filled,
                "closed_trades": stats.closed_trades,
            }
        )
    rows.sort(key=lambda r: (r["edge_score"] is None, -(r["edge_score"] or 0.0)))
    return rows


def get_trade_details(
    db: Session,
    user_id: int,
    strategy: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Per-trade execution detail, newest first.

    ``strategy`` filters by ``strategy_source``; None returns every
    strategy. Pre-migration trades appear with null expected/filled
    fields.
    """
    q = db.query(UserTrade).filter(UserTrade.user_id == user_id)
    if strategy:
        q = q.filter(UserTrade.strategy_source == strategy)
    rows = (
        q.order_by(UserTrade.created_at.desc(), UserTrade.id.desc())
        .limit(max(1, min(int(limit), 200)))
        .all()
    )
    return [_trade_to_dict(row) for row in rows]


def _trade_to_dict(row: UserTrade) -> dict[str, Any]:
    return {
        "id": row.id,
        "market_id": row.market_id,
        "token_id": row.token_id,
        "action": row.action,
        "strategy_source": row.strategy_source,
        "status": row.status,
        "expected_price": row.expected_price,
        "expected_size": row.expected_size,
        "filled_price": row.filled_price,
        "filled_size": row.filled_size,
        "fee_paid": row.fee_paid,
        "slippage_bps": row.slippage_bps,
        "latency_ms": row.latency_ms,
        "pnl": row.pnl,
        "order_hash": row.order_hash,
        "executed_at": row.executed_at.isoformat() if row.executed_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
