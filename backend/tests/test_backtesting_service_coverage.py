"""Coverage tests for the backtesting service (``app.services.analytics.backtesting_service``).

All external I/O is mocked: the SQLAlchemy session factory, the
Polymarket data HTTP API (httpx) and the indicator helpers.

Covers:

* date parsing/validation (valid, invalid, inverted range),
* ``_calculate_metrics`` — empty log, mixed wins/losses, single
  trade (no Sharpe), all-winners (no profit factor), zero-variance
  (no Sharpe), max drawdown, consecutive losses, volume,
* ``_fetch_historical_prices`` — list payload, ``{"history": ...}``
  payload, dict without history, non-200, transport error,
* the recorded CLOB price-history fixture — ordering, price sequence,
  dataset hash, non-monotonic detection, closed markets, 429 handling
  and the observation cache (hit, corruption, upstream failure),
* ``run_copy_trade_backtest`` — replay with sizing caps, daily loss
  limit, day rollover, zero size/price skips, missing timestamps,
* ``run_indicator_backtest`` — no data, insufficient data, RSI /
  MACD / Bollinger signal paths, open-position close-out, unknown
  strategy, ``mid``/``t`` payload keys and None-row filtering,
* ``create_and_run_backtest`` — routing to copy_trade / indicator /
  custom / unknown, the failure path and the not-found fallback,
* ``get_backtest_runs`` / ``get_backtest_run`` queries.
"""

import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import app.services.analytics.backtesting_service as backtesting_service
from app.services.analytics.backtesting_service import (
    _calculate_metrics,
    _parse_backtest_date,
    _validate_backtest_date_range,
    create_and_run_backtest,
    get_backtest_run,
    get_backtest_runs,
    run_copy_trade_backtest,
    run_indicator_backtest,
)
from app.services.market_data.polymarket_service import market_data_cache

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _load_fixture(name: str) -> dict:
    with (FIXTURES_DIR / name).open(encoding="utf-8") as handle:
        return json.load(handle)


# Recorded on 2026-10-21 from the live CLOB API for the YES outcome token of an
# active market: verbatim /markets/{condition_id} and /prices-history payloads.
RECORDED_PRICES_HISTORY = _load_fixture("polymarket_clob_prices_history_yes.json")
# Recorded on 2026-10-21: a market that has already closed and settled.
RECORDED_RESOLVED_MARKET = _load_fixture("polymarket_clob_market_resolved.json")

# The recorded series covers 2026-10-16T11:00Z .. 2026-10-21T10:00Z.
RECORDED_START_DATE = "2026-10-16"
RECORDED_END_DATE = "2026-10-19"

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
        # Irregular USD PnLs cannot be annualized as periodic returns.
        self.assertIsNone(metrics["sharpe_ratio"])

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
    """Fixture-contract tests for the actual CLOB prices-history interface."""

    CONDITION = RECORDED_PRICES_HISTORY["market"]["condition_id"]
    YES = RECORDED_PRICES_HISTORY["market"]["tokens"][0]["token_id"]
    NO = RECORDED_PRICES_HISTORY["market"]["tokens"][1]["token_id"]

    def market(self):
        return json.loads(json.dumps(RECORDED_PRICES_HISTORY["market"]))

    def history(self, count=None):
        payload = json.loads(json.dumps(RECORDED_PRICES_HISTORY["prices_history"]))
        if count is not None:
            payload["history"] = payload["history"][:count]
        return payload

    def recorded_rows(self):
        """The recorded rows that fall inside the tested window."""
        start = datetime.strptime(RECORDED_START_DATE, "%Y-%m-%d").replace(tzinfo=UTC)
        end = datetime.strptime(RECORDED_END_DATE, "%Y-%m-%d").replace(tzinfo=UTC) + timedelta(
            days=1
        )
        return [
            row
            for row in RECORDED_PRICES_HISTORY["prices_history"]["history"]
            if start.timestamp() <= row["t"] < end.timestamp()
        ]

    def client_for(self, *responses):
        client = _httpx_client(responses[0] if responses else _response())
        client.get = AsyncMock(side_effect=list(responses))
        return client

    def cache_keys(self):
        start = datetime.strptime(RECORDED_START_DATE, "%Y-%m-%d").replace(tzinfo=UTC)
        end = datetime.strptime(RECORDED_END_DATE, "%Y-%m-%d").replace(tzinfo=UTC) + timedelta(
            days=1
        )
        window = f"{int(start.timestamp())}:{int(end.timestamp())}:60"
        return (
            f"price_history_token:{self.CONDITION.lower()}:Yes",
            f"price_history_token:{self.CONDITION.lower()}:No",
            f"price_history:{self.YES}:{window}",
            f"price_history:{self.NO}:{window}",
        )

    def setUp(self):
        super().setUp()
        self.clear_cache()

    def tearDown(self):
        self.clear_cache()
        super().tearDown()

    def clear_cache(self):
        for key in self.cache_keys():
            market_data_cache.delete(key)

    async def load(self, client, outcome="Yes", condition_id=None):
        from app.services.market_data.historical_prices import load_token_price_history

        with patch(
            "app.services.market_data.historical_prices.httpx.AsyncClient",
            return_value=client,
        ):
            return await load_token_price_history(
                condition_id or self.CONDITION,
                RECORDED_START_DATE,
                RECORDED_END_DATE,
                outcome=outcome,
            )

    async def test_valid_yes_token_and_clob_parameters(self):
        from app.services.market_data.historical_prices import HistoricalPriceRows

        client = self.client_for(_response(200, self.market()), _response(200, self.history()))
        result = await self.load(client)
        self.assertIsInstance(result, HistoricalPriceRows)
        self.assertEqual(len(result), len(self.recorded_rows()))
        self.assertEqual(result[0]["price"], self.recorded_rows()[0]["p"])
        self.assertEqual(result.provenance["token_id"], self.YES)
        self.assertEqual(result.provenance["outcome"], "Yes")
        self.assertEqual(result.provenance["sample_count"], len(self.recorded_rows()))
        self.assertEqual(result.provenance["quality"], "indicative_price_only")
        self.assertEqual(len(result.provenance["data_hash"]), 64)
        self.assertEqual(client.get.await_count, 2)
        self.assertTrue(
            client.get.await_args_list[0].args[0].endswith("/markets/" + self.CONDITION)
        )
        url = client.get.await_args_list[1].args[0]
        params = client.get.await_args_list[1].kwargs["params"]
        self.assertTrue(url.endswith("/prices-history"))
        self.assertEqual(params["market"], self.YES)
        self.assertEqual(params["fidelity"], 60)
        self.assertIsInstance(params["startTs"], int)
        self.assertIsInstance(params["endTs"], int)
        self.assertNotIn("startDate", params)

    async def test_recorded_response_is_ordered_and_reproducible(self):
        """The recorded YES payload returns ordered rows and a stable hash."""
        client = self.client_for(_response(200, self.market()), _response(200, self.history()))
        result = await self.load(client)

        timestamps = [row["t"] for row in result]
        self.assertEqual(timestamps, sorted(timestamps))
        self.assertTrue(all(b > a for a, b in zip(timestamps, timestamps[1:], strict=False)))

        expected = self.recorded_rows()
        self.assertEqual([row["price"] for row in result], [row["p"] for row in expected])
        self.assertEqual(
            [row["timestamp"] for row in result],
            [datetime.fromtimestamp(row["t"], tz=UTC).isoformat() for row in expected],
        )

        # The dataset hash is the identity of the series: same input, same hash.
        self.assertEqual(
            result.provenance["data_hash"],
            "1e895a0fba531f3ed5c1fbc51a3b28546da65df1b2abc6ac78bdb6a07ae04ae7",
        )

        # A second, identical read of the same payload yields the same hash.
        self.clear_cache()
        again = await self.load(
            self.client_for(_response(200, self.market()), _response(200, self.history()))
        )
        self.assertEqual(again.provenance["data_hash"], result.provenance["data_hash"])

    async def test_observations_carry_full_provenance(self):
        from app.services.market_data.historical_prices import ObservedPrice

        client = self.client_for(_response(200, self.market()), _response(200, self.history()))
        result = await self.load(client)

        self.assertEqual(len(result.observations), len(result))
        observation = result.observations[0]
        self.assertIsInstance(observation, ObservedPrice)
        self.assertEqual(observation.token_id, self.YES)
        self.assertEqual(observation.condition_id, self.CONDITION.lower())
        self.assertEqual(observation.outcome, "Yes")
        self.assertEqual(observation.source_schema_version, "polymarket_clob_prices_history_v1")
        self.assertEqual(observation.quality, "indicative_price_only")
        self.assertEqual(observation.timestamp.isoformat(), result[0]["timestamp"])
        self.assertIsNotNone(observation.available_at)

        provenance = result.provenance
        self.assertEqual(provenance["feed_version"], "prices_history_feed_v1")
        self.assertEqual(provenance["source"], "polymarket_clob_prices_history_v1")
        self.assertEqual(provenance["gap_count"], 0)
        self.assertEqual(provenance["sample_count"], len(result))
        self.assertIn("start_utc", provenance)
        self.assertIn("end_exclusive_utc", provenance)

    async def test_non_monotonic_series_is_detected_not_sorted(self):
        from app.services.market_data.historical_prices import PriceHistoryError

        rows = self.recorded_rows()
        shuffled = [rows[0], rows[2], rows[1], *rows[3:]]
        client = self.client_for(
            _response(200, self.market()),
            _response(200, {"history": shuffled}),
        )
        with self.assertRaisesRegex(PriceHistoryError, "monotonic"):
            await self.load(client)

    async def test_closed_market_before_window_fails_explicitly(self):
        from app.services.market_data.historical_prices import PriceHistoryError

        resolved = json.loads(json.dumps(RECORDED_RESOLVED_MARKET["market"]))
        self.assertTrue(resolved["closed"])
        client = self.client_for(_response(200, resolved))
        with self.assertRaisesRegex(PriceHistoryError, "closed or resolved"):
            await self.load(client, condition_id=resolved["condition_id"])

    async def test_no_token_side_mixing(self):
        client = self.client_for(_response(200, self.market()), _response(200, self.history()))
        result = await self.load(client, outcome="No")
        self.assertEqual(result.provenance["token_id"], self.NO)
        self.assertEqual(client.get.await_args_list[1].kwargs["params"]["market"], self.NO)

    async def test_mismatched_market_identity_fails_closed(self):
        from app.services.market_data.historical_prices import PriceHistoryError

        market = self.market()
        market["condition_id"] = "0x" + "b" * 64
        client = self.client_for(_response(200, market))
        with self.assertRaisesRegex(PriceHistoryError, "different"):
            await self.load(client)
        client.get.assert_awaited_once()

    async def test_unknown_or_duplicate_outcome_mapping_rejected(self):
        from app.services.market_data.historical_prices import PriceHistoryError

        for tokens in (
            [{"token_id": self.YES, "outcome": "Maybe"}],
            [
                {"token_id": self.YES, "outcome": "Yes"},
                {"token_id": self.YES, "outcome": "No"},
            ],
        ):
            market = self.market()
            market["tokens"] = tokens
            with self.subTest(tokens=tokens), self.assertRaises(PriceHistoryError):
                await self.load(self.client_for(_response(200, market)))

    async def test_malformed_json_contract_missing_p_or_nan_price_rejected(self):
        from app.services.market_data.historical_prices import PriceHistoryError

        cases = [
            [{"price": 0.4}],
            [{"t": 1704067200}],
            [{"t": 1704067200, "p": "NaN"}],
            [{"t": "not-a-timestamp", "p": 0.4}],
            [{"t": 1704067200, "p": 1.2}],
        ]
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(PriceHistoryError):
                await self.load(
                    self.client_for(
                        _response(200, self.market()), _response(200, {"history": payload})
                    )
                )

    async def test_list_payload_rejected_instead_of_silently_used(self):
        from app.services.market_data.historical_prices import PriceHistoryError

        client = self.client_for(_response(200, self.market()), _response(200, [{"p": 0.5}]))
        with self.assertRaisesRegex(PriceHistoryError, "history array"):
            await self.load(client)

    async def test_non_200_fails_closed_not_empty_success(self):
        from app.services.market_data.historical_prices import PriceHistoryError

        client = self.client_for(_response(200, self.market()), _response(404, {}))
        with self.assertRaisesRegex(PriceHistoryError, "HTTP 404"):
            await self.load(client)

    async def test_http_transport_failure_is_reported(self):
        import httpx

        from app.services.market_data.historical_prices import PriceHistoryError

        client = self.client_for(_response(200, self.market()))
        client.get.side_effect = [
            _response(200, self.market()),
            httpx.ConnectError("connection reset"),
        ]
        with self.assertRaisesRegex(PriceHistoryError, "unavailable"):
            await self.load(client)

    async def test_empty_and_sparse_history_rejected(self):
        from app.services.market_data.historical_prices import PriceHistoryError

        for payload in ({"history": []}, self.history(count=2)):
            with self.subTest(payload=payload), self.assertRaises(PriceHistoryError):
                await self.load(
                    self.client_for(_response(200, self.market()), _response(200, payload))
                )

    async def test_conflicting_timestamp_values_rejected(self):
        from app.services.market_data.historical_prices import PriceHistoryError

        rows = self.history()["history"]
        rows.append({"t": rows[0]["t"], "p": "0.99"})
        with self.assertRaisesRegex(PriceHistoryError, "Contradictory"):
            await self.load(
                self.client_for(_response(200, self.market()), _response(200, {"history": rows}))
            )

    async def test_invalid_condition_rejected_before_network(self):
        from app.services.market_data.historical_prices import (
            PriceHistoryError,
            load_token_price_history,
        )

        client = self.client_for()
        with (
            patch(
                "app.services.market_data.historical_prices.httpx.AsyncClient",
                return_value=client,
            ),
            self.assertRaisesRegex(PriceHistoryError, "Invalid Polymarket condition ID"),
        ):
            await load_token_price_history("cond-1", RECORDED_START_DATE, RECORDED_END_DATE)
        client.get.assert_not_called()

    async def test_rate_limited_upstream_fails_and_is_not_cached(self):
        """A 429 must fail the run and never become cached empty data."""
        from app.services.market_data.historical_prices import PriceHistoryError

        client = self.client_for(
            _response(200, self.market()),
            _response(429, {}),
            _response(429, {}),
            _response(429, {}),
        )
        with self.assertRaisesRegex(PriceHistoryError, "HTTP 429"):
            await self.load(client)

        for key in self.cache_keys():
            self.assertIsNone(market_data_cache.get(key), msg=f"cache key {key} must stay empty")

    async def test_repeated_query_is_served_from_cache(self):
        client = self.client_for(_response(200, self.market()), _response(200, self.history()))
        first = await self.load(client)
        calls_after_first = client.get.await_count
        self.assertEqual(calls_after_first, 2)

        # The second identical query must be served from the cache: the mock
        # only has two responses left, so any extra call would fail the test.
        second = await self.load(client)
        self.assertEqual(client.get.await_count, calls_after_first)
        self.assertEqual(len(second), len(first))
        self.assertEqual(second.provenance["data_hash"], first.provenance["data_hash"])
        self.assertEqual(second.provenance["token_id"], self.YES)

    async def test_corrupted_cache_entry_is_detected_and_recomputed(self):
        client = self.client_for(
            _response(200, self.market()),
            _response(200, self.history()),
            _response(200, self.history()),
        )
        first = await self.load(client)
        calls_after_first = client.get.await_count

        history_key = f"price_history:{self.YES}:{self.window_key()}"
        entry = market_data_cache.get(history_key)
        self.assertIsNotNone(entry)
        entry["observations"][0]["price_decimal"] = "0.99"
        market_data_cache.set(history_key, entry, ttl_seconds=900)

        recomputed = await self.load(client)
        # The corrupted entry was discarded and the dataset was fetched again.
        self.assertGreater(client.get.await_count, calls_after_first)
        self.assertEqual(recomputed[0]["price"], first[0]["price"])
        self.assertEqual(recomputed.provenance["data_hash"], first.provenance["data_hash"])

    def window_key(self):
        start = datetime.strptime(RECORDED_START_DATE, "%Y-%m-%d").replace(tzinfo=UTC)
        end = datetime.strptime(RECORDED_END_DATE, "%Y-%m-%d").replace(tzinfo=UTC) + timedelta(
            days=1
        )
        return f"{int(start.timestamp())}:{int(end.timestamp())}:60"


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
        self.assertIsNone(entry["pnl"])
        self.assertEqual(result["metrics"]["total_trades"], 0)
        self.assertEqual(result["indicator_values"]["net_pnl_quality"], "unavailable")
        self.assertIsNone(result["metrics"]["sharpe_ratio"])
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

    async def test_leader_loss_cannot_trigger_follower_loss_limit(self):
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
        self.assertEqual(len(result["trade_log"]), 2)
        self.assertTrue(all(row["pnl"] is None for row in result["trade_log"]))
        self.assertEqual(
            result["indicator_values"]["daily_loss_limit"], "unavailable_without_follower_ledger"
        )

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
        prices[45] = 110.0
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
        self.assertEqual(trade["entry_idx"], 35)
        self.assertEqual(trade["exit_idx"], 45)
        self.assertEqual(trade["fill_mode"], "next_sample_indicative")
        # pnl = (110 - 100) * (10 / 100) = 1.0
        self.assertAlmostEqual(trade["pnl"], 1.0)
        self.assertEqual(result["metrics"]["total_trades"], 1)
        self.assertEqual(result["indicator_summary"], "summary")
        self.assertEqual(result["indicator_values"]["sma"]["latest"], 105.0)
        self.assertNotIn("series", result["indicator_values"]["sma"])

    async def test_signal_cannot_fill_on_same_sample(self):
        prices = [0.4] * 60
        prices[35] = 0.6
        prices[45] = 0.2
        rsi_series = [None] * 60
        rsi_series[34] = 20.0  # BUY signal observed at 0.4
        rsi_series[44] = 80.0  # SELL signal observed at 0.4
        result = await self._run({"condition_id": "c1", "_rsi": rsi_series}, _price_data(prices))
        trade = result["trade_log"][0]
        self.assertEqual(trade["entry_price"], 0.6)
        self.assertEqual(trade["exit_price"], 0.2)
        self.assertEqual(trade["entry_idx"], 35)
        self.assertEqual(trade["exit_idx"], 45)
        self.assertLess(trade["pnl"], 0)
        self.assertIsNone(result["metrics"]["sharpe_ratio"])
        self.assertEqual(result["metrics"]["total_volume"], 10.0)

    async def test_invalid_position_size_is_rejected(self):
        result = await self._run(
            {"condition_id": "c1", "position_size": 0}, _price_data([0.4] * 60)
        )
        self.assertIn("position_size must be positive", result["error"])
        self.assertEqual(result["trade_log"], [])

    async def test_last_bar_signal_cannot_generate_future_fill(self):
        prices = [0.4] * 60
        rsi_series = [None] * 60
        rsi_series[59] = 20.0
        result = await self._run({"condition_id": "c1", "_rsi": rsi_series}, _price_data(prices))
        self.assertEqual(result["trade_log"], [])
        self.assertIsNone(result["indicator_values"]["unclosed_position"])

    async def test_rsi_open_position_closed_at_last_price(self):
        prices = [100.0] * 60
        rsi_series = [None] * 60
        rsi_series[34] = 25.0  # BUY, never sold
        result = await self._run(
            {"condition_id": "c1", "strategy": "rsi_mean_reversion", "_rsi": rsi_series},
            _price_data(prices),
        )
        self.assertEqual(result["trade_log"], [])
        self.assertEqual(result["indicator_values"]["unrealized_pnl_indicative"], 0.0)
        self.assertEqual(result["indicator_values"]["unclosed_position"]["entry_idx"], 35)

    async def test_rsi_buy_requires_no_position(self):
        # Two oversold readings in a row: only the first opens
        # a position. The remaining inventory is NOT treated as sold.
        prices = [100.0] * 60
        rsi_series = [None] * 60
        rsi_series[34] = 25.0
        rsi_series[35] = 20.0
        result = await self._run(
            {"condition_id": "c1", "strategy": "rsi_mean_reversion", "_rsi": rsi_series},
            _price_data(prices),
        )
        self.assertEqual(result["trade_log"], [])
        self.assertEqual(result["indicator_values"]["unclosed_position"]["entry_price"], 100.0)

    async def test_macd_crossover_buy_and_sell(self):
        prices = [100.0] * 60
        prices[44] = 110.0
        prices[45] = 110.0
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
        prices[45] = 110.0
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

    async def test_unknown_strategy_fails_explicitly(self):
        result = await self._run(
            {"condition_id": "c1", "strategy": "unknown"}, _price_data([100.0] * 60)
        )
        self.assertEqual(result["trade_log"], [])
        self.assertEqual(result["metrics"]["total_trades"], 0)
        self.assertIn("Unsupported indicator strategy", result["error"])

    async def test_mid_without_price_is_rejected(self):
        prices = [100.0] * 60
        price_data = [{"mid": p, "t": f"t{i}"} for i, p in enumerate(prices)]
        result = await self._run({"condition_id": "c1"}, price_data)
        self.assertEqual(result["trade_log"], [])
        self.assertIn("price missing", result["error"])

    async def test_none_rows_cannot_be_silently_skipped(self):
        prices = [100.0] * 30
        price_data = [None] + _price_data(prices)
        result = await self._run({"condition_id": "c1"}, price_data)
        self.assertEqual(result["metrics"]["total_trades"], 0)
        self.assertIn("price missing", result["error"])

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
        # Position stays open; the last sampled mark is unrealized.
        self.assertEqual(result["trade_log"], [])
        self.assertEqual(result["indicator_values"]["unclosed_position"]["shares"], 0.05)

    async def test_settled_zero_price_is_not_rejected(self):
        """A market that settled at 0.0 is a valid price, not a malformed row."""
        prices = [100.0] * 60
        prices[40] = 0.0
        result = await self._run({"condition_id": "c1"}, _price_data(prices))
        self.assertNotIn("error", result)
        self.assertEqual(result["metrics"]["total_trades"], 0)

    async def test_negative_price_is_still_rejected(self):
        result = await self._run({"condition_id": "c1"}, _price_data([-0.1] + [100.0] * 59))
        self.assertEqual(result["trade_log"], [])
        self.assertIn("invalid price", result["error"])
        self.assertEqual(result["indicator_values"]["history_quality"], "invalid_source")


# ── History failure provenance ────────────────────────────────


class HistoryFailureProvenanceTests(unittest.IsolatedAsyncioTestCase):
    """A failed history read stays attributable and never looks successful."""

    async def _run_with_failure(self, reason: str, message: str, context=None):
        from app.services.market_data.historical_prices import PriceHistoryError

        failure = PriceHistoryError(message, reason=reason, context=context)
        fetch = AsyncMock(side_effect=failure)
        with patch.object(backtesting_service, "_fetch_historical_prices", fetch):
            return await run_indicator_backtest(
                1,
                {
                    "condition_id": "0x" + "a" * 64,
                    "start_date": RECORDED_START_DATE,
                    "end_date": RECORDED_END_DATE,
                },
            )

    async def test_upstream_failure_keeps_provenance_and_names_the_cause(self):
        result = await self._run_with_failure(
            "upstream_unavailable",
            "Polymarket historical market data unavailable",
            context={
                "token_id": "12345678901234567890",
                "provenance": {"fetched_at": "2026-10-21T10:00:00+00:00"},
            },
        )
        self.assertEqual(result["trade_log"], [])
        self.assertIsNone(result["metrics"]["sharpe_ratio"])

        indicator_values = result["indicator_values"]
        self.assertEqual(indicator_values["history_quality"], "upstream_unavailable")
        self.assertEqual(
            indicator_values["error_reason"], "Polymarket historical market data unavailable"
        )

        provenance = indicator_values["history_source"]
        self.assertEqual(provenance["token_id"], "12345678901234567890")
        self.assertEqual(provenance["source"], "polymarket_clob_prices_history_v1")
        self.assertEqual(provenance["feed_version"], "prices_history_feed_v1")
        self.assertEqual(provenance["start_utc"], "2026-10-16T00:00:00+00:00")
        self.assertEqual(provenance["end_exclusive_utc"], "2026-10-20T00:00:00+00:00")
        self.assertEqual(provenance["fetched_at"], "2026-10-21T10:00:00+00:00")
        self.assertEqual(provenance["error_kind"], "upstream_unavailable")
        self.assertEqual(
            provenance["error_reason"], "Polymarket historical market data unavailable"
        )

    async def test_upstream_failure_is_not_labelled_insufficient_coverage(self):
        upstream = await self._run_with_failure(
            "upstream_unavailable", "Polymarket historical market data unavailable"
        )
        sparse = await self._run_with_failure(
            "insufficient_coverage", "Insufficient historical price coverage"
        )
        self.assertEqual(upstream["indicator_values"]["history_quality"], "upstream_unavailable")
        self.assertEqual(sparse["indicator_values"]["history_quality"], "insufficient_coverage")
        self.assertNotEqual(
            upstream["indicator_values"]["history_quality"],
            sparse["indicator_values"]["history_quality"],
        )


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
        self.assertEqual(run.status, "failed")
        self.assertIn("Unknown strategy type: inverse_bot", run.error_message)

    async def test_history_failure_marks_run_failed_without_sharpe(self):
        """A failed history read is a failed run, never a zero-return success."""
        from app.services.market_data.historical_prices import PriceHistoryError

        db = _run_db(run=MagicMock(name="run"))
        fetch = AsyncMock(
            side_effect=PriceHistoryError(
                "Polymarket history HTTP 429", reason="upstream_http_error"
            )
        )
        with (
            patch.object(backtesting_service, "SessionLocal", return_value=db),
            patch.object(backtesting_service, "BacktestRun") as run_cls,
            patch.object(backtesting_service, "_fetch_historical_prices", fetch),
        ):
            run_cls.return_value.id = 42
            run = await create_and_run_backtest(
                1,
                "indicator",
                "history failure",
                RECORDED_START_DATE,
                RECORDED_END_DATE,
                {"condition_id": "0x" + "a" * 64},
            )
        self.assertEqual(run.status, "failed")
        self.assertIsNone(run.sharpe_ratio)
        self.assertIsNone(run.profit_factor)
        self.assertEqual(run.total_trades, 0)
        self.assertIn("HTTP 429", run.error_message)
        self.assertEqual(run.indicator_values["history_quality"], "upstream_unavailable")

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
