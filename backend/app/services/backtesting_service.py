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

from app.models.backtest_run import BacktestRun
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
    volumes = [abs(t.get("size", 0) * t.get("price", 0)) for t in trade_log]

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

    # Sharpe ratio (annualized, assuming daily trades)
    sharpe = None
    if len(pnls) > 1:
        mean_pnl = sum(pnls) / len(pnls)
        variance = sum((p - mean_pnl) ** 2 for p in pnls) / (len(pnls) - 1)
        std_pnl = math.sqrt(variance) if variance > 0 else 0
        if std_pnl > 0:
            sharpe = round((mean_pnl / std_pnl) * math.sqrt(252), 4)

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
) -> list[dict[str, Any]]:
    """Fetch historical price data from Polymarket data API."""
    import httpx

    prices: list[dict[str, Any]] = []
    try:
        from app.services.polymarket_service import POLYMARKET_DATA_API

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{POLYMARKET_DATA_API}/prices",
                params={
                    "market": condition_id,
                    "startDate": start_date,
                    "endDate": end_date,
                    "interval": "1h",
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    prices = data
                elif isinstance(data, dict) and "history" in data:
                    prices = data["history"]
    except Exception as e:
        logger.warning("Could not fetch historical prices for %s: %s", condition_id[:20], e)

    return prices


async def run_copy_trade_backtest(
    user_id: int,
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
        from app.models.user_trade import UserTrade

        followed_wallet = params.get("followed_wallet", "")
        max_pos_size = float(params.get("max_position_size", 100))
        daily_loss_limit = float(params.get("daily_loss_limit", 50))

        # Fetch historical trades from this wallet
        trades = (
            db.query(UserTrade)
            .filter(
                UserTrade.copied_from_wallet == followed_wallet,
                UserTrade.executed_at >= params.get("start_date", "2024-01-01"),
                UserTrade.executed_at <= params.get("end_date", "2025-12-31"),
            )
            .order_by(UserTrade.executed_at.asc())
            .all()
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
    user_id: int,
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

    # Fetch historical prices
    price_data = await _fetch_historical_prices(
        condition_id,
        params.get("start_date", "2024-01-01"),
        params.get("end_date", "2025-12-31"),
    )

    if not price_data:
        return {
            "trade_log": [],
            "metrics": _calculate_metrics([]),
            "error": "No historical price data available",
        }

    prices = [float(p.get("price", p.get("mid", 0.5))) for p in price_data if p]
    timestamps = [p.get("timestamp", p.get("t", "")) for p in price_data if p]

    if len(prices) < 30:
        return {
            "trade_log": [],
            "metrics": _calculate_metrics([]),
            "error": "Insufficient price data (need at least 30 data points)",
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

    for i in range(30, len(prices)):
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

        # Execute signal
        if signal == "BUY" and position is None:
            position = {"entry_price": prices[i], "entry_idx": i}
        elif signal == "SELL" and position is not None:
            pnl = (prices[i] - position["entry_price"]) * (position_size / position["entry_price"])
            trade_log.append(
                {
                    "timestamp": timestamps[i] if i < len(timestamps) else "",
                    "side": "SELL",
                    "entry_price": position["entry_price"],
                    "exit_price": prices[i],
                    "size": position_size,
                    "price": prices[i],
                    "pnl": round(pnl, 4),
                    "strategy": strategy,
                }
            )
            position = None

    # Close open position at last price
    if position is not None:
        pnl = (prices[-1] - position["entry_price"]) * (position_size / position["entry_price"])
        trade_log.append(
            {
                "timestamp": timestamps[-1] if timestamps else "",
                "side": "SELL",
                "entry_price": position["entry_price"],
                "exit_price": prices[-1],
                "size": position_size,
                "price": prices[-1],
                "pnl": round(pnl, 4),
                "strategy": strategy,
            }
        )

    return {
        "trade_log": trade_log,
        "metrics": _calculate_metrics(trade_log),
        "indicator_summary": generate_indicator_summary(indicators),
        "indicator_values": {
            k: {kk: vv for kk, vv in v.items() if kk != "series"}
            for k, v in indicators.items()
            if isinstance(v, dict) and "series" in v
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
            run = db.query(BacktestRun).filter(BacktestRun.id == run_id).first()
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
                run.status = "completed"
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
            run = db.query(BacktestRun).filter(BacktestRun.id == run_id).first()
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
        return db.query(BacktestRun).filter(BacktestRun.id == run_id).first()
    finally:
        db.close()


def get_backtest_runs(user_id: int, limit: int = 20) -> list[BacktestRun]:
    """Get recent backtest runs for a user."""
    db = SessionLocal()
    try:
        return (
            db.query(BacktestRun)
            .filter(BacktestRun.user_id == user_id)
            .order_by(BacktestRun.created_at.desc())
            .limit(limit)
            .all()
        )
    finally:
        db.close()


def get_backtest_run(run_id: int, user_id: int) -> BacktestRun | None:
    """Get a specific backtest run."""
    db = SessionLocal()
    try:
        return (
            db.query(BacktestRun)
            .filter(
                BacktestRun.id == run_id,
                BacktestRun.user_id == user_id,
            )
            .first()
        )
    finally:
        db.close()
