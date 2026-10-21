"""
Backtesting Engine — simulate strategies against historical trade data.

Supports:
  - Copy-trade strategy replay
  - Inverse bot strategy replay
  - Custom indicator-based strategies
  - Full metrics: win rate, PnL, Sharpe ratio, max drawdown, profit factor

Inspired by 0xrsydn/polymarket-crypto-toolkit back-testing approach.
"""

import logging
import math
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.analytics.backtest_run import BacktestRun
from app.repositories.analytics.backtesting import BacktestRepository
from app.repositories.trading.backtesting import TradingBacktestRepository
from app.utils.database import SessionLocal
from app.utils.indicators import compute_all_indicators, generate_indicator_summary
from app.utils.time import utc_now

logger = logging.getLogger(__name__)
BACKTEST_DATE_FORMAT = "%Y-%m-%d"


def _parse_backtest_date(date_value: str, field_name: str) -> datetime:
    try:
        return datetime.strptime(date_value, BACKTEST_DATE_FORMAT)
    except ValueError as exc:
        raise ValueError(f"Invalid {field_name}. Use YYYY-MM-DD") from exc


def _validate_backtest_date_range(start_date: str, end_date: str) -> None:
    start_dt = _parse_backtest_date(start_date, "start_date")
    end_dt = _parse_backtest_date(end_date, "end_date")
    if start_dt > end_dt:
        raise ValueError("start_date must be on or before end_date")


# ── Metric calculators ──


def _calculate_metrics(trade_log: list[dict[str, Any]]) -> dict[str, Any]:
    """Calculate performance metrics from a trade log."""
    if not trade_log:
        return {
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "max_drawdown": 0.0,
            "sharpe_ratio": None,
            "profit_factor": None,
            "avg_trade_pnl": 0.0,
            "max_consecutive_losses": 0,
            "total_volume": 0.0,
        }

    pnls = [t.get("pnl", 0.0) for t in trade_log]
    winning = [p for p in pnls if p > 0]
    losing = [p for p in pnls if p < 0]
    volumes = [abs(t.get("notional_usdc", t.get("size", 0) * t.get("price", 0))) for t in trade_log]

    total_pnl = sum(pnls)
    win_count = len(winning)
    loss_count = len(losing)
    total = len(pnls)

    # Win rate
    win_rate = (win_count / total * 100) if total > 0 else 0.0

    # Max drawdown
    peak = 0.0
    cum_pnl = 0.0
    max_dd = 0.0
    for pnl in pnls:
        cum_pnl += pnl
        if cum_pnl > peak:
            peak = cum_pnl
        dd = peak - cum_pnl
        if dd > max_dd:
            max_dd = dd

    # Irregular, USD-denominated trade PnLs are NOT a time series of
    # periodic returns. An annualized Sharpe requires a consistently sampled
    # cash+position NAV series. Do not manufacture the statistic from trades.
    sharpe = None

    # Profit factor
    gross_profit = sum(winning) if winning else 0
    gross_loss = abs(sum(losing)) if losing else 0
    profit_factor = round(gross_profit / gross_loss, 4) if gross_loss > 0 else None

    # Max consecutive losses
    max_consec = 0
    current_consec = 0
    for pnl in pnls:
        if pnl < 0:
            current_consec += 1
            max_consec = max(max_consec, current_consec)
        else:
            current_consec = 0

    return {
        "total_trades": total,
        "winning_trades": win_count,
        "losing_trades": loss_count,
        "win_rate": round(win_rate, 2),
        "total_pnl": round(total_pnl, 4),
        "max_drawdown": round(max_dd, 4),
        "sharpe_ratio": sharpe,
        "profit_factor": profit_factor,
        "avg_trade_pnl": round(total_pnl / total, 4) if total > 0 else 0.0,
        "max_consecutive_losses": max_consec,
        "total_volume": round(sum(volumes), 4),
    }


async def _fetch_historical_prices(
    condition_id: str,
    start_date: str,
    end_date: str,
    outcome: str = "Yes",
) -> list[dict[str, Any]]:
    """Resolve the outcome token and fetch validated CLOB history (not Data API)."""
    from app.services.market_data.historical_prices import load_token_price_history

    return await load_token_price_history(condition_id, start_date, end_date, outcome=outcome)


def _failure_provenance(
    condition_id: str,
    start_date: str,
    end_date: str,
    outcome: str,
    *,
    reason: str,
    message: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Provenance for a run that never produced a validated dataset.

    A failed run must stay attributable: the requested window, the token
    mapping, the feed identity and the reason it failed are recorded even
    when the upstream feed was never usable.
    """
    from app.services.market_data.historical_prices import (
        FEED_VERSION,
        SOURCE_SCHEMA_VERSION,
        price_window,
    )

    context = context or {}
    provenance = dict(context.get("provenance") or {})
    try:
        start_dt, end_dt = price_window(start_date, end_date)
        window = {"start_utc": start_dt.isoformat(), "end_exclusive_utc": end_dt.isoformat()}
    except ValueError:
        window = {"start_utc": start_date, "end_exclusive_utc": end_date}
    provenance.update(
        {
            "source": SOURCE_SCHEMA_VERSION,
            "source_schema_version": SOURCE_SCHEMA_VERSION,
            "feed_version": FEED_VERSION,
            "source_url": provenance.get("source_url", ""),
            "condition_id": str(condition_id),
            "outcome": outcome,
            "token_id": context.get("token_id") or provenance.get("token_id", ""),
            "fetched_at": provenance.get("fetched_at") or utc_now().isoformat(),
            "price_only": True,
            "error_reason": message,
            "error_kind": reason,
        }
    )
    provenance.update(window)
    return provenance


def _failed_history_run(
    condition_id: str,
    start_date: str,
    end_date: str,
    outcome: str,
    *,
    reason: str,
    message: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A failed run that carries the provenance of the failure it reports."""
    from app.services.market_data.historical_prices import FAILURE_QUALITY_BY_REASON

    quality = FAILURE_QUALITY_BY_REASON.get(reason, "history_error")
    provenance = _failure_provenance(
        condition_id,
        start_date,
        end_date,
        outcome,
        reason=reason,
        message=message,
        context=context,
    )
    provenance["quality"] = quality
    return {
        "trade_log": [],
        "metrics": _calculate_metrics([]),
        "error": message,
        "indicator_values": {
            "history_source": provenance,
            "history_quality": quality,
            "error_reason": message,
        },
    }


async def run_copy_trade_backtest(
    user_id: int,  # noqa: ARG001 - common strategy runner signature
    params: dict[str, Any],
) -> dict[str, Any]:
    """Backtest a copy-trade strategy.

    Replays historical trades from a followed trader and simulates
    copying them with the given sizing parameters.

    Parameters:
        - followed_wallet: the trader wallet to simulate copying
        - start_date / end_date: date range
        - max_position_size: max USDC per position
        - daily_loss_limit: max daily loss before stopping
    """
    db = SessionLocal()
    try:
        followed_wallet = params.get("followed_wallet", "")
        max_pos_size = float(params.get("max_position_size", 100))
        daily_loss_limit = float(params.get("daily_loss_limit", 50))

        # Fetch historical trades from this wallet
        trades = BacktestRepository(db).historical_trades(
            followed_wallet,
            params.get("start_date", "2024-01-01"),
            params.get("end_date", "2025-12-31"),
        )

        trade_log: list[dict[str, Any]] = []
        daily_loss = 0.0
        current_day = None

        for trade in trades:
            trade_day = trade.executed_at.date() if trade.executed_at else None
            if trade_day != current_day:
                daily_loss = 0.0
                current_day = trade_day

            # Skip if daily loss limit breached
            if daily_loss >= daily_loss_limit:
                continue

            # Simulate the copy trade
            size = min(float(trade.amount or 0), max_pos_size)
            price = float(trade.price or 0)
            if size <= 0 or price <= 0:
                continue

            # Simulate a simple PnL based on the actual trade outcome
            pnl = float(trade.pnl or 0)
            if pnl < 0:
                daily_loss += abs(pnl)

            trade_log.append(
                {
                    "timestamp": trade.executed_at.isoformat() if trade.executed_at else "",
                    "market_id": trade.market_id or "",
                    "side": trade.action or "",
                    "size": size,
                    "price": price,
                    "pnl": pnl,
                }
            )

        return {
            "trade_log": trade_log,
            "metrics": _calculate_metrics(trade_log),
        }
    finally:
        db.close()


async def run_indicator_backtest(
    user_id: int,  # noqa: ARG001 - common strategy runner signature
    params: dict[str, Any],
) -> dict[str, Any]:
    """Backtest an indicator-based strategy.

    Uses technical indicators (RSI, MACD, Bollinger Bands) to generate
    buy/sell signals on historical price data.

    Parameters:
        - condition_id: market to backtest
        - start_date / end_date: date range
        - strategy: "rsi_mean_reversion" | "macd_crossover" | "bollinger_bounce"
        - position_size: USDC per trade
        - rsi_oversold / rsi_overbought: thresholds for RSI strategy
    """
    condition_id = params.get("condition_id", "")
    strategy = params.get("strategy", "rsi_mean_reversion")
    position_size = float(params.get("position_size", 10))
    rsi_oversold = float(params.get("rsi_oversold", 30))
    rsi_overbought = float(params.get("rsi_overbought", 70))
    if not math.isfinite(position_size) or position_size <= 0:
        return {
            "trade_log": [],
            "metrics": _calculate_metrics([]),
            "error": "position_size must be positive and finite",
        }
    start_date = params.get("start_date", "2024-01-01")
    end_date = params.get("end_date", "2025-12-31")
    outcome = params.get("outcome", "Yes")

    # A missing/malformed feed must not produce nominally successful performance.
    # Only the dedicated CLOB adapter accepts and validates raw t/p prices.
    from app.services.market_data.historical_prices import PriceHistoryError

    try:
        price_data = await _fetch_historical_prices(
            condition_id,
            start_date,
            end_date,
            outcome=outcome,
        )
    except PriceHistoryError as exc:
        # An upstream/transport failure is not "insufficient data": keep the
        # provenance of the attempt and name the actual cause.
        return _failed_history_run(
            condition_id,
            start_date,
            end_date,
            outcome,
            reason=exc.reason,
            message=str(exc),
            context=exc.context,
        )

    if not price_data:
        return _failed_history_run(
            condition_id,
            start_date,
            end_date,
            outcome,
            reason="empty_history",
            message="No historical price data available",
        )

    prices = []
    timestamps = []
    for point in price_data:
        if not isinstance(point, dict) or "price" not in point:
            return _failed_history_run(
                condition_id,
                start_date,
                end_date,
                outcome,
                reason="invalid_source",
                message="Malformed historical price row (price missing)",
            )
        try:
            value = float(point["price"])
        except (ValueError, TypeError, OverflowError):
            value = float("nan")
        # A settled-at-zero outcome is a legitimate price; only a negative or
        # non-finite value is a malformed row.
        if not math.isfinite(value) or value < 0:
            return _failed_history_run(
                condition_id,
                start_date,
                end_date,
                outcome,
                reason="invalid_source",
                message="Malformed historical price row (invalid price)",
            )
        prices.append(value)
        timestamps.append(point.get("timestamp", point.get("t", "")))

    provenance = getattr(price_data, "provenance", None)

    if len(prices) < 30:
        return _failed_history_run(
            condition_id,
            start_date,
            end_date,
            outcome,
            reason="insufficient_coverage",
            message="Insufficient price data (need at least 30 data points)",
            context={"provenance": provenance} if provenance else None,
        )

    if strategy not in {"rsi_mean_reversion", "macd_crossover", "bollinger_bounce"}:
        return {
            "trade_log": [],
            "metrics": _calculate_metrics([]),
            "error": "Unsupported indicator strategy",
            "indicator_values": {"history_quality": "invalid_strategy"},
        }

    # Compute indicators
    indicators = compute_all_indicators(prices)

    trade_log: list[dict[str, Any]] = []
    position = None  # {"side": "BUY", "entry_price": float, "entry_idx": int}

    from app.utils.indicators import bollinger_bands as calc_bb
    from app.utils.indicators import macd as calc_macd
    from app.utils.indicators import rsi as calc_rsi

    rsi_series = calc_rsi(prices)
    macd_data = calc_macd(prices)
    bb_data = calc_bb(prices)

    for i in range(30, len(prices) - 1):
        signal = None

        if strategy == "rsi_mean_reversion":
            rsi_val = rsi_series[i]
            if rsi_val is not None:
                if rsi_val < rsi_oversold and position is None:
                    signal = "BUY"
                elif rsi_val > rsi_overbought and position is not None:
                    signal = "SELL"

        elif strategy == "macd_crossover":
            macd_line = macd_data["macd"]
            signal_line = macd_data["signal"]
            if i > 0 and macd_line[i] is not None and signal_line[i] is not None:
                prev_macd = macd_line[i - 1]
                prev_signal = signal_line[i - 1]
                if prev_macd is not None and prev_signal is not None:
                    if (
                        prev_macd <= prev_signal
                        and macd_line[i] > signal_line[i]
                        and position is None
                    ):
                        signal = "BUY"
                    elif (
                        prev_macd >= prev_signal
                        and macd_line[i] < signal_line[i]
                        and position is not None
                    ):
                        signal = "SELL"

        elif strategy == "bollinger_bounce":
            lower = bb_data["lower"][i]
            upper = bb_data["upper"][i]
            if lower is not None and upper is not None:
                if prices[i] <= lower and position is None:
                    signal = "BUY"
                elif prices[i] >= upper and position is not None:
                    signal = "SELL"

        # The indicator uses observation i. Earliest possible modeled fill
        # is the NEXT observed price i+1, not the signal's own candle.
        # This is still a price-only scenario, not an executable CLOB fill.
        next_idx = i + 1
        next_price = prices[next_idx]
        if signal == "BUY" and position is None:
            # Next-sample zero prices cannot size an entry.
            if next_price <= 0:
                continue
            shares = position_size / next_price
            position = {
                "entry_price": next_price,
                "entry_idx": next_idx,
                "entry_signal_idx": i,
                "shares": shares,
            }
        elif signal == "SELL" and position is not None:
            pnl = (next_price - position["entry_price"]) * position["shares"]
            trade_log.append(
                {
                    "timestamp": timestamps[next_idx] if next_idx < len(timestamps) else "",
                    "signal_timestamp": timestamps[i] if i < len(timestamps) else "",
                    "side": "SELL",
                    "entry_price": position["entry_price"],
                    "exit_price": next_price,
                    "size": position_size,
                    "shares": position["shares"],
                    "notional_usdc": position_size,
                    "price": next_price,
                    "pnl": round(pnl, 4),
                    "strategy": strategy,
                    "fill_mode": "next_sample_indicative",
                    "cost_quality": "unknown",
                    "entry_signal_idx": position["entry_signal_idx"],
                    "entry_idx": position["entry_idx"],
                    "exit_signal_idx": i,
                    "exit_idx": next_idx,
                }
            )
            position = None

    # Unclosed inventory is marked, NOT converted to a fictitious last-bar
    # SELL. Realized PnL covers only modeled completed round trips.
    unrealized = None
    if position is not None:
        unrealized = round((prices[-1] - position["entry_price"]) * position["shares"], 4)

    return {
        "trade_log": trade_log,
        "metrics": _calculate_metrics(trade_log),
        "indicator_summary": generate_indicator_summary(indicators),
        "indicator_values": {
            **{
                k: {kk: vv for kk, vv in v.items() if kk != "series"}
                for k, v in indicators.items()
                if isinstance(v, dict) and "series" in v
            },
            "history_source": provenance,
            "history_quality": "indicative_price_only" if provenance else "unverified_fixture",
            "execution_assumption": "next_sample_indicative_no_verified_book",
            "net_pnl_quality": "costs_unknown",
            "unrealized_pnl_indicative": unrealized,
            "unclosed_position": position,
        },
    }


async def create_and_run_backtest(
    user_id: int,
    strategy_type: str,
    strategy_name: str,
    start_date: str,
    end_date: str,
    parameters: dict[str, Any],
) -> BacktestRun:
    """Create a BacktestRun record and execute the backtest."""
    _validate_backtest_date_range(start_date=start_date, end_date=end_date)

    db = SessionLocal()
    try:
        run = BacktestRun(
            user_id=user_id,
            strategy_type=strategy_type,
            strategy_name=strategy_name,
            start_date=start_date,
            end_date=end_date,
            parameters=parameters,
            status="running",
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        run_id = run.id
    finally:
        db.close()

    try:
        # Route to the right engine
        if strategy_type == "copy_trade":
            result = await run_copy_trade_backtest(
                user_id,
                {
                    **parameters,
                    "start_date": start_date,
                    "end_date": end_date,
                },
            )
        elif strategy_type in ("indicator", "custom"):
            result = await run_indicator_backtest(
                user_id,
                {
                    **parameters,
                    "start_date": start_date,
                    "end_date": end_date,
                },
            )
        else:
            result = {
                "trade_log": [],
                "metrics": _calculate_metrics([]),
                "error": f"Unknown strategy type: {strategy_type}",
            }

        metrics = result.get("metrics", {})
        trade_log = result.get("trade_log", [])

        # Update the run with results
        db = SessionLocal()
        try:
            run = BacktestRepository(db).get_run(run_id)
            if run:
                run.total_trades = metrics.get("total_trades", 0)
                run.winning_trades = metrics.get("winning_trades", 0)
                run.losing_trades = metrics.get("losing_trades", 0)
                run.win_rate = metrics.get("win_rate")
                run.total_pnl = metrics.get("total_pnl", 0)
                run.max_drawdown = metrics.get("max_drawdown")
                run.sharpe_ratio = metrics.get("sharpe_ratio")
                run.profit_factor = metrics.get("profit_factor")
                run.avg_trade_pnl = metrics.get("avg_trade_pnl")
                run.max_consecutive_losses = metrics.get("max_consecutive_losses")
                run.total_volume = metrics.get("total_volume")
                run.trade_log = trade_log
                run.indicator_values = result.get("indicator_values")
                run.status = "failed" if result.get("error") else "completed"
                run.completed_at = utc_now()
                if result.get("error"):
                    run.error_message = result["error"]
                db.commit()
                db.refresh(run)
                return run
        finally:
            db.close()

    except Exception as e:
        logger.error("Backtest run %d failed: %s", run_id, e, exc_info=True)
        db = SessionLocal()
        try:
            run = BacktestRepository(db).get_run(run_id)
            if run:
                run.status = "failed"
                run.error_message = str(e)[:500]
                db.commit()
                db.refresh(run)
                return run
        finally:
            db.close()

    # Fallback
    db = SessionLocal()
    try:
        return BacktestRepository(db).get_run(run_id)
    finally:
        db.close()


def get_backtest_runs(user_id: int, limit: int = 20) -> list[BacktestRun]:
    """Get recent backtest runs for a user."""
    db = SessionLocal()
    try:
        return BacktestRepository(db).list_runs(user_id, limit)
    finally:
        db.close()


def get_backtest_run(run_id: int, user_id: int) -> BacktestRun | None:
    """Get a specific backtest run."""
    db = SessionLocal()
    try:
        return BacktestRepository(db).get_user_run(run_id, user_id)
    finally:
        db.close()


def delete_backtest_run(db: Session, run_id: int, user_id: int) -> bool:
    """Delete one backtest only when it belongs to the requesting user."""
    run = TradingBacktestRepository(db).find_run_for_user(run_id, user_id)
    if run is None:
        return False
    db.delete(run)
    db.commit()
    return True
