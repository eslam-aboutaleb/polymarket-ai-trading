"""Coverage tests for the backtesting service (``app.services.backtesting_service``).

All external I/O is mocked: the SQLAlchemy session factory, the
Polymarket data HTTP API (httpx) and the indicator helpers.

Covers:

* date parsing/validation (valid, invalid, inverted range),
* ``_calculate_metrics`` — empty log, mixed wins/losses, single
  trade (no Sharpe), all-winners (no profit factor), zero-variance
  (no Sharpe), max drawdown, consecutive losses, volume,
* ``_fetch_historical_prices`` — list payload, ``{"history": ...}``
  payload, dict without history, non-200, transport error,
* ``run_copy_trade_backtest`` — replay with sizing caps, daily loss
  limit, day rollover, zero size/price skips, missing timestamps,
* ``run_indicator_backtest`` — no data, insufficient data, RSI /
  MACD / Bollinger signal paths, open-position close-out, unknown
  strategy, ``mid``/``t`` payload keys and None-row filtering,
* ``create_and_run_backtest`` — routing to copy_trade / indicator /
  custom / unknown, the failure path and the not-found fallback,
* ``get_backtest_runs`` / ``get_backtest_run`` queries.
"""

import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import app.services.backtesting_service as backtesting_service
from app.services.backtesting_service import (
    _calculate_metrics,
    _parse_backtest_date,
    _validate_backtest_date_range,
    create_and_run_backtest,
    get_backtest_run,
    get_backtest_runs,
    run_copy_trade_backtest,
    run_indicator_backtest,
)

# ── Date helpers ──────────────────────────────────────────────


class DateParsingTests(unittest.TestCase):
    def test_parse_valid_date(self):
        parsed = _parse_backtest_date("2024-06-15", "start_date")
        self.assertEqual(parsed, datetime(2024, 6, 15))

    def test_parse_invalid_date_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            _parse_backtest_date("15-06-2024", "start_date")
        self.assertIn("Invalid start_date", str(ctx.exception))
        self.assertIn("YYYY-MM-DD", str(ctx.exception))

    def test_validate_range_accepts_equal_dates(self):
        _validate_backtest_date_range("2024-01-01", "2024-01-01")

    def test_validate_range_accepts_ordered_dates(self):
        _validate_backtest_date_range("2024-01-01", "2024-12-31")

    def test_validate_range_rejects_inverted_dates(self):
        with self.assertRaises(ValueError) as ctx:
            _validate_backtest_date_range("2024-12-31", "2024-01-01")
        self.assertIn("start_date must be on or before end_date", str(ctx.exception))


# ── Metric calculators ────────────────────────────────────────


class CalculateMetricsTests(unittest.TestCase):
    def test_empty_log_returns_zeroed_metrics(self):
        metrics = _calculate_metrics([])
        self.assertEqual(metrics["total_trades"], 0)
        self.assertEqual(metrics["winning_trades"], 0)
        self.assertEqual(metrics["losing_trades"], 0)
        self.assertEqual(metrics["win_rate"], 0.0)
        self.assertEqual(metrics["total_pnl"], 0.0)
        self.assertEqual(metrics["max_drawdown"], 0.0)
        self.assertIsNone(metrics["sharpe_ratio"])
        self.assertIsNone(metrics["profit_factor"])
        self.assertEqual(metrics["avg_trade_pnl"], 0.0)
        self.assertEqual(metrics["max_consecutive_losses"], 0)
        self.assertEqual(metrics["total_volume"], 0.0)

    def test_mixed_trades(self):
        trade_log = [
            {"pnl": 10.0, "size": 100, "price": 0.5},
            {"pnl": -5.0, "size": 100, "price": 0.5},
            {"pnl": 5.0, "size": 50, "price": 0.4},
        ]
        metrics = _calculate_metrics(trade_log)
        self.assertEqual(metrics["total_trades"], 3)
        self.assertEqual(metrics["winning_trades"], 2)
        self.assertEqual(metrics["losing_trades"], 1)
        self.assertAlmostEqual(metrics["win_rate"], 66.67)
        self.assertAlmostEqual(metrics["total_pnl"], 10.0)
        self.assertAlmostEqual(metrics["avg_trade_pnl"], round(10.0 / 3, 4))
        # gross profit 15 / gross loss 5 = 3.0
        self.assertAlmostEqual(metrics["profit_factor"], 3.0)
        # volume = 100*0.5 + 100*0.5 + 50*0.4 = 120
        self.assertAlmostEqual(metrics["total_volume"], 120.0)
        # Sharpe computed for >1 trades with variance
        self.assertIsNotNone(metrics["sharpe_ratio"])

    def test_single_trade_has_no_sharpe(self):
        metrics = _calculate_metrics([{"pnl": 5.0, "size": 10, "price": 1.0}])
        self.assertIsNone(metrics["sharpe_ratio"])
        self.assertIsNone(metrics["profit_factor"])  # no losses
        self.assertAlmostEqual(metrics["win_rate"], 100.0)

    def test_all_winners_have_no_profit_factor(self):
        metrics = _calculate_metrics(
            [{"pnl": 1.0, "size": 1, "price": 1.0}, {"pnl": 2.0, "size": 1, "price": 1.0}]
        )
        self.assertIsNone(metrics["profit_factor"])

    def test_zero_variance_has_no_sharpe(self):
        metrics = _calculate_metrics(
            [{"pnl": 1.0, "size": 1, "price": 1.0}, {"pnl": 1.0, "size": 1, "price": 1.0}]
        )
        self.assertIsNone(metrics["sharpe_ratio"])

    def test_max_drawdown(self):
        # Cumulative: 10, 5, -5, 0 → peak 10, deepest trough -5 → DD 15.
        trade_log = [
            {"pnl": 10.0, "size": 1, "price": 1.0},
            {"pnl": -5.0, "size": 1, "price": 1.0},
            {"pnl": -10.0, "size": 1, "price": 1.0},
            {"pnl": 5.0, "size": 1, "price": 1.0},
        ]
        self.assertAlmostEqual(_calculate_metrics(trade_log)["max_drawdown"], 15.0)

    def test_max_consecutive_losses(self):
        trade_log = [
            {"pnl": 1.0, "size": 1, "price": 1.0},
            {"pnl": -1.0, "size": 1, "price": 1.0},
            {"pnl": -2.0, "size": 1, "price": 1.0},
            {"pnl": -3.0, "size": 1, "price": 1.0},
            {"pnl": 1.0, "size": 1, "price": 1.0},
        ]
        self.assertEqual(_calculate_metrics(trade_log)["max_consecutive_losses"], 3)

    def test_zero_pnl_trades_count_as_neither_win_nor_loss(self):
        metrics = _calculate_metrics([{"pnl": 0.0, "size": 1, "price": 1.0}])
        self.assertEqual(metrics["winning_trades"], 0)
        self.assertEqual(metrics["losing_trades"], 0)
        self.assertEqual(metrics["win_rate"], 0.0)

    def test_missing_pnl_defaults_to_zero(self):
        metrics = _calculate_metrics([{"size": 1, "price": 1.0}])
        self.assertEqual(metrics["total_pnl"], 0.0)


# ── Historical price fetching ─────────────────────────────────


def _httpx_client(response):
    client = MagicMock(name="async_client")
    client.get = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _response(status_code=200, json_data=None):
    response = MagicMock(name="response")
    response.status_code = status_code
    response.json = MagicMock(return_value=json_data)
    return response


class FetchHistoricalPricesTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_payload_returned_as_is(self):
        data = [{"price": 1.0}, {"price": 2.0}]
        response = _response(200, data)
        with (
            patch("httpx.AsyncClient", return_value=_httpx_client(response)),
            patch("app.services.polymarket_service.POLYMARKET_DATA_API", "https://data.example"),
        ):
            prices = await backtesting_service._fetch_historical_prices(
                "cond-1", "2024-01-01", "2024-01-02"
            )
        self.assertEqual(prices, data)

    async def test_history_dict_payload_unwrapped(self):
        history = [{"price": 1.0}]
        response = _response(200, {"history": history})
        with (
            patch("httpx.AsyncClient", return_value=_httpx_client(response)),
            patch("app.services.polymarket_service.POLYMARKET_DATA_API", "https://data.example"),
        ):
            prices = await backtesting_service._fetch_historical_prices(
                "cond-1", "2024-01-01", "2024-01-02"
            )
        self.assertEqual(prices, history)

    async def test_dict_without_history_returns_empty(self):
        response = _response(200, {"foo": "bar"})
        with (
            patch("httpx.AsyncClient", return_value=_httpx_client(response)),
            patch("app.services.polymarket_service.POLYMARKET_DATA_API", "https://data.example"),
        ):
            prices = await backtesting_service._fetch_historical_prices(
                "cond-1", "2024-01-01", "2024-01-02"
            )
        self.assertEqual(prices, [])

    async def test_non_200_returns_empty(self):
        response = _response(500, [{"price": 1.0}])
        with (
            patch("httpx.AsyncClient", return_value=_httpx_client(response)),
            patch("app.services.polymarket_service.POLYMARKET_DATA_API", "https://data.example"),
        ):
            prices = await backtesting_service._fetch_historical_prices(
                "cond-1", "2024-01-01", "2024-01-02"
            )
        self.assertEqual(prices, [])

    async def test_transport_error_returns_empty_and_logs(self):
        client = MagicMock(name="async_client")
        client.get = AsyncMock(side_effect=RuntimeError("connection reset"))
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        with (
            patch("httpx.AsyncClient", return_value=client),
            patch("app.services.polymarket_service.POLYMARKET_DATA_API", "https://data.example"),
            self.assertLogs(backtesting_service.logger, level="WARNING"),
        ):
            prices = await backtesting_service._fetch_historical_prices(
                "cond-1", "2024-01-01", "2024-01-02"
            )
        self.assertEqual(prices, [])


# ── Copy-trade replay ─────────────────────────────────────────


def _trade(executed_at, amount=100.0, price=0.5, pnl=10.0, market_id="mkt-1", action="buy"):
    trade = MagicMock(name="user_trade")
    trade.executed_at = executed_at
    trade.amount = amount
    trade.price = price
    trade.pnl = pnl
    trade.market_id = market_id
    trade.action = action
    trade.copied_from_wallet = "0xwallet"
    return trade


def _copy_trade_db(trades):
    db = MagicMock(name="db")
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = trades
    return db


class RunCopyTradeBacktestTests(unittest.IsolatedAsyncioTestCase):
    async def test_replays_trades_with_metrics(self):
        base = datetime(2024, 6, 15, 12, 0, 0)
        trades = [
            _trade(base, amount=100.0, price=0.5, pnl=10.0),
            _trade(base + timedelta(minutes=5), amount=50.0, price=0.4, pnl=-5.0),
        ]
        db = _copy_trade_db(trades)
        with patch.object(backtesting_service, "SessionLocal", return_value=db):
            result = await run_copy_trade_backtest(
                1,
                {
                    "followed_wallet": "0xwallet",
                    "start_date": "2024-01-01",
                    "end_date": "2024-12-31",
                },
            )
        self.assertEqual(len(result["trade_log"]), 2)
        entry = result["trade_log"][0]
        self.assertEqual(entry["timestamp"], base.isoformat())
        self.assertEqual(entry["market_id"], "mkt-1")
        self.assertEqual(entry["side"], "buy")
        self.assertEqual(entry["size"], 100.0)
        self.assertEqual(entry["price"], 0.5)
        self.assertEqual(entry["pnl"], 10.0)
        self.assertEqual(result["metrics"]["total_trades"], 2)
        self.assertAlmostEqual(result["metrics"]["total_pnl"], 5.0)
        db.close.assert_called_once()

    async def test_position_size_capped_at_max(self):
        base = datetime(2024, 6, 15, 12, 0, 0)
        trades = [_trade(base, amount=500.0, price=0.5, pnl=1.0)]
        db = _copy_trade_db(trades)
        with patch.object(backtesting_service, "SessionLocal", return_value=db):
            result = await run_copy_trade_backtest(
                1, {"followed_wallet": "0xwallet", "max_position_size": 100}
            )
        self.assertEqual(result["trade_log"][0]["size"], 100.0)

    async def test_daily_loss_limit_skips_subsequent_trades(self):
        base = datetime(2024, 6, 15, 12, 0, 0)
        trades = [
            _trade(base, amount=100.0, price=0.5, pnl=-60.0),
            _trade(base + timedelta(minutes=5), amount=100.0, price=0.5, pnl=10.0),
        ]
        db = _copy_trade_db(trades)
        with patch.object(backtesting_service, "SessionLocal", return_value=db):
            result = await run_copy_trade_backtest(
                1, {"followed_wallet": "0xwallet", "daily_loss_limit": 50}
            )
        # First trade breaches the 50 USDC daily loss limit; the
        # second trade on the same day is skipped.
        self.assertEqual(len(result["trade_log"]), 1)
        self.assertEqual(result["trade_log"][0]["pnl"], -60.0)

    async def test_daily_loss_resets_on_new_day(self):
        day1 = datetime(2024, 6, 15, 12, 0, 0)
        day2 = datetime(2024, 6, 16, 12, 0, 0)
        trades = [
            _trade(day1, amount=100.0, price=0.5, pnl=-60.0),
            _trade(day2, amount=100.0, price=0.5, pnl=10.0),
        ]
        db = _copy_trade_db(trades)
        with patch.object(backtesting_service, "SessionLocal", return_value=db):
            result = await run_copy_trade_backtest(
                1, {"followed_wallet": "0xwallet", "daily_loss_limit": 50}
            )
        self.assertEqual(len(result["trade_log"]), 2)

    async def test_zero_size_or_price_skipped(self):
        base = datetime(2024, 6, 15, 12, 0, 0)
        trades = [
            _trade(base, amount=0.0, price=0.5, pnl=1.0),
            _trade(base, amount=100.0, price=0.0, pnl=1.0),
            _trade(base, amount=None, price=0.5, pnl=1.0),
            _trade(base, amount=100.0, price=None, pnl=1.0),
        ]
        db = _copy_trade_db(trades)
        with patch.object(backtesting_service, "SessionLocal", return_value=db):
            result = await run_copy_trade_backtest(1, {"followed_wallet": "0xwallet"})
        self.assertEqual(result["trade_log"], [])

    async def test_missing_executed_at_uses_empty_timestamp(self):
        trades = [_trade(None, amount=100.0, price=0.5, pnl=1.0)]
        db = _copy_trade_db(trades)
        with patch.object(backtesting_service, "SessionLocal", return_value=db):
            result = await run_copy_trade_backtest(1, {"followed_wallet": "0xwallet"})
        self.assertEqual(result["trade_log"][0]["timestamp"], "")

    async def test_no_trades_returns_empty_log(self):
        db = _copy_trade_db([])
        with patch.object(backtesting_service, "SessionLocal", return_value=db):
            result = await run_copy_trade_backtest(1, {"followed_wallet": "0xwallet"})
        self.assertEqual(result["trade_log"], [])
        self.assertEqual(result["metrics"]["total_trades"], 0)


# ── Indicator replay ──────────────────────────────────────────


def _price_data(prices):
    return [{"price": p, "timestamp": f"t{i}"} for i, p in enumerate(prices)]


def _indicator_patches(rsi_series=None, macd_data=None, bb_data=None):
    """Patch the indicator helpers the strategy loop uses."""
    n = 60
    if rsi_series is None:
        rsi_series = [None] * n
    if macd_data is None:
        macd_data = {"macd": [None] * n, "signal": [None] * n, "histogram": [None] * n}
    if bb_data is None:
        bb_data = {"lower": [None] * n, "upper": [None] * n, "middle": [None] * n}
    return (
        patch("app.utils.indicators.rsi", MagicMock(return_value=rsi_series)),
        patch("app.utils.indicators.macd", MagicMock(return_value=macd_data)),
        patch("app.utils.indicators.bollinger_bands", MagicMock(return_value=bb_data)),
    )


INDICATORS_RESULT = {
    "price_count": 60,
    "latest_price": 110.0,
    "sma": {"period": 20, "latest": 105.0, "series": [None] * 60},
    "rsi": {"period": 14, "latest": 50.0, "series": [None] * 60},
}


class RunIndicatorBacktestTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, params, price_data):
        fetch = AsyncMock(return_value=price_data)
        rsi_patch, macd_patch, bb_patch = _indicator_patches(
            params.pop("_rsi", None), params.pop("_macd", None), params.pop("_bb", None)
        )
        with (
            patch.object(backtesting_service, "_fetch_historical_prices", fetch),
            patch.object(
                backtesting_service,
                "compute_all_indicators",
                MagicMock(return_value=INDICATORS_RESULT),
            ),
            patch.object(
                backtesting_service, "generate_indicator_summary", MagicMock(return_value="summary")
            ),
            rsi_patch,
            macd_patch,
            bb_patch,
        ):
            return await run_indicator_backtest(1, params)

    async def test_no_price_data_returns_error(self):
        result = await self._run({"condition_id": "c1"}, [])
        self.assertEqual(result["trade_log"], [])
        self.assertEqual(result["metrics"]["total_trades"], 0)
        self.assertIn("No historical price data", result["error"])

    async def test_insufficient_price_data_returns_error(self):
        result = await self._run({"condition_id": "c1"}, _price_data([100.0] * 29))
        self.assertEqual(result["trade_log"], [])
        self.assertIn("Insufficient price data", result["error"])

    async def test_rsi_mean_reversion_buy_and_sell(self):
        prices = [100.0] * 60
        prices[44] = 110.0
        rsi_series = [None] * 60
        rsi_series[34] = 25.0  # oversold → BUY
        rsi_series[44] = 75.0  # overbought → SELL
        result = await self._run(
            {"condition_id": "c1", "strategy": "rsi_mean_reversion", "_rsi": rsi_series},
            _price_data(prices),
        )
        self.assertEqual(len(result["trade_log"]), 1)
        trade = result["trade_log"][0]
        self.assertEqual(trade["side"], "SELL")
        self.assertEqual(trade["entry_price"], 100.0)
        self.assertEqual(trade["exit_price"], 110.0)
        self.assertEqual(trade["strategy"], "rsi_mean_reversion")
        # pnl = (110 - 100) * (10 / 100) = 1.0
        self.assertAlmostEqual(trade["pnl"], 1.0)
        self.assertEqual(result["metrics"]["total_trades"], 1)
        self.assertEqual(result["indicator_summary"], "summary")
        self.assertEqual(result["indicator_values"]["sma"]["latest"], 105.0)
        self.assertNotIn("series", result["indicator_values"]["sma"])

    async def test_rsi_open_position_closed_at_last_price(self):
        prices = [100.0] * 60
        rsi_series = [None] * 60
        rsi_series[34] = 25.0  # BUY, never sold
        result = await self._run(
            {"condition_id": "c1", "strategy": "rsi_mean_reversion", "_rsi": rsi_series},
            _price_data(prices),
        )
        self.assertEqual(len(result["trade_log"]), 1)
        trade = result["trade_log"][0]
        self.assertEqual(trade["entry_price"], 100.0)
        self.assertEqual(trade["exit_price"], 100.0)  # last price
        self.assertEqual(trade["timestamp"], "t59")

    async def test_rsi_buy_requires_no_position(self):
        # Two oversold readings in a row: only the first opens
        # a position; the position is then closed at the last
        # price (one round-trip trade in the log).
        prices = [100.0] * 60
        rsi_series = [None] * 60
        rsi_series[34] = 25.0
        rsi_series[35] = 20.0
        result = await self._run(
            {"condition_id": "c1", "strategy": "rsi_mean_reversion", "_rsi": rsi_series},
            _price_data(prices),
        )
        self.assertEqual(len(result["trade_log"]), 1)
        self.assertEqual(result["trade_log"][0]["entry_price"], 100.0)

    async def test_macd_crossover_buy_and_sell(self):
        prices = [100.0] * 60
        prices[44] = 110.0
        macd_line = [None] * 60
        signal_line = [None] * 60
        macd_line[33], signal_line[33] = 1.0, 2.0
        macd_line[34], signal_line[34] = 3.0, 1.0  # bullish cross → BUY
        macd_line[43], signal_line[43] = 3.0, 1.0
        macd_line[44], signal_line[44] = 1.0, 3.0  # bearish cross → SELL
        macd_data = {"macd": macd_line, "signal": signal_line, "histogram": [None] * 60}
        result = await self._run(
            {"condition_id": "c1", "strategy": "macd_crossover", "_macd": macd_data},
            _price_data(prices),
        )
        self.assertEqual(len(result["trade_log"]), 1)
        self.assertAlmostEqual(result["trade_log"][0]["pnl"], 1.0)

    async def test_macd_requires_previous_values(self):
        # Crossover at i=34 but i-1 values are None → no signal.
        prices = [100.0] * 60
        macd_line = [None] * 60
        signal_line = [None] * 60
        macd_line[34], signal_line[34] = 3.0, 1.0
        macd_data = {"macd": macd_line, "signal": signal_line, "histogram": [None] * 60}
        result = await self._run(
            {"condition_id": "c1", "strategy": "macd_crossover", "_macd": macd_data},
            _price_data(prices),
        )
        self.assertEqual(result["trade_log"], [])

    async def test_bollinger_bounce_buy_and_sell(self):
        prices = [100.0] * 60
        prices[44] = 110.0
        lower = [None] * 60
        upper = [None] * 60
        # Both bands must be present at an index for a signal.
        lower[34], upper[34] = 105.0, 200.0  # price 100 <= lower → BUY
        lower[44], upper[44] = 50.0, 105.0  # price 110 >= upper → SELL
        bb_data = {"lower": lower, "upper": upper, "middle": [None] * 60}
        result = await self._run(
            {"condition_id": "c1", "strategy": "bollinger_bounce", "_bb": bb_data},
            _price_data(prices),
        )
        self.assertEqual(len(result["trade_log"]), 1)
        self.assertAlmostEqual(result["trade_log"][0]["pnl"], 1.0)

    async def test_bollinger_requires_both_bands(self):
        prices = [100.0] * 60
        bb_data = {"lower": [None] * 60, "upper": [None] * 60, "middle": [None] * 60}
        result = await self._run(
            {"condition_id": "c1", "strategy": "bollinger_bounce", "_bb": bb_data},
            _price_data(prices),
        )
        self.assertEqual(result["trade_log"], [])

    async def test_unknown_strategy_generates_no_trades(self):
        result = await self._run(
            {"condition_id": "c1", "strategy": "unknown"}, _price_data([100.0] * 60)
        )
        self.assertEqual(result["trade_log"], [])
        self.assertEqual(result["metrics"]["total_trades"], 0)
        self.assertEqual(result["indicator_summary"], "summary")

    async def test_mid_and_t_payload_keys(self):
        prices = [100.0] * 60
        price_data = [{"mid": p, "t": f"t{i}"} for i, p in enumerate(prices)]
        result = await self._run({"condition_id": "c1"}, price_data)
        self.assertEqual(result["trade_log"], [])
        self.assertEqual(result["metrics"]["total_trades"], 0)

    async def test_none_rows_filtered_from_price_data(self):
        prices = [100.0] * 30
        price_data = [None] + _price_data(prices)
        result = await self._run({"condition_id": "c1"}, price_data)
        # 30 valid rows survive the ``if p`` filter — exactly enough
        # to proceed (loop range is empty).
        self.assertEqual(result["metrics"]["total_trades"], 0)
        self.assertNotIn("error", result)

    async def test_custom_thresholds_forwarded(self):
        prices = [100.0] * 60
        rsi_series = [None] * 60
        rsi_series[34] = 20.0  # below custom oversold of 25
        result = await self._run(
            {
                "condition_id": "c1",
                "strategy": "rsi_mean_reversion",
                "rsi_oversold": 25,
                "rsi_overbought": 75,
                "position_size": 5,
                "_rsi": rsi_series,
            },
            _price_data(prices),
        )
        # Position opened at 34 and closed at the last price.
        self.assertEqual(len(result["trade_log"]), 1)
        self.assertEqual(result["trade_log"][0]["size"], 5)


# ── Run creation and routing ──────────────────────────────────


def _run_db(run=None):
    db = MagicMock(name="db")
    db.query.return_value.filter.return_value.first.return_value = run
    db.query.return_value.filter.return_value.all.return_value = []
    return db


class CreateAndRunBacktestTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_date_range_raises_before_db_access(self):
        db = _run_db()
        with (
            patch.object(backtesting_service, "SessionLocal", return_value=db),
            self.assertRaises(ValueError),
        ):
            await create_and_run_backtest(1, "copy_trade", "test", "2024-12-31", "2024-01-01", {})
        db.query.assert_not_called()

    async def test_copy_trade_routing(self):
        db = _run_db(run=MagicMock(name="run"))
        engine = AsyncMock(
            return_value={
                "trade_log": [{"pnl": 1.0, "size": 10, "price": 1.0}],
                "metrics": _calculate_metrics([{"pnl": 1.0, "size": 10, "price": 1.0}]),
            }
        )
        with (
            patch.object(backtesting_service, "SessionLocal", return_value=db),
            patch.object(backtesting_service, "BacktestRun") as run_cls,
            patch.object(backtesting_service, "run_copy_trade_backtest", engine),
        ):
            run_cls.return_value.id = 42
            run = await create_and_run_backtest(
                1,
                "copy_trade",
                "copy test",
                "2024-01-01",
                "2024-12-31",
                {"followed_wallet": "0xabc"},
            )
        engine.assert_awaited_once()
        kwargs = engine.await_args.kwargs if engine.await_args.kwargs else {}
        args = engine.await_args.args
        self.assertEqual(args[0], 1)
        self.assertEqual(args[1]["followed_wallet"], "0xabc")
        self.assertEqual(args[1]["start_date"], "2024-01-01")
        self.assertEqual(args[1]["end_date"], "2024-12-31")
        self.assertEqual(kwargs, {})
        # The run record is updated with the engine's metrics.
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.total_trades, 1)
        self.assertEqual(run.winning_trades, 1)
        self.assertAlmostEqual(run.total_pnl, 1.0)
        self.assertEqual(run.trade_log, [{"pnl": 1.0, "size": 10, "price": 1.0}])
        db.commit.assert_called()
        db.refresh.assert_called()

    async def test_indicator_routing(self):
        db = _run_db(run=MagicMock(name="run"))
        engine = AsyncMock(
            return_value={
                "trade_log": [],
                "metrics": _calculate_metrics([]),
                "indicator_values": {"sma": {"latest": 1.0}},
            }
        )
        with (
            patch.object(backtesting_service, "SessionLocal", return_value=db),
            patch.object(backtesting_service, "BacktestRun") as run_cls,
            patch.object(backtesting_service, "run_indicator_backtest", engine),
        ):
            run_cls.return_value.id = 42
            run = await create_and_run_backtest(
                1,
                "indicator",
                "ind test",
                "2024-01-01",
                "2024-12-31",
                {"condition_id": "c1"},
            )
        engine.assert_awaited_once()
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.indicator_values, {"sma": {"latest": 1.0}})

    async def test_custom_routing_uses_indicator_engine(self):
        db = _run_db(run=MagicMock(name="run"))
        engine = AsyncMock(return_value={"trade_log": [], "metrics": _calculate_metrics([])})
        with (
            patch.object(backtesting_service, "SessionLocal", return_value=db),
            patch.object(backtesting_service, "BacktestRun") as run_cls,
            patch.object(backtesting_service, "run_indicator_backtest", engine),
        ):
            run_cls.return_value.id = 42
            await create_and_run_backtest(
                1, "custom", "custom test", "2024-01-01", "2024-12-31", {}
            )
        engine.assert_awaited_once()

    async def test_unknown_strategy_type_records_error(self):
        db = _run_db(run=MagicMock(name="run"))
        with (
            patch.object(backtesting_service, "SessionLocal", return_value=db),
            patch.object(backtesting_service, "BacktestRun") as run_cls,
        ):
            run_cls.return_value.id = 42
            run = await create_and_run_backtest(
                1, "inverse_bot", "unknown", "2024-01-01", "2024-12-31", {}
            )
        self.assertEqual(run.status, "completed")
        self.assertIn("Unknown strategy type: inverse_bot", run.error_message)

    async def test_engine_error_marks_run_failed(self):
        db = _run_db(run=MagicMock(name="run"))
        engine = AsyncMock(side_effect=RuntimeError("boom"))
        with (
            patch.object(backtesting_service, "SessionLocal", return_value=db),
            patch.object(backtesting_service, "BacktestRun") as run_cls,
            patch.object(backtesting_service, "run_copy_trade_backtest", engine),
            self.assertLogs(backtesting_service.logger, level="ERROR"),
        ):
            run_cls.return_value.id = 42
            run = await create_and_run_backtest(
                1, "copy_trade", "failing", "2024-01-01", "2024-12-31", {}
            )
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error_message, "boom")

    async def test_run_not_found_falls_back_to_none(self):
        db = _run_db(run=None)
        engine = AsyncMock(return_value={"trade_log": [], "metrics": _calculate_metrics([])})
        with (
            patch.object(backtesting_service, "SessionLocal", return_value=db),
            patch.object(backtesting_service, "BacktestRun") as run_cls,
            patch.object(backtesting_service, "run_copy_trade_backtest", engine),
        ):
            run_cls.return_value.id = 42
            run = await create_and_run_backtest(
                1, "copy_trade", "missing", "2024-01-01", "2024-12-31", {}
            )
        self.assertIsNone(run)

    async def test_error_path_run_not_found_returns_none(self):
        db = _run_db(run=None)
        engine = AsyncMock(side_effect=RuntimeError("boom"))
        with (
            patch.object(backtesting_service, "SessionLocal", return_value=db),
            patch.object(backtesting_service, "BacktestRun") as run_cls,
            patch.object(backtesting_service, "run_copy_trade_backtest", engine),
            self.assertLogs(backtesting_service.logger, level="ERROR"),
        ):
            run_cls.return_value.id = 42
            run = await create_and_run_backtest(
                1, "copy_trade", "missing", "2024-01-01", "2024-12-31", {}
            )
        self.assertIsNone(run)


# ── Run listing queries ───────────────────────────────────────


class RunListingTests(unittest.TestCase):
    def test_get_backtest_runs_returns_limited_list(self):
        run1, run2 = MagicMock(name="run1"), MagicMock(name="run2")
        db = MagicMock(name="db")
        (
            db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value
        ) = [run1, run2]
        with patch.object(backtesting_service, "SessionLocal", return_value=db):
            runs = get_backtest_runs(1, limit=2)
        self.assertEqual(runs, [run1, run2])
        db.query.return_value.filter.return_value.order_by.return_value.limit.assert_called_once_with(
            2
        )
        db.close.assert_called_once()

    def test_get_backtest_run_returns_matching_run(self):
        run = MagicMock(name="run")
        db = MagicMock(name="db")
        db.query.return_value.filter.return_value.first.return_value = run
        with patch.object(backtesting_service, "SessionLocal", return_value=db):
            found = get_backtest_run(42, 1)
        self.assertIs(found, run)
        db.close.assert_called_once()

    def test_get_backtest_run_returns_none_when_missing(self):
        db = MagicMock(name="db")
        db.query.return_value.filter.return_value.first.return_value = None
        with patch.object(backtesting_service, "SessionLocal", return_value=db):
            self.assertIsNone(get_backtest_run(42, 1))


if __name__ == "__main__":
    unittest.main()
