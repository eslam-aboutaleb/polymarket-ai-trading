"""Coverage tests for :mod:`app.services.crypto_markets_service`.

The discovery service is a pure parser (``parse_crypto_markets``) plus a
Gamma-API fetcher, a runtime mapping with staleness tracking and a
scheduler-locked refresh lifecycle. These tests drive every branch of the
parser with synthetic Gamma payloads against a frozen clock, mock the
httpx transport for ``_fetch_gamma_events``, and exercise the start/stop
lifecycle of the background refresh task (including the stubborn-task
shutdown path).

Module-level runtime state (``_markets``, ``_last_refresh_at``,
``_running``, ``_refresh_task``, ``_stop_event``) is saved and restored
around every test so the suite never leaks state into other modules.
"""

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import app.services.crypto_markets_service as crypto_markets_service
from app.services.crypto_markets_service import (
    CryptoMarket,
    current_window_start,
    parse_crypto_markets,
    window_start_matches,
)

# Frozen clock: 2026-01-01T12:00:00Z. Every window boundary below is
# computed relative to this moment so the retention window is deterministic.
NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

CID = "0x" + "ab" * 32


def _iso(minutes_offset: float) -> str:
    return (NOW + timedelta(minutes=minutes_offset)).isoformat()


def _market(**overrides) -> dict:
    market = {
        "conditionId": CID,
        "tokens": [
            {"token_id": "up-token", "outcome": "Up"},
            {"token_id": "down-token", "outcome": "Down"},
        ],
        "startDate": _iso(0),
        "endDate": _iso(5),
    }
    market.update(overrides)
    return market


def _event(title: str, markets: list | None = None, **overrides) -> dict:
    event = {
        "title": title,
        "markets": markets if markets is not None else [_market()],
        "startDate": _iso(0),
        "endDate": _iso(5),
    }
    event.update(overrides)
    return event


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload=None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """httpx.AsyncClient double that records GET calls."""

    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.get_calls: list[tuple[str, dict]] = []

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False

    async def get(self, url, params=None):
        self.get_calls.append((url, params))
        if self.error is not None:
            raise self.error
        return self.response


class _StateIsolatedTestCase(unittest.IsolatedAsyncioTestCase):
    """Save/restore the module's runtime state around each test."""

    def setUp(self) -> None:
        self._saved_state = (
            crypto_markets_service._markets,
            crypto_markets_service._last_refresh_at,
            crypto_markets_service._refresh_task,
            crypto_markets_service._stop_event,
            crypto_markets_service._running,
        )
        crypto_markets_service._markets = {}
        crypto_markets_service._last_refresh_at = 0.0
        crypto_markets_service._refresh_task = None
        crypto_markets_service._stop_event = None
        crypto_markets_service._running = False

    def tearDown(self) -> None:
        (
            crypto_markets_service._markets,
            crypto_markets_service._last_refresh_at,
            crypto_markets_service._refresh_task,
            crypto_markets_service._stop_event,
            crypto_markets_service._running,
        ) = self._saved_state


# ─────────────── dataclass helpers ───────────────


class CryptoMarketHelpersTests(unittest.TestCase):
    def test_token_id_properties_read_the_mapping(self):
        market = CryptoMarket(
            symbol="BTC",
            window_minutes=5,
            window_start_epoch=0,
            condition_id=CID,
            token_ids={"up": "u", "down": "d"},
        )
        self.assertEqual(market.up_token_id, "u")
        self.assertEqual(market.down_token_id, "d")

    def test_token_id_properties_default_to_empty_string(self):
        market = CryptoMarket("BTC", 5, 0, CID)
        self.assertEqual(market.up_token_id, "")
        self.assertEqual(market.down_token_id, "")

    def test_price_properties_default_to_half(self):
        market = CryptoMarket("BTC", 5, 0, CID)
        self.assertEqual(market.up_price, 0.5)
        self.assertEqual(market.down_price, 0.5)

    def test_price_properties_read_the_mapping(self):
        market = CryptoMarket("BTC", 5, 0, CID, prices={"up": 0.9, "down": 0.1})
        self.assertEqual(market.up_price, 0.9)
        self.assertEqual(market.down_price, 0.1)

    def test_to_dict_snapshot(self):
        market = CryptoMarket(
            "BTC",
            5,
            123,
            CID,
            token_ids={"up": "u"},
            prices={"up": 0.9},
            question="Bitcoin 5m Up or Down",
        )
        snapshot = market.to_dict()
        self.assertEqual(
            snapshot,
            {
                "symbol": "BTC",
                "window_minutes": 5,
                "window_start_epoch": 123,
                "condition_id": CID,
                "token_ids": {"up": "u"},
                "prices": {"up": 0.9},
                "question": "Bitcoin 5m Up or Down",
            },
        )
        # The snapshot copies the mutable mappings.
        snapshot["token_ids"]["up"] = "mutated"
        self.assertEqual(market.up_token_id, "u")


# ─────────────── window maths ───────────────


class WindowMathTests(unittest.TestCase):
    def test_current_window_start_snaps_to_the_boundary(self):
        # 12:00:00Z is exactly on a 5m boundary.
        self.assertEqual(current_window_start(5, NOW), int(NOW.timestamp()))
        # 12:02:30Z still sits inside the 12:00 window.
        moment = NOW + timedelta(minutes=2, seconds=30)
        self.assertEqual(current_window_start(5, moment), int(NOW.timestamp()))
        # 12:05:00Z opens the next window.
        moment = NOW + timedelta(minutes=5)
        self.assertEqual(current_window_start(5, moment), int(NOW.timestamp()) + 300)

    def test_current_window_start_defaults_to_now(self):
        result = current_window_start(15)
        self.assertEqual(result % 900, 0)

    def test_window_start_matches_validates_against_the_clock(self):
        start = current_window_start(5, NOW)
        market = CryptoMarket("BTC", 5, start, CID)
        self.assertTrue(window_start_matches(market, 5, NOW))
        self.assertFalse(window_start_matches(market, 5, NOW + timedelta(minutes=5)))
        # 12:00Z is also a 15m boundary, so the 15m check must
        # use a moment outside the 12:00 15m window.
        self.assertFalse(window_start_matches(market, 15, NOW + timedelta(minutes=15)))
        # A market from the previous 5m window never matches.
        previous = CryptoMarket("BTC", 5, start - 300, CID)
        self.assertFalse(window_start_matches(previous, 5, NOW))


# ─────────────── low-level parsers ───────────────


class ParseIsoDatetimeTests(unittest.TestCase):
    def test_none_and_non_string_return_none(self):
        self.assertIsNone(crypto_markets_service._parse_iso_datetime(None))
        self.assertIsNone(crypto_markets_service._parse_iso_datetime(123))
        self.assertIsNone(crypto_markets_service._parse_iso_datetime(""))

    def test_invalid_string_returns_none(self):
        self.assertIsNone(crypto_markets_service._parse_iso_datetime("not-a-date"))

    def test_aware_string_is_parsed(self):
        parsed = crypto_markets_service._parse_iso_datetime("2026-01-01T12:00:00+00:00")
        self.assertEqual(parsed, datetime(2026, 1, 1, 12, 0, tzinfo=UTC))

    def test_z_suffix_is_normalised(self):
        parsed = crypto_markets_service._parse_iso_datetime("2026-01-01T12:00:00Z")
        self.assertEqual(parsed, datetime(2026, 1, 1, 12, 0, tzinfo=UTC))

    def test_naive_string_is_assumed_utc(self):
        parsed = crypto_markets_service._parse_iso_datetime("2026-01-01T12:00:00")
        self.assertEqual(parsed, datetime(2026, 1, 1, 12, 0, tzinfo=UTC))


class MarketTitleTests(unittest.TestCase):
    def test_event_title_wins(self):
        self.assertEqual(
            crypto_markets_service._market_title(
                {"title": "Event T", "question": "Event Q"},
                {"question": "Market Q", "title": "Market T"},
            ),
            "Event T",
        )

    def test_event_question_when_no_title(self):
        self.assertEqual(
            crypto_markets_service._market_title({"question": "Event Q"}, {"title": "Market T"}),
            "Event Q",
        )

    def test_market_question_when_event_has_neither(self):
        self.assertEqual(
            crypto_markets_service._market_title({}, {"question": "Market Q", "title": "Market T"}),
            "Market Q",
        )

    def test_market_group_item_title(self):
        self.assertEqual(
            crypto_markets_service._market_title({}, {"groupItemTitle": "GIT"}),
            "GIT",
        )

    def test_market_title_last_resort(self):
        self.assertEqual(
            crypto_markets_service._market_title({}, {"title": "Market T"}),
            "Market T",
        )

    def test_blank_strings_are_skipped(self):
        self.assertEqual(
            crypto_markets_service._market_title({"title": "  "}, {"question": "  "}),
            "",
        )

    def test_no_title_anywhere(self):
        self.assertEqual(crypto_markets_service._market_title({}, {}), "")


class ExtractTokensTests(unittest.TestCase):
    def test_snake_and_camel_case_token_ids(self):
        tokens = crypto_markets_service._extract_tokens(
            {
                "tokens": [
                    {"token_id": "up-snake", "outcome": "up"},
                    {"tokenId": "down-camel", "outcome": "down"},
                ]
            }
        )
        self.assertEqual(tokens, {"up": "up-snake", "down": "down-camel"})

    def test_outcome_aliases_and_case(self):
        tokens = crypto_markets_service._extract_tokens(
            {
                "tokens": [
                    {"token_id": "a", "outcome": "YES"},
                    {"token_id": "b", "outcome": "No"},
                ]
            }
        )
        self.assertEqual(tokens, {"up": "a", "down": "b"})

    def test_non_dict_tokens_are_skipped(self):
        self.assertEqual(crypto_markets_service._extract_tokens({"tokens": ["nope", 5]}), {})

    def test_tokens_without_an_id_are_skipped(self):
        self.assertEqual(
            crypto_markets_service._extract_tokens(
                {"tokens": [{"outcome": "up"}, {"token_id": "", "outcome": "down"}]}
            ),
            {},
        )

    def test_unrelated_outcomes_are_ignored(self):
        self.assertEqual(
            crypto_markets_service._extract_tokens(
                {"tokens": [{"token_id": "x", "outcome": "maybe"}]}
            ),
            {},
        )

    def test_missing_tokens_key(self):
        self.assertEqual(crypto_markets_service._extract_tokens({}), {})


class ExtractPricesTests(unittest.TestCase):
    def test_outcome_prices_list_is_preferred(self):
        prices = crypto_markets_service._extract_prices({"outcomePrices": ["0.9", "0.1"]})
        self.assertEqual(prices, {"up": 0.9, "down": 0.1})

    def test_short_or_invalid_outcome_prices_fall_back_to_tokens(self):
        prices = crypto_markets_service._extract_prices(
            {
                "outcomePrices": ["not-a-number", "also-not"],
                "tokens": [
                    {"token_id": "u", "outcome": "up", "price": "0.7"},
                    {"token_id": "d", "outcome": "down", "price": "0.3"},
                ],
            }
        )
        self.assertEqual(prices, {"up": 0.7, "down": 0.3})

    def test_single_element_outcome_prices_fall_back(self):
        prices = crypto_markets_service._extract_prices(
            {
                "outcomePrices": [0.9],
                "tokens": [{"token_id": "u", "outcome": "up", "price": 0.6}],
            }
        )
        self.assertEqual(prices, {"up": 0.6})

    def test_token_prices_with_invalid_values_are_skipped(self):
        prices = crypto_markets_service._extract_prices(
            {
                "tokens": [
                    {"token_id": "u", "outcome": "up", "price": "bad"},
                    {"token_id": "d", "outcome": "down"},
                ]
            }
        )
        self.assertEqual(prices, {})

    def test_non_dict_tokens_are_skipped(self):
        self.assertEqual(crypto_markets_service._extract_prices({"tokens": [1, "x"]}), {})

    def test_no_price_information(self):
        self.assertEqual(crypto_markets_service._extract_prices({}), {})


class AlignWindowStartTests(unittest.TestCase):
    def test_snaps_down_to_the_window_boundary(self):
        # 12:02:30 -> 12:00:00 for a 5m window.
        epoch = int(NOW.timestamp()) + 150
        self.assertEqual(crypto_markets_service._align_window_start(epoch, 5), int(NOW.timestamp()))

    def test_exact_boundary_is_unchanged(self):
        epoch = int(NOW.timestamp())
        self.assertEqual(crypto_markets_service._align_window_start(epoch, 5), epoch)


# ─────────────── the market parser ───────────────


class ParseCryptoMarketsTests(unittest.TestCase):
    def test_btc_5m_market_is_mapped(self):
        markets = parse_crypto_markets([_event("Bitcoin 5m Up or Down")], now=NOW)
        self.assertEqual(len(markets), 1)
        market = markets[("BTC", 5, int(NOW.timestamp()))]
        self.assertEqual(market.symbol, "BTC")
        self.assertEqual(market.window_minutes, 5)
        self.assertEqual(market.condition_id, CID)
        self.assertEqual(market.up_token_id, "up-token")
        self.assertEqual(market.down_token_id, "down-token")
        self.assertEqual(market.question, "Bitcoin 5m Up or Down")
        self.assertIsNotNone(market.start_date)
        self.assertIsNotNone(market.end_date)

    def test_eth_15m_and_sol_1h_titles(self):
        events = [
            _event("Ethereum 15m Up/Down", [_market(startDate=_iso(0))]),
            _event("Solana 1h Direction", [_market(startDate=_iso(0))]),
        ]
        markets = parse_crypto_markets(events, now=NOW)
        self.assertIn(("ETH", 15, int(NOW.timestamp())), markets)
        self.assertIn(("SOL", 60, int(NOW.timestamp())), markets)

    def test_window_variants_in_titles(self):
        cases = [
            ("BTC 5m up or down", "BTC", 5),
            ("BTC 5 min up or down", "BTC", 5),
            ("BTC 5 minutes up or down", "BTC", 5),
            ("BTC five minutes up or down", "BTC", 5),
            ("ETH 15m up or down", "ETH", 15),
            ("ETH 15 minutes up or down", "ETH", 15),
            ("ETH fifteen minutes up or down", "ETH", 15),
            ("SOL 1h up or down", "SOL", 60),
            ("SOL 1 hour up or down", "SOL", 60),
            ("SOL 60 minutes up or down", "SOL", 60),
            ("SOL hourly direction", "SOL", 60),
        ]
        for title, symbol, window in cases:
            with self.subTest(title=title):
                markets = parse_crypto_markets(
                    [_event(title, [_market(startDate=_iso(0))])], now=NOW
                )
                self.assertEqual(list(markets), [(symbol, window, int(NOW.timestamp()))])

    def test_non_dict_events_are_skipped(self):
        self.assertEqual(parse_crypto_markets(["nope", 5], now=NOW), {})

    def test_event_without_a_market_list_is_skipped(self):
        self.assertEqual(parse_crypto_markets([{"title": "No markets"}], now=NOW), {})
        self.assertEqual(
            parse_crypto_markets([{"title": "Bad markets", "markets": "x"}], now=NOW),
            {},
        )

    def test_non_dict_markets_are_skipped(self):
        self.assertEqual(
            parse_crypto_markets([_event("Bitcoin 5m Up or Down", markets=["nope"])], now=NOW),
            {},
        )

    def test_title_without_direction_is_skipped(self):
        self.assertEqual(
            parse_crypto_markets([_event("Bitcoin 5m", [_market()])], now=NOW),
            {},
        )

    def test_title_without_a_symbol_is_skipped(self):
        self.assertEqual(
            parse_crypto_markets([_event("Crude Oil 5m Up or Down", [_market()])], now=NOW),
            {},
        )

    def test_title_without_a_window_is_skipped(self):
        self.assertEqual(
            parse_crypto_markets([_event("Bitcoin Up or Down", [_market()])], now=NOW),
            {},
        )

    def test_market_without_a_condition_id_is_skipped(self):
        self.assertEqual(
            parse_crypto_markets(
                [_event("Bitcoin 5m Up or Down", [_market(conditionId="")])], now=NOW
            ),
            {},
        )

    def test_market_without_tokens_is_skipped(self):
        self.assertEqual(
            parse_crypto_markets([_event("Bitcoin 5m Up or Down", [_market(tokens=[])])], now=NOW),
            {},
        )

    def test_market_without_a_start_date_is_skipped(self):
        # Neither the market nor the event carries a start date.
        event = _event(
            "Bitcoin 5m Up or Down",
            [_market(startDate=None)],
            startDate=None,
        )
        self.assertEqual(parse_crypto_markets([event], now=NOW), {})

    def test_event_level_start_and_end_dates_are_used(self):
        event = _event("Bitcoin 5m Up or Down", [_market(startDate=None, endDate=None)])
        markets = parse_crypto_markets([event], now=NOW)
        self.assertEqual(len(markets), 1)
        market = next(iter(markets.values()))
        self.assertEqual(market.start_date, NOW)
        self.assertEqual(market.end_date, NOW + timedelta(minutes=5))

    def test_invalid_end_date_is_tolerated(self):
        markets = parse_crypto_markets(
            [_event("Bitcoin 5m Up or Down", [_market(endDate="garbage")])], now=NOW
        )
        self.assertEqual(len(markets), 1)
        self.assertIsNone(next(iter(markets.values())).end_date)

    def test_stale_window_is_dropped(self):
        # 11:50Z aligns to current_start - 600s: outside the retention window.
        markets = parse_crypto_markets(
            [_event("Bitcoin 5m Up or Down", [_market(startDate=_iso(-10))])], now=NOW
        )
        self.assertEqual(markets, {})

    def test_previous_window_is_retained(self):
        # 11:57Z aligns to current_start - 300s: the previous window.
        markets = parse_crypto_markets(
            [_event("Bitcoin 5m Up or Down", [_market(startDate=_iso(-3))])], now=NOW
        )
        self.assertEqual(list(markets), [("BTC", 5, int(NOW.timestamp()) - 300)])

    def test_next_window_is_retained(self):
        # 12:07Z aligns to current_start + 300s: the next window.
        markets = parse_crypto_markets(
            [_event("Bitcoin 5m Up or Down", [_market(startDate=_iso(7))])], now=NOW
        )
        self.assertEqual(list(markets), [("BTC", 5, int(NOW.timestamp()) + 300)])

    def test_window_too_far_in_the_future_is_dropped(self):
        # 12:17Z aligns to current_start + 900s: beyond the retention window.
        markets = parse_crypto_markets(
            [_event("Bitcoin 5m Up or Down", [_market(startDate=_iso(17))])], now=NOW
        )
        self.assertEqual(markets, {})

    def test_later_event_overwrites_the_same_key(self):
        events = [
            _event("Bitcoin 5m Up or Down", [_market(conditionId=CID)]),
            _event(
                "Bitcoin 5m Up or Down",
                [_market(conditionId="0x" + "cd" * 32)],
            ),
        ]
        markets = parse_crypto_markets(events, now=NOW)
        self.assertEqual(len(markets), 1)
        self.assertEqual(next(iter(markets.values())).condition_id, "0x" + "cd" * 32)

    def test_clock_noise_is_aligned_to_the_boundary(self):
        # A start 90 seconds into the window still maps to the window start.
        markets = parse_crypto_markets(
            [_event("Bitcoin 5m Up or Down", [_market(startDate=_iso(1.5))])], now=NOW
        )
        self.assertEqual(list(markets), [("BTC", 5, int(NOW.timestamp()))])


# ─────────────── Gamma fetch ───────────────


class FetchGammaEventsTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_fetch_returns_the_event_list(self):
        payload = [{"title": "Bitcoin 5m Up or Down"}]
        client = _FakeAsyncClient(_FakeResponse(payload=payload))
        with patch.object(crypto_markets_service.httpx, "AsyncClient", lambda **kw: client):
            events = await crypto_markets_service._fetch_gamma_events()
        self.assertEqual(events, payload)
        url, params = client.get_calls[0]
        self.assertEqual(url, f"{crypto_markets_service.POLYMARKET_GAMMA_API}/events")
        self.assertEqual(
            params,
            {
                "limit": crypto_markets_service.FETCH_LIMIT,
                "active": True,
                "closed": False,
                "order": "startDate",
                "ascending": False,
            },
        )

    async def test_non_200_response_returns_an_empty_list(self):
        client = _FakeAsyncClient(_FakeResponse(status_code=503))
        with patch.object(crypto_markets_service.httpx, "AsyncClient", lambda **kw: client):
            self.assertEqual(await crypto_markets_service._fetch_gamma_events(), [])

    async def test_non_list_json_returns_an_empty_list(self):
        client = _FakeAsyncClient(_FakeResponse(payload={"events": []}))
        with patch.object(crypto_markets_service.httpx, "AsyncClient", lambda **kw: client):
            self.assertEqual(await crypto_markets_service._fetch_gamma_events(), [])

    async def test_transport_error_propagates(self):
        client = _FakeAsyncClient(error=RuntimeError("dns failure"))
        with (
            patch.object(crypto_markets_service.httpx, "AsyncClient", lambda **kw: client),
            self.assertRaises(RuntimeError),
        ):
            await crypto_markets_service._fetch_gamma_events()


class RefreshMarketsTests(_StateIsolatedTestCase):
    def _live_event(self, title: str = "Bitcoin 5m Up or Down") -> dict:
        """An event whose window is current relative to the real clock."""
        now = datetime.now(UTC)
        market = _market(
            startDate=now.isoformat(),
            endDate=(now + timedelta(minutes=5)).isoformat(),
        )
        return {
            "title": title,
            "markets": [market],
            "startDate": now.isoformat(),
        }

    async def test_refresh_rebuilds_the_mapping(self):
        events = [self._live_event()]
        with patch.object(
            crypto_markets_service,
            "_fetch_gamma_events",
            new=AsyncMock(return_value=events),
        ) as fetch:
            count = await crypto_markets_service.refresh_markets()
        fetch.assert_awaited_once_with()
        self.assertEqual(count, 1)
        start = current_window_start(5)
        self.assertIn(("BTC", 5, start), crypto_markets_service._markets)
        self.assertGreater(crypto_markets_service._last_refresh_at, 0.0)

    async def test_refresh_failure_keeps_the_previous_mapping(self):
        crypto_markets_service._markets = {
            ("BTC", 5, int(NOW.timestamp())): CryptoMarket("BTC", 5, int(NOW.timestamp()), CID)
        }
        with (
            patch.object(
                crypto_markets_service,
                "_fetch_gamma_events",
                new=AsyncMock(side_effect=RuntimeError("gamma down")),
            ),
            self.assertLogs(crypto_markets_service.logger, level="WARNING"),
        ):
            count = await crypto_markets_service.refresh_markets()
        self.assertEqual(count, 1)
        self.assertEqual(len(crypto_markets_service._markets), 1)

    async def test_refresh_with_no_markets_clears_the_mapping(self):
        crypto_markets_service._markets = {
            ("BTC", 5, int(NOW.timestamp())): CryptoMarket("BTC", 5, int(NOW.timestamp()), CID)
        }
        with patch.object(
            crypto_markets_service,
            "_fetch_gamma_events",
            new=AsyncMock(return_value=[]),
        ):
            count = await crypto_markets_service.refresh_markets()
        self.assertEqual(count, 0)
        self.assertEqual(crypto_markets_service._markets, {})


# ─────────────── lookups ───────────────


class LookupTests(_StateIsolatedTestCase):
    def _seed(self) -> None:
        start = current_window_start(5, NOW)
        crypto_markets_service._markets = {
            ("BTC", 5, start): CryptoMarket("BTC", 5, start, CID),
            ("ETH", 15, start): CryptoMarket("ETH", 15, start, CID),
        }

    def test_get_market_with_an_explicit_window_start(self):
        self._seed()
        start = current_window_start(5, NOW)
        self.assertIsNotNone(crypto_markets_service.get_market("BTC", 5, start))
        self.assertIsNone(crypto_markets_service.get_market("BTC", 5, start + 300))

    def test_get_market_defaults_to_the_current_window(self):
        self._seed()
        with patch.object(
            crypto_markets_service,
            "current_window_start",
            return_value=current_window_start(5, NOW),
        ):
            market = crypto_markets_service.get_market("btc", 5)
        self.assertIsNotNone(market)
        self.assertEqual(market.symbol, "BTC")

    def test_get_market_returns_none_for_unknown_keys(self):
        self._seed()
        self.assertIsNone(crypto_markets_service.get_market("SOL", 5))

    def test_get_active_markets_returns_current_windows_only(self):
        self._seed()
        active = crypto_markets_service.get_active_markets(now=NOW)
        self.assertEqual(
            sorted((m.symbol, m.window_minutes) for m in active),
            [("BTC", 5), ("ETH", 15)],
        )

    def test_get_active_markets_with_no_markets(self):
        self.assertEqual(crypto_markets_service.get_active_markets(now=NOW), [])

    def test_markets_stale_before_any_refresh(self):
        self.assertTrue(crypto_markets_service.markets_stale())

    def test_markets_stale_after_a_fresh_refresh(self):
        crypto_markets_service._last_refresh_at = datetime.now(UTC).timestamp()
        self.assertFalse(crypto_markets_service.markets_stale())

    def test_markets_stale_after_an_old_refresh(self):
        crypto_markets_service._last_refresh_at = datetime.now(UTC).timestamp() - 3600
        self.assertTrue(crypto_markets_service.markets_stale())

    def test_markets_stale_honours_a_custom_max_age(self):
        crypto_markets_service._last_refresh_at = datetime.now(UTC).timestamp() - 10
        self.assertFalse(crypto_markets_service.markets_stale(max_age_seconds=60))
        self.assertTrue(crypto_markets_service.markets_stale(max_age_seconds=5))


# ─────────────── refresh loop ───────────────


class PeriodicRefreshTests(_StateIsolatedTestCase):
    async def test_cycle_runs_refresh_and_heartbeat_then_waits(self):
        stop_event = asyncio.Event()
        refresh = AsyncMock(return_value=0)
        heartbeat = MagicMock()
        wait_calls: list[asyncio.Future] = []

        async def _fake_wait_for(coro, timeout=None):
            wait_calls.append(coro)
            coro.close()
            stop_event.set()
            return

        with (
            patch.object(crypto_markets_service, "refresh_markets", new=refresh),
            patch.object(crypto_markets_service, "scheduler_heartbeat", new=heartbeat),
            patch.object(crypto_markets_service.asyncio, "wait_for", _fake_wait_for),
        ):
            await crypto_markets_service._periodic_refresh(stop_event)

        refresh.assert_awaited_once()
        heartbeat.assert_called_once_with("latency_arb")
        self.assertEqual(len(wait_calls), 1)

    async def test_timeout_in_the_wait_is_suppressed_and_the_loop_continues(self):
        stop_event = asyncio.Event()
        refresh = AsyncMock(return_value=0)
        heartbeat = MagicMock()
        waits = 0

        async def _fake_wait_for(coro, timeout=None):
            nonlocal waits
            coro.close()
            waits += 1
            if waits == 1:
                # First wait times out: the suppression lets the
                # loop cycle a second time.
                raise TimeoutError
            stop_event.set()
            return

        with (
            patch.object(crypto_markets_service, "refresh_markets", new=refresh),
            patch.object(crypto_markets_service, "scheduler_heartbeat", new=heartbeat),
            patch.object(crypto_markets_service.asyncio, "wait_for", _fake_wait_for),
        ):
            await crypto_markets_service._periodic_refresh(stop_event)

        # The first wait timed out (suppressed), the loop cycled once more
        # and the second wait observed the stop event.
        self.assertEqual(refresh.await_count, 2)
        self.assertEqual(heartbeat.call_count, 2)
        self.assertEqual(waits, 2)


# ─────────────── lifecycle ───────────────


class LifecycleTests(_StateIsolatedTestCase):
    async def test_start_creates_the_refresh_task(self):
        with (
            patch.object(
                crypto_markets_service,
                "refresh_markets",
                new=AsyncMock(return_value=0),
            ),
            patch.object(crypto_markets_service, "scheduler_heartbeat", MagicMock()),
        ):
            await crypto_markets_service.start_crypto_markets_refresh()
            self.assertTrue(crypto_markets_service._running)
            self.assertIsInstance(crypto_markets_service._refresh_task, asyncio.Task)
            await crypto_markets_service.stop_crypto_markets_refresh()

        self.assertFalse(crypto_markets_service._running)
        self.assertIsNone(crypto_markets_service._refresh_task)
        self.assertIsNone(crypto_markets_service._stop_event)

    async def test_second_start_while_running_is_a_no_op(self):
        with (
            patch.object(
                crypto_markets_service,
                "refresh_markets",
                new=AsyncMock(return_value=0),
            ),
            patch.object(crypto_markets_service, "scheduler_heartbeat", MagicMock()),
        ):
            await crypto_markets_service.start_crypto_markets_refresh()
            first_task = crypto_markets_service._refresh_task
            await crypto_markets_service.start_crypto_markets_refresh()
            self.assertIs(crypto_markets_service._refresh_task, first_task)
            await crypto_markets_service.stop_crypto_markets_refresh()

    async def test_stop_without_a_running_loop_is_a_no_op(self):
        await crypto_markets_service.stop_crypto_markets_refresh()
        self.assertFalse(crypto_markets_service._running)
        self.assertIsNone(crypto_markets_service._refresh_task)

    async def test_stop_after_the_task_finished_clears_state(self):
        finished = asyncio.create_task(asyncio.sleep(0))
        await finished
        crypto_markets_service._running = True
        crypto_markets_service._refresh_task = finished
        crypto_markets_service._stop_event = asyncio.Event()

        await crypto_markets_service.stop_crypto_markets_refresh()

        self.assertFalse(crypto_markets_service._running)
        self.assertIsNone(crypto_markets_service._refresh_task)
        self.assertIsNone(crypto_markets_service._stop_event)

    async def test_stop_survives_a_task_that_raises_on_shutdown(self):
        async def _stubborn() -> None:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                raise RuntimeError("stubborn worker") from None

        stubborn = asyncio.create_task(_stubborn())
        self.addCleanup(stubborn.cancel)
        await asyncio.sleep(0)
        crypto_markets_service._running = True
        crypto_markets_service._refresh_task = stubborn
        crypto_markets_service._stop_event = asyncio.Event()

        with self.assertLogs(crypto_markets_service.logger, level="DEBUG"):
            await crypto_markets_service.stop_crypto_markets_refresh()

        self.assertFalse(crypto_markets_service._running)
        self.assertIsNone(crypto_markets_service._refresh_task)
        self.assertIsNone(crypto_markets_service._stop_event)


if __name__ == "__main__":
    unittest.main()
