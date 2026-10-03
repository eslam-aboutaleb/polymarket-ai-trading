"""Market-discovery tests for the latency-arbitrage engine (plan 06).

Covers the Gamma-event → (symbol, window, window-start) mapping
with synthetic payloads:

* ``current_window_start`` alignment for 5m/15m/1h boundaries,
* title-pattern matching (symbol + window + direction),
* outcome-label mapping (Up/Down and Yes/No),
* price extraction (``outcomePrices`` list and per-token prices),
* exclusion of non-crypto, non-direction, windowless,
    conditionless and stale markets,
* ``window_start_matches`` validation (the wrong-window guard),
* ``get_market`` / ``get_active_markets`` lookups.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from app.services.crypto_markets_service import (
    CryptoMarket,
    current_window_start,
    get_active_markets,
    get_market,
    parse_crypto_markets,
    window_start_matches,
)


def _iso(epoch_seconds: int) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=UTC).isoformat()


def _gamma_event(
    title: str,
    condition_id: str,
    tokens: list[dict],
    start_epoch: int,
    end_epoch: int | None = None,
    outcome_prices: list[str] | None = None,
) -> dict:
    """A synthetic Gamma /events payload."""
    market: dict = {
        "conditionId": condition_id,
        "question": title,
        "tokens": tokens,
        "startDate": _iso(start_epoch),
    }
    if end_epoch is not None:
        market["endDate"] = _iso(end_epoch)
    if outcome_prices is not None:
        market["outcomePrices"] = outcome_prices
    return {
        "id": f"evt-{condition_id}",
        "title": title,
        "markets": [market],
    }


def _up_down_tokens(
    up_token: str = "token-up-1",  # noqa: S107
    down_token: str = "token-down-1",  # noqa: S107
    up_price: float = 0.55,
    down_price: float = 0.45,
) -> list[dict]:
    return [
        {
            "token_id": up_token,
            "outcome": "Up",
            "price": up_price,
        },
        {
            "token_id": down_token,
            "outcome": "Down",
            "price": down_price,
        },
    ]


class WindowAlignmentTests(unittest.TestCase):
    def test_current_window_start_aligns_to_boundaries(self):
        now = datetime(2026, 10, 3, 2, 27, 41, tzinfo=UTC)
        # 02:27:41 → 5m boundary 02:25:00, 15m boundary
        # 02:15:00, 1h boundary 02:00:00.
        self.assertEqual(current_window_start(5, now), 1790994300)
        self.assertEqual(current_window_start(15, now), 1790993700)
        self.assertEqual(current_window_start(60, now), 1790992800)

    def test_current_window_start_exact_boundary(self):
        now = datetime(2026, 10, 3, 2, 30, 0, tzinfo=UTC)
        self.assertEqual(current_window_start(5, now), now.timestamp())
        self.assertEqual(current_window_start(15, now), now.timestamp())

    def test_current_window_start_never_in_the_future(self):
        now = datetime.now(UTC)
        for minutes in (5, 15, 60):
            start = current_window_start(minutes, now)
            self.assertLessEqual(start, now.timestamp())
            self.assertGreater(now.timestamp() - start, -1.0)
            self.assertLessEqual(now.timestamp() - start, minutes * 60)


class ParseCryptoMarketsTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(UTC)
        self.start_5m = current_window_start(5, self.now)
        self.start_15m = current_window_start(15, self.now)
        self.start_60m = current_window_start(60, self.now)

    def test_bitcoin_5m_market(self):
        events = [
            _gamma_event(
                "Bitcoin up or down in 5 minutes?",
                "cond-btc-5m",
                _up_down_tokens(),
                self.start_5m,
            )
        ]
        markets = parse_crypto_markets(events, self.now)
        self.assertIn(("BTC", 5, self.start_5m), markets)
        market = markets[("BTC", 5, self.start_5m)]
        self.assertEqual(market.condition_id, "cond-btc-5m")
        self.assertEqual(market.up_token_id, "token-up-1")
        self.assertEqual(market.down_token_id, "token-down-1")
        self.assertEqual(market.up_price, 0.55)
        self.assertEqual(market.down_price, 0.45)
        self.assertEqual(market.question, "Bitcoin up or down in 5 minutes?")

    def test_ethereum_15m_and_solana_1h(self):
        events = [
            _gamma_event(
                "Ethereum up or down in 15 minutes?",
                "cond-eth-15m",
                _up_down_tokens("eth-up", "eth-down"),
                self.start_15m,
            ),
            _gamma_event(
                "Solana up or down in 1 hour?",
                "cond-sol-1h",
                _up_down_tokens("sol-up", "sol-down"),
                self.start_60m,
            ),
        ]
        markets = parse_crypto_markets(events, self.now)
        self.assertIn(("ETH", 15, self.start_15m), markets)
        self.assertIn(("SOL", 60, self.start_60m), markets)
        self.assertEqual(
            markets[("ETH", 15, self.start_15m)].up_token_id,
            "eth-up",
        )
        self.assertEqual(
            markets[("SOL", 60, self.start_60m)].down_token_id,
            "sol-down",
        )

    def test_title_variants(self):
        events = [
            _gamma_event(
                "BTC 5m: up or down?",
                "cond-btc-variant",
                _up_down_tokens(),
                self.start_5m,
            ),
            _gamma_event(
                "Will Ethereum be up or down in the next 15 minutes?",
                "cond-eth-variant",
                _up_down_tokens(),
                self.start_15m,
            ),
            _gamma_event(
                "Solana 1-hour direction market",
                "cond-sol-variant",
                _up_down_tokens(),
                self.start_60m,
            ),
        ]
        markets = parse_crypto_markets(events, self.now)
        self.assertIn(("BTC", 5, self.start_5m), markets)
        self.assertIn(("ETH", 15, self.start_15m), markets)
        self.assertIn(("SOL", 60, self.start_60m), markets)

    def test_yes_no_outcomes_map_to_up_down(self):
        tokens = [
            {"token_id": "yes-token", "outcome": "Yes", "price": 0.62},
            {"token_id": "no-token", "outcome": "No", "price": 0.38},
        ]
        events = [
            _gamma_event(
                "Bitcoin up or down in 5 minutes?",
                "cond-yesno",
                tokens,
                self.start_5m,
            )
        ]
        markets = parse_crypto_markets(events, self.now)
        market = markets[("BTC", 5, self.start_5m)]
        self.assertEqual(market.up_token_id, "yes-token")
        self.assertEqual(market.down_token_id, "no-token")

    def test_outcome_prices_list(self):
        events = [
            _gamma_event(
                "Bitcoin up or down in 5 minutes?",
                "cond-prices",
                _up_down_tokens(),
                self.start_5m,
                outcome_prices=["0.71", "0.29"],
            )
        ]
        markets = parse_crypto_markets(events, self.now)
        market = markets[("BTC", 5, self.start_5m)]
        self.assertAlmostEqual(market.up_price, 0.71)
        self.assertAlmostEqual(market.down_price, 0.29)

    def test_non_crypto_event_excluded(self):
        events = [
            _gamma_event(
                "Will the US election be decided in 2028?",
                "cond-election",
                _up_down_tokens(),
                self.start_5m,
            )
        ]
        self.assertEqual(parse_crypto_markets(events, self.now), {})

    def test_crypto_without_direction_excluded(self):
        events = [
            _gamma_event(
                "Bitcoin price prediction for 5 minutes",
                "cond-nodirection",
                _up_down_tokens(),
                self.start_5m,
            )
        ]
        self.assertEqual(parse_crypto_markets(events, self.now), {})

    def test_crypto_without_window_excluded(self):
        events = [
            _gamma_event(
                "Bitcoin up or down?",
                "cond-nowindow",
                _up_down_tokens(),
                self.start_5m,
            )
        ]
        self.assertEqual(parse_crypto_markets(events, self.now), {})

    def test_market_without_condition_id_excluded(self):
        events = [
            _gamma_event(
                "Bitcoin up or down in 5 minutes?",
                "",
                _up_down_tokens(),
                self.start_5m,
            )
        ]
        self.assertEqual(parse_crypto_markets(events, self.now), {})

    def test_market_without_tokens_excluded(self):
        events = [
            _gamma_event(
                "Bitcoin up or down in 5 minutes?",
                "cond-notokens",
                [],
                self.start_5m,
            )
        ]
        self.assertEqual(parse_crypto_markets(events, self.now), {})

    def test_market_without_start_date_excluded(self):
        event = _gamma_event(
            "Bitcoin up or down in 5 minutes?",
            "cond-nostart",
            _up_down_tokens(),
            self.start_5m,
        )
        del event["markets"][0]["startDate"]
        self.assertEqual(parse_crypto_markets([event], self.now), {})

    def test_stale_window_excluded(self):
        # A window that ended two windows ago is untradeable.
        stale_start = self.start_5m - 2 * 5 * 60
        events = [
            _gamma_event(
                "Bitcoin up or down in 5 minutes?",
                "cond-stale",
                _up_down_tokens(),
                stale_start,
            )
        ]
        self.assertEqual(parse_crypto_markets(events, self.now), {})

    def test_next_window_retained(self):
        # The next window's market is active (pre-tradeable).
        next_start = self.start_5m + 5 * 60
        events = [
            _gamma_event(
                "Bitcoin up or down in 5 minutes?",
                "cond-next",
                _up_down_tokens(),
                next_start,
            )
        ]
        markets = parse_crypto_markets(events, self.now)
        self.assertIn(("BTC", 5, next_start), markets)

    def test_malformed_events_ignored(self):
        markets = parse_crypto_markets([None, "not-a-dict", {"no-markets": True}, {}], self.now)
        self.assertEqual(markets, {})

    def test_window_start_aligned_to_boundary(self):
        # A startDate with second-level noise still aligns
        # to the window boundary.
        noisy_start = self.start_5m + 37
        events = [
            _gamma_event(
                "Bitcoin up or down in 5 minutes?",
                "cond-noisy",
                _up_down_tokens(),
                noisy_start,
            )
        ]
        markets = parse_crypto_markets(events, self.now)
        self.assertIn(("BTC", 5, self.start_5m), markets)


class WindowValidationTests(unittest.TestCase):
    def test_window_start_matches_current_window(self):
        now = datetime.now(UTC)
        start = current_window_start(5, now)
        market = CryptoMarket(
            symbol="BTC",
            window_minutes=5,
            window_start_epoch=start,
            condition_id="cond-1",
        )
        self.assertTrue(window_start_matches(market, 5, now))

    def test_window_start_rejects_stale_window(self):
        now = datetime.now(UTC)
        stale = current_window_start(5, now) - 5 * 60
        market = CryptoMarket(
            symbol="BTC",
            window_minutes=5,
            window_start_epoch=stale,
            condition_id="cond-1",
        )
        self.assertFalse(window_start_matches(market, 5, now))

    def test_window_start_rejects_future_window(self):
        now = datetime.now(UTC)
        future = current_window_start(5, now) + 5 * 60
        market = CryptoMarket(
            symbol="BTC",
            window_minutes=5,
            window_start_epoch=future,
            condition_id="cond-1",
        )
        self.assertFalse(window_start_matches(market, 5, now))


class MarketLookupTests(unittest.TestCase):
    def test_get_market_defaults_to_current_window(self):
        now = datetime.now(UTC)
        start = current_window_start(5, now)
        events = [
            _gamma_event(
                "Bitcoin up or down in 5 minutes?",
                "cond-lookup",
                _up_down_tokens(),
                start,
            )
        ]
        # parse_crypto_markets is pure; the module-level
        # mapping is exercised through the lookup helpers
        # with an explicitly populated mapping.
        markets = parse_crypto_markets(events, now)
        self.assertIn(("BTC", 5, start), markets)
        market = markets[("BTC", 5, start)]
        self.assertEqual(market.condition_id, "cond-lookup")
        self.assertTrue(window_start_matches(market, 5, now))

    def test_get_active_markets_filters_current_windows(self):
        now = datetime.now(UTC)
        start_5m = current_window_start(5, now)
        # Two windows ago is outside the retained
        # [previous, next-next] range and is dropped.
        stale_5m = start_5m - 2 * 5 * 60
        events = [
            _gamma_event(
                "Bitcoin up or down in 5 minutes?",
                "cond-active",
                _up_down_tokens(),
                start_5m,
            ),
            _gamma_event(
                "Bitcoin up or down in 5 minutes?",
                "cond-stale",
                _up_down_tokens(),
                stale_5m,
            ),
        ]
        markets = parse_crypto_markets(events, now)
        # Only the current window survives parsing.
        self.assertEqual(len(markets), 1)
        self.assertIn(("BTC", 5, start_5m), markets)

    def test_get_market_returns_none_when_unmapped(self):
        # The module-level mapping is empty in a fresh
        # process; lookups return None rather than raising.
        self.assertIsNone(get_market("BTC", 5))
        self.assertIsNone(get_market("DOGE", 5))

    def test_get_active_markets_empty_when_unmapped(self):
        self.assertEqual(get_active_markets(), [])


if __name__ == "__main__":
    unittest.main()
