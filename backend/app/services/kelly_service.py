"""
Kelly-criterion position sizing (plan 04).

Sizes orders from an estimated win probability instead of a
fixed amount or a mirror of the followed trader's notional.
For a binary contract bought at ``price`` that pays 1.0 on a
win, the full Kelly fraction is::

    b = (1 - price) / price        net odds received on the wager
    q = 1 - p                      probability of losing
    f* = max(0, (b*p - q) / b)     fraction of bankroll to wager

The application applies a fractional multiplier (quarter Kelly
by default, ``UserSettings.kelly_fraction``) because the
inputs are estimates, not certainties.

**Kelly input quality drives sizing quality.**  The
probability ``p`` resolved by :func:`resolve_edge_probability`
comes from an LLM probability/confidence estimate or a historical
win rate; neither is a calibrated probability.  An over-confident
edge estimate makes Kelly over-bet, so the default multiplier is
0.25 (quarter Kelly): a mis-estimated edge costs at most a
quarter of the full-Kelly wager while retaining most of the
long-run bankroll growth.  Every Kelly-sized order logs the
edge source used so sizing decisions can be audited against
outcomes.

Bankroll is defined as USDC balance plus unrealized PnL,
matching the portfolio equity calculation
(``PolymarketService.get_portfolio_summary``:
``total_current_value - total_invested``).
"""

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.assessment import Assessment
from app.models.trade_history import TradeHistory
from app.models.user_trade import UserTrade
from app.models.winner import Winner
from app.services.pre_trade_gate import _to_float

logger = logging.getLogger(__name__)

# Fractional-Kelly multiplier applied when the user has not
# configured one.  Quarter Kelly bounds the damage of an
# over-confident edge estimate (see the module docstring).
DEFAULT_KELLY_MULTIPLIER = 0.25

# CLOB minimum order size in USDC notional (marketable buys
# below $1 are rejected by the exchange).
MIN_ORDER_SIZE_USDC = 1.0

# Recommendations that carry no positive edge for a long
# position: an "avoid" assessment's confidence must never be
# read as P(Yes).
NO_EDGE_RECOMMENDATIONS = ("avoid", "skip")


def kelly_fraction(p: float, price: float) -> float:
    """Full Kelly fraction for a binary bought at ``price`` paying 1.0 on win.

    ``b = (1 - price) / price`` is the net odds, ``q = 1 - p``
    the lose probability, and ``f* = max(0, (b*p - q) / b)``
    the fraction of bankroll to wager.  Returns 0.0 for
    degenerate inputs (no valid odds, no valid probability) —
    a market priced at or beyond the estimated probability
    carries no edge.
    """
    try:
        p = float(p)
        price = float(price)
    except (TypeError, ValueError):
        return 0.0

    if not (0.0 < price < 1.0) or not (0.0 < p < 1.0):
        return 0.0

    b = (1.0 - price) / price
    if b <= 0:
        return 0.0
    q = 1.0 - p
    f_star = (b * p - q) / b
    return max(0.0, f_star)


def size(
    bankroll: float,
    p: float,
    price: float,
    multiplier: float = DEFAULT_KELLY_MULTIPLIER,
    min_order: float = MIN_ORDER_SIZE_USDC,
    max_position_size: float | None = None,
) -> float:
    """Kelly-sized USDC wager: ``clamp(f* * multiplier * bankroll, min_order, max_position_size)``.

    A non-positive edge (``f* <= 0``) sizes to 0 and logs
    INFO "no edge".  A non-positive bankroll sizes to 0: there
    is nothing to wager.  ``multiplier`` must be in ``(0, 1]``
    — a fractional Kelly of 0 or a leverage factor above 1 is
    a configuration error and raises ``ValueError``.
    """
    if not (0.0 < multiplier <= 1.0):
        raise ValueError(f"Kelly multiplier must be in (0, 1], got {multiplier}")

    f_star = kelly_fraction(p, price)
    if f_star <= 0:
        logger.info(
            "Kelly sizing: no edge (p=%.4f price=%.4f f*=0)",
            _to_float(p),
            _to_float(price),
        )
        return 0.0

    bankroll_usdc = _to_float(bankroll)
    if bankroll_usdc <= 0:
        logger.info("Kelly sizing: bankroll is %.2f USDC; no size", bankroll_usdc)
        return 0.0

    raw = f_star * multiplier * bankroll_usdc

    upper = (
        _to_float(max_position_size)
        if max_position_size is not None and _to_float(max_position_size) > 0
        else float("inf")
    )
    lower = _to_float(min_order, MIN_ORDER_SIZE_USDC)

    clamped = min(raw, upper)
    if clamped < lower:
        clamped = lower
    return round(clamped, 2)


def effective_trade_params(p_market: float, side: str, price: float) -> tuple[float, float]:
    """Map a market-level Yes-probability to the (p, price) Kelly inputs for a side.

    A BUY of the Yes outcome at ``price`` wagers with win
    probability ``p_market``.  A SELL at ``price`` is the
    mirror image: it wins when the market resolves No, so the
    win probability is ``1 - p_market`` and the equivalent
    purchase price of that position is ``1 - price`` (selling
    Yes at 0.7 == buying No at 0.3).  This keeps one Kelly
    formula for both sides.
    """
    p = _to_float(p_market)
    px = _to_float(price)
    if str(side).upper() == "SELL":
        return (max(0.0, min(1.0, 1.0 - p)), max(0.0, min(1.0, 1.0 - px)))
    return (max(0.0, min(1.0, p)), max(0.0, min(1.0, px)))


def kelly_trade_params(
    p_market: float,
    edge_source: str | None,
    side: str,
    price: float,
) -> tuple[float, float]:
    """Map a resolved edge probability to Kelly ``(p, price)`` for a side.

    ``effective_trade_params`` mirrors a *market Yes-probability* for a SELL
    (``p -> 1 - p``). But an ``edge_source == "ai_assessment"`` probability is
    already direction-aware — ``P(this trade is profitable)`` (see
    :func:`resolve_edge_probability`) — so mirroring ``p`` again inverts the
    edge and under-sizes or skips the sell. Only the price mirrors for a SELL
    (economically a NO-side wager regardless of where ``p`` came from), so the
    price always mirrors but the direction-aware ``p`` never does.
    """
    p = max(0.0, min(1.0, _to_float(p_market)))
    px = max(0.0, min(1.0, _to_float(price)))
    if str(side).upper() == "SELL":
        kelly_p = p if edge_source == "ai_assessment" else 1.0 - p
        return (kelly_p, 1.0 - px)
    return (p, px)


def resolve_edge_probability(
    db: Session,
    user_id: int,
    market_id: str,
    trader_wallet: str | None = None,
) -> tuple[float | None, str | None]:
    """Resolve the estimated win probability for a market.

    **Two probability semantics reach this function and they are
    not interchangeable.**

    * ``Assessment.confidence`` (0-100) is the model's assessment
      strength for the market — a rough ``P(YES resolves true)``.
      Callers map it onto a side themselves
      (``effective_trade_params`` mirrors it for a SELL / NO
      position), so it must stay side-agnostic here.
    * ``Assessment.ai_probability`` is the copy-trade evaluation's
      "your calibrated probability THIS TRADE is profitable". It is
      already direction-aware — the probability of *profit*, not of
      the YES outcome — so it is used verbatim and must never be
      negated, mirrored or re-based for a NO-side position: reading
      a 70% chance of profit as ``1 - 0.70`` would size a NO-side
      copy of the same verdict at a 30% edge.

    Preference order:

      1. ``ai_assessment`` — ``ai_probability`` of the latest
         ``Assessment`` for the market when it is present and in
         ``(0, 1)`` (a stated certainty of 0 or 1 is not a
         forecast, and a value above 1 is a percentage that was
         never scaled), otherwise the confidence (0-100) of that
         assessment. Both are used only when the recommendation is
         not "avoid"/"skip" (an avoid carries no positive edge for
         a long position).
      2. ``trader_win_rate`` — the followed trader's
         historical win rate (``Winner.win_rate``) when
         ``trader_wallet`` is given; otherwise the user's own
         realized win rate from their executed trades.
      3. ``None`` — no estimate available; callers must not
         size.

    Returns ``(probability, edge_source)``.  Callers log the
    source on every Kelly-sized order for audit.
    """
    # 1 – AI confidence from the latest assessment for the market.
    try:
        latest = (
            db.query(Assessment)
            .join(TradeHistory, TradeHistory.id == Assessment.trade_history_id)
            .filter(TradeHistory.market_id == market_id)
            .order_by(Assessment.created_at.desc())
            .first()
        )
        if latest is not None:
            recommendation = str(latest.recommendation or "").lower()
            if recommendation not in NO_EDGE_RECOMMENDATIONS:
                # Preferred estimate: the direction-aware P(profit).
                ai_probability = (
                    _to_float(latest.ai_probability) if latest.ai_probability is not None else 0.0
                )
                if 0.0 < ai_probability < 1.0:
                    p = round(ai_probability, 4)
                    logger.info(
                        "Kelly edge source=ai_assessment (ai_probability) user=%d market=%s p=%.4f",
                        user_id,
                        market_id,
                        p,
                    )
                    return (p, "ai_assessment")
                # Legacy fallback: confidence read as P(YES).
                confidence = _to_float(
                    latest.confidence if latest.confidence is not None else latest.ai_score,
                )
                if 0 < confidence <= 100:
                    p = round(confidence / 100.0, 4)
                    logger.info(
                        "Kelly edge source=ai_assessment user=%d market=%s p=%.4f",
                        user_id,
                        market_id,
                        p,
                    )
                    return (p, "ai_assessment")
    except Exception as e:
        logger.debug("Kelly AI-assessment edge lookup failed: %s", e)

    # 2 – Historical win rate: the followed trader's, or the user's own.
    if trader_wallet:
        try:
            winner = db.query(Winner).filter(Winner.wallet_address == trader_wallet.lower()).first()
            if winner is not None:
                win_rate = _to_float(winner.win_rate)
                if 0 < win_rate <= 100:
                    p = round(win_rate / 100.0, 4)
                    logger.info(
                        "Kelly edge source=trader_win_rate user=%d market=%s trader=%s p=%.4f",
                        user_id,
                        market_id,
                        trader_wallet[:12],
                        p,
                    )
                    return (p, "trader_win_rate")
        except Exception as e:
            logger.debug("Kelly trader win-rate lookup failed: %s", e)

    try:
        rows = (
            db.query(UserTrade.pnl)
            .filter(
                UserTrade.user_id == user_id,
                UserTrade.pnl.isnot(None),
                UserTrade.status == "executed",
            )
            .all()
        )
        pnls = [float(row[0]) for row in rows if row[0] is not None]
        if pnls:
            wins = sum(1 for pnl in pnls if pnl > 0)
            p = round(wins / len(pnls), 4)
            if 0 < p < 1:
                logger.info(
                    "Kelly edge source=user_win_rate user=%d market=%s wins=%d/%d p=%.4f",
                    user_id,
                    market_id,
                    wins,
                    len(pnls),
                    p,
                )
                return (p, "user_win_rate")
    except Exception as e:
        logger.debug("Kelly user win-rate lookup failed: %s", e)

    logger.info("Kelly edge source=none user=%d market=%s", user_id, market_id)
    return (None, None)


async def compute_bankroll(
    service: Any,
    wallet_address: str,
    private_key: str | None = None,
    clob_creds: dict | None = None,
) -> float:
    """Bankroll = USDC balance + unrealized PnL (portfolio equity calc).

    ``service`` is a ``PolymarketService`` instance.  Both
    fetches are best-effort: an unreachable endpoint sizes
    from whatever is known (0.0), never raises, and never
    blocks order placement.
    """
    usdc_balance = 0.0
    try:
        balance = await service.get_wallet_balance(
            wallet_address,
            private_key=private_key,
            clob_creds=clob_creds,
        )
        usdc_balance = _to_float(balance.get("usdc_balance"))
    except Exception as e:
        logger.warning("Kelly bankroll: balance fetch failed for %s: %s", wallet_address, e)

    unrealized_pnl = 0.0
    try:
        positions = await service.get_positions(
            wallet_address,
            private_key=private_key,
            clob_creds=clob_creds,
        )
        for pos in positions or []:
            try:
                pos_size = _to_float(pos.get("size"))
                avg_price = _to_float(pos.get("avgPrice", pos.get("avg_price")))
                cur_price = _to_float(pos.get("curPrice", pos.get("price")))
                unrealized_pnl += pos_size * (cur_price - avg_price)
            except (TypeError, ValueError):
                continue
    except Exception as e:
        logger.warning("Kelly bankroll: positions fetch failed for %s: %s", wallet_address, e)

    bankroll = usdc_balance + unrealized_pnl
    logger.info(
        "Kelly bankroll for %s: usdc=%.2f unrealized_pnl=%.2f total=%.2f",
        wallet_address,
        usdc_balance,
        unrealized_pnl,
        bankroll,
    )
    return round(bankroll, 2)
