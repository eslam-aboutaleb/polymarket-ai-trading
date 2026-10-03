"""Coverage tests for ``app.services.polymarket_service``.

Every external I/O boundary is faked:

* ``httpx.AsyncClient`` -- replaced (only inside the service module's
  namespace) by a routing fake that maps URL substrings to canned
  responses, so Gamma / CLOB / Data-API calls are deterministic.
* Web3 / CLOB client -- instance attributes are replaced with mocks or
  lightweight fakes; ``_run_blocking`` still exercises the real
  thread-pool off-loading path.
* The module-level ``_market_data_cache`` and the class-level
  ``_market_cache`` / ``_historical_winrate_cache`` dicts are swapped
  for fresh per-test doubles so cache hits/misses are assertable.

Covered: balance helpers (CLOB proxy-wallet probing with signature-type
fallback, micro-USDC vs decimal detection, on-chain fallback), positions
(trade-history aggregation, dust/closed-market filtering, price lookup
cascade, Data-API normalisation), trade history (authenticated + public
fallback + position-derived pseudo-trades), market browsing (active /
newest / category / search-all with pagination, dedup, noise filters and
caching), trader stats, prices, historical win-rate computation, and the
portfolio summary rollup.
"""

import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from app.services import polymarket_service
from app.services.polymarket_service import (
    POLYMARKET_CLOB_API,
    POLYMARKET_DATA_API,
    POLYMARKET_GAMMA_API,
    PolymarketService,
    get_polymarket_service,
)

WALLET = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
PRIVATE_KEY = "0x" + "ab" * 32


class _RecordingCache:
    """Minimal cache double that records every ``set`` for TTL asserts."""

    def __init__(self):
        self.store: dict = {}
        self.set_calls: list[tuple[str, object, int]] = []

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ttl_seconds=300):
        self.set_calls.append((key, value, ttl_seconds))
        self.store[key] = value

    def set_calls_for(self, key):
        return [call for call in self.set_calls if call[0] == key]


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json_data = json_data

    def json(self):
        if isinstance(self._json_data, Exception):
            raise self._json_data
        return self._json_data


class _FakeAsyncClient:
    """Fake ``httpx.AsyncClient`` routing GETs by URL substring.

    A route value may be a ``_FakeResponse`` or a callable
    ``(url, params) -> _FakeResponse`` for request-dependent behaviour
    (e.g. pagination).
    """

    is_closed = False

    def __init__(self, routes=None, default=None, **kwargs):
        self.routes = routes or {}
        self.default = default
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, params=None):
        self.calls.append((url, params))
        for substring, handler in self.routes.items():
            if substring in url:
                if callable(handler):
                    return handler(url, params)
                return handler
        if self.default is not None:
            return self.default
        return _FakeResponse(status_code=404, json_data=None)


def _make_client_factory(routes, default, instances):
    class _Client(_FakeAsyncClient):
        def __init__(self, **kwargs):
            super().__init__(routes=routes, default=default, **kwargs)
            instances.append(self)

    return _Client


class _FakeClob:
    """Deterministic stand-in for an authenticated ``ClobClient``."""

    def __init__(
        self,
        markets=None,
        last_prices=None,
        midpoints=None,
        last_price=None,
        trades=None,
    ):
        self._markets = markets or {}
        self._last_prices = last_prices if last_prices is not None else []
        self._midpoints = midpoints if midpoints is not None else []
        self._last_price = last_price
        self._trades = trades if trades is not None else []
        self.calls: list[tuple] = []

    def get_market(self, condition_id):
        self.calls.append(("get_market", condition_id))
        value = self._markets.get(condition_id)
        if isinstance(value, Exception):
            raise value
        return value

    def get_last_trades_prices(self, params):
        self.calls.append(("get_last_trades_prices", params))
        if isinstance(self._last_prices, Exception):
            raise self._last_prices
        return self._last_prices

    def get_midpoints(self, params):
        self.calls.append(("get_midpoints", params))
        if isinstance(self._midpoints, Exception):
            raise self._midpoints
        return self._midpoints

    def get_last_trade_price(self, asset_id):
        self.calls.append(("get_last_trade_price", asset_id))
        if isinstance(self._last_price, Exception):
            raise self._last_price
        return self._last_price

    def get_trades(self):
        self.calls.append(("get_trades",))
        if isinstance(self._trades, Exception):
            raise self._trades
        return self._trades

    def get_balance_allowance(self, params):
        self.calls.append(("get_balance_allowance", params))
        return self._balance_response


class _PolymarketServiceTestCase(unittest.IsolatedAsyncioTestCase):
    """Fresh service + fresh class-level caches + recording market cache."""

    def setUp(self):
        self.service = PolymarketService()

        market_cache: dict = {}
        mc_patcher = patch.object(PolymarketService, "_market_cache", market_cache)
        mc_patcher.start()
        self.addCleanup(mc_patcher.stop)
        self.market_cache = market_cache

        hr_cache: dict = {}
        hr_patcher = patch.object(PolymarketService, "_historical_winrate_cache", hr_cache)
        hr_patcher.start()
        self.addCleanup(hr_patcher.stop)
        self.hr_cache = hr_cache

        self.cache = _RecordingCache()
        cache_patcher = patch.object(polymarket_service, "_market_data_cache", self.cache)
        cache_patcher.start()
        self.addCleanup(cache_patcher.stop)

        # Neutralise Web3 so no test can touch the network.
        self.service.w3 = MagicMock()
        self.service.w3.eth.get_balance.return_value = 0
        self.service.usdc_contract = MagicMock()

        # The shared httpx client is a class-level singleton; reset it
        # so no test can inherit a stale client (or make real calls).
        saved_shared = PolymarketService._shared_http_client
        self.addCleanup(lambda: setattr(PolymarketService, "_shared_http_client", saved_shared))
        PolymarketService._shared_http_client = None

        self.http_instances: list[_FakeAsyncClient] = []

    @property
    def http_calls(self):
        return [call for inst in self.http_instances for call in inst.calls]

    def _patch_http(self, routes=None, default=None):
        """Swap the service module's ``httpx`` for the routing fake."""
        factory = _make_client_factory(routes or {}, default, self.http_instances)
        fake_httpx = MagicMock()
        fake_httpx.AsyncClient = factory
        patcher = patch.object(polymarket_service, "httpx", fake_httpx)
        patcher.start()
        self.addCleanup(patcher.stop)
        return factory

    def _install_shared_client(self, client):
        """Point the shared-client singleton at a fake (search_all_markets)."""
        saved = PolymarketService._shared_http_client
        PolymarketService._shared_http_client = client
        self.addCleanup(lambda: setattr(PolymarketService, "_shared_http_client", saved))

    def _set_matic_wei(self, wei):
        self.service.w3.eth.get_balance.return_value = wei

    def _set_usdc_raw(self, raw):
        self.service.usdc_contract.functions.balanceOf.return_value.call.return_value = raw


class RunBlockingTests(_PolymarketServiceTestCase):
    """``_run_blocking`` off-loads to the dedicated executor."""

    async def test_runs_func_with_args(self):
        def add(a, b):
            return a + b

        self.assertEqual(await PolymarketService._run_blocking(add, 2, 3), 5)

    async def test_runs_func_with_kwargs(self):
        def greet(name, punctuation="!"):
            return f"hi {name}{punctuation}"

        result = await PolymarketService._run_blocking(greet, "bob", punctuation="?")
        self.assertEqual(result, "hi bob?")

    async def test_propagates_exceptions(self):
        def boom():
            raise RuntimeError("worker error")

        with self.assertRaises(RuntimeError):
            await PolymarketService._run_blocking(boom)


class GetClobClientTests(_PolymarketServiceTestCase):
    """``_get_clob_client`` construction and credential handling."""

    def _patch_clob_types(self):
        client_cls = MagicMock()
        creds_cls = MagicMock()
        client_patcher = patch("py_clob_client.client.ClobClient", client_cls)
        creds_patcher = patch("py_clob_client.clob_types.ApiCreds", creds_cls)
        client_patcher.start()
        creds_patcher.start()
        self.addCleanup(client_patcher.stop)
        self.addCleanup(creds_patcher.stop)
        return client_cls, creds_cls

    def test_creds_are_set_when_provided(self):
        client_cls, creds_cls = self._patch_clob_types()
        creds = {
            "api_key": "k",
            "api_secret": "s",
            "api_passphrase": "p",
        }

        client = self.service._get_clob_client(PRIVATE_KEY, creds)

        client_cls.assert_called_once_with(
            host=POLYMARKET_CLOB_API,
            chain_id=137,
            key=PRIVATE_KEY,
            signature_type=1,
            funder=None,
        )
        creds_cls.assert_called_once_with(api_key="k", api_secret="s", api_passphrase="p")
        client_cls.return_value.set_api_creds.assert_called_once_with(creds_cls.return_value)
        client_cls.return_value.create_or_derive_api_creds.assert_not_called()
        self.assertIs(client, client_cls.return_value)

    def test_creds_are_derived_when_missing(self):
        client_cls, creds_cls = self._patch_clob_types()

        self.service._get_clob_client(PRIVATE_KEY, None)

        creds_cls.assert_not_called()
        client_cls.return_value.create_or_derive_api_creds.assert_called_once_with()
        client_cls.return_value.set_api_creds.assert_called_once_with(
            client_cls.return_value.create_or_derive_api_creds.return_value
        )

    def test_signature_type_and_funder_are_forwarded(self):
        client_cls, _ = self._patch_clob_types()

        self.service._get_clob_client(PRIVATE_KEY, None, signature_type=2, funder="0xfunder")

        client_cls.assert_called_once_with(
            host=POLYMARKET_CLOB_API,
            chain_id=137,
            key=PRIVATE_KEY,
            signature_type=2,
            funder="0xfunder",
        )


class ToFloatTests(_PolymarketServiceTestCase):
    """``_to_float`` coercion helper."""

    def test_none_returns_default(self):
        self.assertEqual(PolymarketService._to_float(None), 0.0)
        self.assertEqual(PolymarketService._to_float(None, 1.5), 1.5)

    def test_numeric_strings_and_numbers(self):
        self.assertEqual(PolymarketService._to_float("10"), 10.0)
        self.assertEqual(PolymarketService._to_float(3.5), 3.5)
        self.assertEqual(PolymarketService._to_float(7), 7.0)

    def test_invalid_values_return_default(self):
        self.assertEqual(PolymarketService._to_float("abc"), 0.0)
        self.assertEqual(PolymarketService._to_float([1]), 0.0)
        self.assertEqual(PolymarketService._to_float({"a": 1}), 0.0)


class NormalizeDataApiPositionsTests(_PolymarketServiceTestCase):
    """``_normalize_data_api_positions`` field mapping and filtering."""

    def test_full_field_mapping(self):
        items = [
            {
                "size": "10",
                "avgPrice": "0.5",
                "curPrice": "0.7",
                "title": "T",
                "market": "M",
                "market_slug": "m-slug",
                "outcome": "Yes",
                "asset_id": "a1",
            }
        ]
        result = PolymarketService._normalize_data_api_positions(items)

        self.assertEqual(len(result), 1)
        pos = result[0]
        self.assertEqual(pos["title"], "T")
        self.assertEqual(pos["market"], "M")
        self.assertEqual(pos["market_slug"], "m-slug")
        self.assertEqual(pos["outcome"], "Yes")
        self.assertEqual(pos["size"], 10.0)
        self.assertEqual(pos["avgPrice"], 0.5)
        self.assertEqual(pos["curPrice"], 0.7)
        self.assertEqual(pos["pnl"], round(10 * 0.7 - 10 * 0.5, 4))
        self.assertEqual(pos["asset_id"], "a1")

    def test_snake_case_and_alternate_field_names(self):
        items = [
            {
                "amount": "5",
                "avg_price": "0.4",
                "cur_price": "0.3",
                "question": "Q",
                "condition_id": "c1",
                "marketSlug": "ms",
                "outcome_name": "No",
                "token_id": "t1",
            }
        ]
        result = PolymarketService._normalize_data_api_positions(items)

        pos = result[0]
        self.assertEqual(pos["title"], "Q")
        self.assertEqual(pos["market"], "c1")
        self.assertEqual(pos["market_slug"], "ms")
        self.assertEqual(pos["outcome"], "No")
        self.assertEqual(pos["size"], 5.0)
        self.assertEqual(pos["avgPrice"], 0.4)
        self.assertEqual(pos["curPrice"], 0.3)
        self.assertEqual(pos["asset_id"], "t1")

    def test_title_falls_back_to_market_then_unknown(self):
        items = [{"market": "M", "size": "1"}, {"size": "1"}]
        result = PolymarketService._normalize_data_api_positions(items)
        self.assertEqual(result[0]["title"], "M")
        self.assertEqual(result[1]["title"], "Unknown")

    def test_cur_price_falls_back_to_price_then_avg_price(self):
        items = [
            {"size": "2", "avgPrice": "0.5", "price": "0.9"},
            {"size": "2", "avgPrice": "0.5"},
        ]
        result = PolymarketService._normalize_data_api_positions(items)
        self.assertEqual(result[0]["curPrice"], 0.9)
        self.assertEqual(result[1]["curPrice"], 0.5)

    def test_invalid_size_is_skipped(self):
        items = [
            {"size": "abc", "avgPrice": "0.5"},
            {"size": "0.0005", "avgPrice": "0.5"},
            {"size": None, "avgPrice": "0.5"},
        ]
        self.assertEqual(PolymarketService._normalize_data_api_positions(items), [])

    def test_invalid_prices_default_to_zero(self):
        items = [{"size": "2", "avgPrice": "x", "curPrice": "y"}]
        result = PolymarketService._normalize_data_api_positions(items)
        self.assertEqual(result[0]["avgPrice"], 0.0)
        self.assertEqual(result[0]["curPrice"], 0.0)
        self.assertEqual(result[0]["pnl"], 0.0)

    def test_non_dict_items_are_skipped(self):
        items = ["nope", 42, None, {"size": "1", "avgPrice": "0.5"}]
        result = PolymarketService._normalize_data_api_positions(items)
        self.assertEqual(len(result), 1)

    def test_empty_list(self):
        self.assertEqual(PolymarketService._normalize_data_api_positions([]), [])


class BuildTokensFromGammaTests(_PolymarketServiceTestCase):
    """``_build_tokens_from_gamma`` token pairing."""

    def test_existing_tokens_are_preserved(self):
        m = {"tokens": [{"token_id": "x", "outcome": "Yes"}]}
        PolymarketService._build_tokens_from_gamma(m)
        self.assertEqual(m["tokens"], [{"token_id": "x", "outcome": "Yes"}])

    def test_empty_tokens_list_is_rebuilt(self):
        m = {"tokens": [], "clobTokenIds": '["t1"]', "outcomes": '["Yes"]'}
        PolymarketService._build_tokens_from_gamma(m)
        self.assertEqual(m["tokens"], [{"token_id": "t1", "outcome": "Yes"}])

    def test_no_token_ids_is_a_no_op(self):
        m = {"outcomes": '["Yes","No"]'}
        PolymarketService._build_tokens_from_gamma(m)
        self.assertNotIn("tokens", m)

    def test_snake_case_token_ids_key(self):
        m = {"clob_token_ids": '["t1","t2"]', "outcomes": '["Yes","No"]'}
        PolymarketService._build_tokens_from_gamma(m)
        self.assertEqual(
            m["tokens"],
            [
                {"token_id": "t1", "outcome": "Yes"},
                {"token_id": "t2", "outcome": "No"},
            ],
        )

    def test_list_token_ids_without_json(self):
        m = {"clobTokenIds": ["t1", "t2"], "outcomes": ["Yes", "No"]}
        PolymarketService._build_tokens_from_gamma(m)
        self.assertEqual(len(m["tokens"]), 2)

    def test_invalid_json_is_a_no_op(self):
        m = {"clobTokenIds": "not-json", "outcomes": '["Yes"]'}
        PolymarketService._build_tokens_from_gamma(m)
        self.assertNotIn("tokens", m)

    def test_non_list_token_ids_is_a_no_op(self):
        m = {"clobTokenIds": '"just-a-string"', "outcomes": '["Yes"]'}
        PolymarketService._build_tokens_from_gamma(m)
        self.assertNotIn("tokens", m)

    def test_missing_outcomes_defaults_to_yes_no(self):
        m = {"clobTokenIds": '["t1","t2"]'}
        PolymarketService._build_tokens_from_gamma(m)
        self.assertEqual(
            m["tokens"],
            [
                {"token_id": "t1", "outcome": "Yes"},
                {"token_id": "t2", "outcome": "No"},
            ],
        )

    def test_non_list_outcomes_defaults_to_yes_no(self):
        m = {"clobTokenIds": '["t1"]', "outcomes": {"a": 1}}
        PolymarketService._build_tokens_from_gamma(m)
        self.assertEqual(m["tokens"], [{"token_id": "t1", "outcome": "Yes"}])

    def test_fewer_outcomes_than_tokens(self):
        m = {"clobTokenIds": '["t1","t2","t3"]', "outcomes": '["Yes"]'}
        PolymarketService._build_tokens_from_gamma(m)
        self.assertEqual([t["outcome"] for t in m["tokens"]], ["Yes", "No", "No"])


class EventMatchesTagTests(_PolymarketServiceTestCase):
    """``_event_matches_tag`` server-side tag filtering."""

    def test_matching_slug(self):
        event = {"tags": [{"slug": "NBA"}]}
        self.assertTrue(PolymarketService._event_matches_tag(event, "sports"))

    def test_non_matching_slug(self):
        event = {"tags": [{"slug": "politics"}]}
        self.assertFalse(PolymarketService._event_matches_tag(event, "sports"))

    def test_unknown_tag_never_matches(self):
        event = {"tags": [{"slug": "sports"}]}
        self.assertFalse(PolymarketService._event_matches_tag(event, "does-not-exist"))

    def test_event_without_tags(self):
        self.assertFalse(PolymarketService._event_matches_tag({}, "sports"))
        self.assertFalse(PolymarketService._event_matches_tag({"tags": []}, "sports"))

    def test_tag_without_slug_is_skipped(self):
        event = {"tags": [{"name": "NBA"}, {"slug": "nfl"}]}
        self.assertTrue(PolymarketService._event_matches_tag(event, "sports"))


class IsNoiseEventTests(_PolymarketServiceTestCase):
    """``_is_noise_event`` pre-filter for newest markets."""

    def test_up_or_down_title_is_noise(self):
        self.assertTrue(
            self.service._is_noise_event({"title": "BTC up or down 5m", "liquidity": 1000})
        )

    def test_updown_title_is_noise(self):
        self.assertTrue(self.service._is_noise_event({"title": "ETH updown", "liquidity": 1000}))

    def test_title_matching_is_case_insensitive(self):
        self.assertTrue(
            self.service._is_noise_event({"title": "BTC UP OR DOWN", "liquidity": 1000})
        )

    def test_low_liquidity_is_noise(self):
        self.assertTrue(self.service._is_noise_event({"title": "Fine", "liquidity": 39}))

    def test_missing_liquidity_is_noise(self):
        self.assertTrue(self.service._is_noise_event({"title": "Fine"}))
        self.assertTrue(self.service._is_noise_event({}))

    def test_healthy_event_is_not_noise(self):
        self.assertFalse(self.service._is_noise_event({"title": "Fine", "liquidity": 40}))


class ResolveMarketQuestionTests(_PolymarketServiceTestCase):
    """``_resolve_market_question`` caching and error handling."""

    def _clob(self, market):
        clob = _FakeClob()
        clob._markets["cid-1"] = market
        return clob

    def test_resolves_full_market_info(self):
        clob = self._clob(
            {
                "question": "Will it rain?",
                "market_slug": "rain",
                "description": "d",
                "end_date_iso": "2026-01-01",
                "closed": False,
                "active": True,
                "accepting_orders": True,
                "tokens": [
                    {"token_id": "t1", "outcome": "Yes"},
                    {"token_id": "t2", "outcome": "No"},
                    "not-a-dict",
                ],
            }
        )

        info = self.service._resolve_market_question(clob, "cid-1")

        self.assertEqual(info["question"], "Will it rain?")
        self.assertEqual(info["market_slug"], "rain")
        self.assertEqual(info["description"], "d")
        self.assertEqual(info["end_date_iso"], "2026-01-01")
        self.assertFalse(info["closed"])
        self.assertTrue(info["active"])
        self.assertTrue(info["accepting_orders"])
        self.assertEqual(info["tokens"], {"t1": "Yes", "t2": "No"})

    def test_result_is_cached(self):
        clob = self._clob({"question": "Q"})
        first = self.service._resolve_market_question(clob, "cid-1")
        second = self.service._resolve_market_question(clob, "cid-1")
        self.assertIs(first, second)
        self.assertEqual([c for c in clob.calls if c[0] == "get_market"], [("get_market", "cid-1")])

    def test_non_dict_market_yields_defaults(self):
        clob = self._clob("not-a-dict")
        info = self.service._resolve_market_question(clob, "cid-1")
        self.assertEqual(info["question"], "cid-1")
        self.assertIsNone(info["market_slug"])
        self.assertNotIn("tokens", info)

    def test_missing_market_yields_defaults(self):
        clob = self._clob(None)
        info = self.service._resolve_market_question(clob, "cid-1")
        self.assertEqual(info["question"], "cid-1")

    def test_client_error_yields_defaults_and_caches(self):
        clob = self._clob(RuntimeError("boom"))
        info = self.service._resolve_market_question(clob, "cid-1")
        self.assertEqual(info["question"], "cid-1")
        self.assertIsNone(info["market_slug"])
        # Still cached so repeated lookups stay free.
        self.assertIn("cid-1", self.market_cache)

    def test_market_without_tokens_has_no_token_map(self):
        clob = self._clob({"question": "Q", "tokens": []})
        info = self.service._resolve_market_question(clob, "cid-1")
        self.assertNotIn("tokens", info)


class NormalizeClobTradeTests(_PolymarketServiceTestCase):
    """``_normalize_clob_trade`` field normalisation."""

    def _item(self, **overrides):
        item = {
            "id": "t1",
            "market": "cid-1",
            "outcome": "Yes",
            "side": "buy",
            "size": "10",
            "price": "0.5",
            "fee_rate_bps": "10",
            "match_time": "1700000000",
            "trader_side": "MAKER",
            "status": "filled",
            "transaction_hash": "0xhash",
            "maker_address": "0xmaker",
        }
        item.update(overrides)
        return item

    def test_full_normalisation(self):
        result = self.service._normalize_clob_trade(
            self._item(), {"question": "Q", "market_slug": "q"}
        )

        self.assertEqual(result["id"], "t1")
        self.assertEqual(result["market"], "Q")
        self.assertEqual(result["market_slug"], "q")
        self.assertEqual(result["condition_id"], "cid-1")
        self.assertEqual(result["outcome"], "Yes")
        self.assertEqual(result["side"], "BUY")
        self.assertEqual(result["size"], 10.0)
        self.assertEqual(result["price"], 0.5)
        self.assertEqual(result["type"], "MAKER")
        self.assertEqual(result["status"], "FILLED")
        self.assertEqual(
            result["timestamp"],
            datetime.fromtimestamp(1700000000, tz=UTC).isoformat(),
        )
        self.assertEqual(result["fee_rate_bps"], 10.0)
        self.assertEqual(result["fee"], 0.005)
        self.assertEqual(result["transaction_hash"], "0xhash")
        self.assertEqual(result["maker_address"], "0xmaker")
        self.assertEqual(result["trader_side"], "MAKER")
        self.assertIsNone(result["pnl"])

    def test_defaults_when_fields_missing(self):
        result = self.service._normalize_clob_trade({"market": "cid-1"}, {"question": "Q"})
        self.assertEqual(result["side"], "")
        self.assertEqual(result["type"], "TRADE")
        self.assertEqual(result["status"], "CONFIRMED")
        self.assertIsNone(result["timestamp"])
        self.assertEqual(result["fee_rate_bps"], 0.0)
        self.assertEqual(result["fee"], 0.0)
        self.assertIsNone(result["market_slug"])

    def test_last_update_used_when_match_time_missing(self):
        result = self.service._normalize_clob_trade(
            self._item(match_time=None, last_update="1700000100"), {}
        )
        self.assertEqual(
            result["timestamp"],
            datetime.fromtimestamp(1700000100, tz=UTC).isoformat(),
        )

    def test_invalid_match_time_is_kept_raw(self):
        result = self.service._normalize_clob_trade(self._item(match_time="not-a-number"), {})
        self.assertEqual(result["timestamp"], "not-a-number")

    def test_huge_match_time_is_kept_raw(self):
        # datetime.fromtimestamp raises OSError for absurd epochs; the
        # service must keep the raw value instead of blowing up.
        result = self.service._normalize_clob_trade(self._item(match_time=str(10**18)), {})
        self.assertEqual(result["timestamp"], str(10**18))

    def test_market_info_question_falls_back_to_raw_market(self):
        result = self.service._normalize_clob_trade(self._item(), {})
        self.assertEqual(result["market"], "cid-1")


class NormalizeAuthenticatedTradesTests(_PolymarketServiceTestCase):
    """``_normalize_authenticated_trades`` sorting and pagination."""

    def _clob(self):
        return _FakeClob(
            markets={
                "cid-1": {"question": "Q One", "market_slug": "q-one"},
                "cid-2": {"question": "Q Two"},
            }
        )

    def test_markets_are_resolved_and_sorted_desc(self):
        clob = self._clob()
        raw = [
            self._trade("t1", "cid-1", match_time="1700000000"),
            self._trade("t2", "cid-2", match_time="1700000100"),
        ]

        result = self.service._normalize_authenticated_trades(clob, raw, 200, 0)

        self.assertEqual([t["id"] for t in result], ["t2", "t1"])
        self.assertEqual(result[0]["market"], "Q Two")
        self.assertEqual(result[1]["market"], "Q One")
        self.assertEqual(result[1]["market_slug"], "q-one")

    def test_offset_and_limit_are_applied(self):
        clob = self._clob()
        raw = [
            self._trade("t1", "cid-1", match_time="1700000002"),
            self._trade("t2", "cid-1", match_time="1700000001"),
            self._trade("t3", "cid-1", match_time="1700000000"),
        ]

        result = self.service._normalize_authenticated_trades(clob, raw, 2, 1)
        self.assertEqual([t["id"] for t in result], ["t2", "t3"])

        result = self.service._normalize_authenticated_trades(clob, raw, 2, 10)
        self.assertEqual(result, [])

    def test_unresolvable_market_uses_condition_id(self):
        clob = _FakeClob(markets={})
        clob._markets["cid-1"] = RuntimeError("boom")
        raw = [self._trade("t1", "cid-1", match_time="1700000000")]

        result = self.service._normalize_authenticated_trades(clob, raw, 200, 0)
        self.assertEqual(result[0]["market"], "cid-1")

    def test_empty_trades(self):
        self.assertEqual(
            self.service._normalize_authenticated_trades(self._clob(), [], 200, 0),
            [],
        )

    @staticmethod
    def _trade(tid, cid, match_time):
        return {
            "id": tid,
            "market": cid,
            "outcome": "Yes",
            "side": "BUY",
            "size": "1",
            "price": "0.5",
            "match_time": match_time,
        }


class GetWalletBalanceTests(_PolymarketServiceTestCase):
    """``get_wallet_balance`` CLOB probe + on-chain fallback."""

    def _clob_balance_client(self, responses_by_sig):
        """Patch ``_get_clob_client`` to return per-signature-type fakes."""
        clients = {st: _FakeClob() for st in responses_by_sig}
        for st, resp in responses_by_sig.items():
            if isinstance(resp, Exception):
                clients[st] = None
            else:
                clients[st]._balance_response = resp

        def factory(private_key, clob_creds, signature_type=1):
            if signature_type not in clients or clients[signature_type] is None:
                raise RuntimeError(f"sig type {signature_type} unusable")
            return clients[signature_type]

        self.service._get_clob_client = MagicMock(side_effect=factory)
        return clients

    async def test_on_chain_fallback_without_private_key(self):
        self._set_matic_wei(2_000_000_000_000_000_000)
        self._set_usdc_raw(2_500_000)

        result = await self.service.get_wallet_balance(WALLET)

        self.assertEqual(result["wallet_address"], WALLET)
        self.assertEqual(result["matic_balance"], 2.0)
        self.assertEqual(result["usdc_balance"], 2.5)
        self.assertEqual(result["chain"], "polygon")
        self.service.usdc_contract.functions.balanceOf.assert_called_once()

    async def test_matic_failure_is_tolerated(self):
        self.service.w3.eth.get_balance.side_effect = RuntimeError("rpc down")
        self._set_usdc_raw(1_000_000)

        result = await self.service.get_wallet_balance(WALLET)

        self.assertEqual(result["matic_balance"], 0.0)
        self.assertEqual(result["usdc_balance"], 1.0)

    async def test_clob_micro_usdc_balance(self):
        self._set_matic_wei(1_000_000_000_000_000_000)
        self._clob_balance_client(
            {1: {"balance": "5000000"}, 0: {"balance": "0"}, 2: {"balance": "0"}}
        )

        result = await self.service.get_wallet_balance(WALLET, private_key=PRIVATE_KEY)

        # 5,000,000 micro-USDC == 5.0 USDC
        self.assertEqual(result["usdc_balance"], 5.0)
        self.assertEqual(result["matic_balance"], 1.0)
        # On-chain fallback must not run when the CLOB path succeeds.
        self.service.usdc_contract.functions.balanceOf.assert_not_called()

    async def test_clob_decimal_balance(self):
        self._clob_balance_client(
            {1: {"balance": "1.25"}, 0: {"balance": "0"}, 2: {"balance": "0"}}
        )

        result = await self.service.get_wallet_balance(WALLET, private_key=PRIVATE_KEY)
        self.assertEqual(result["usdc_balance"], 1.25)

    async def test_clob_probes_all_signature_types(self):
        # sig 1 and 0 return zero, sig 2 carries the balance.
        self._clob_balance_client(
            {
                1: {"balance": "0"},
                0: {"balance": "0"},
                2: {"balance": "2500000"},
            }
        )

        result = await self.service.get_wallet_balance(WALLET, private_key=PRIVATE_KEY)
        self.assertEqual(result["usdc_balance"], 2.5)

    async def test_all_zero_balances_return_zero(self):
        self._clob_balance_client({1: {"balance": "0"}, 0: {"balance": "0"}, 2: {"balance": "0"}})

        result = await self.service.get_wallet_balance(WALLET, private_key=PRIVATE_KEY)
        self.assertEqual(result["usdc_balance"], 0.0)

    async def test_first_usable_signature_type_wins(self):
        # sig 1 raises, sig 0 succeeds with a balance.
        self._clob_balance_client(
            {1: RuntimeError("nope"), 0: {"balance": "1000000"}, 2: {"balance": "0"}}
        )

        result = await self.service.get_wallet_balance(WALLET, private_key=PRIVATE_KEY)
        self.assertEqual(result["usdc_balance"], 1.0)

    async def test_all_signature_types_failing_falls_back_on_chain(self):
        # Every probe fails: the loop completes without raising,
        # a warning is logged, and the on-chain USDC balance is
        # used as the fallback.
        self._set_matic_wei(3_000_000_000_000_000_000)
        self._set_usdc_raw(7_000_000)
        self._clob_balance_client(
            {1: RuntimeError("a"), 0: RuntimeError("b"), 2: RuntimeError("c")}
        )

        result = await self.service.get_wallet_balance(WALLET, private_key=PRIVATE_KEY)
        self.assertEqual(result["matic_balance"], 3.0)
        self.assertEqual(result["usdc_balance"], 7.0)
        self.service.usdc_contract.functions.balanceOf.assert_called_once_with(WALLET)

    async def test_non_numeric_balance_falls_back_on_chain(self):
        # A non-numeric balance breaks the Decimal conversion in the
        # CLOB branch, which is the one path that reaches the outer
        # except and triggers the on-chain fallback.
        self._set_matic_wei(3_000_000_000_000_000_000)
        self._set_usdc_raw(7_000_000)
        self._clob_balance_client({1: {"balance": "not-a-number"}})

        result = await self.service.get_wallet_balance(WALLET, private_key=PRIVATE_KEY)
        self.assertEqual(result["matic_balance"], 3.0)
        self.assertEqual(result["usdc_balance"], 7.0)

    async def test_on_chain_usdc_failure_yields_zero(self):
        self.service.usdc_contract.functions.balanceOf.return_value.call.side_effect = RuntimeError(
            "contract call failed"
        )

        result = await self.service.get_wallet_balance(WALLET)

        self.assertEqual(result["usdc_balance"], 0.0)

    async def test_invalid_address_returns_zero_balances(self):
        result = await self.service.get_wallet_balance("not-an-address")
        self.assertEqual(result["matic_balance"], 0.0)
        self.assertEqual(result["usdc_balance"], 0.0)

    async def test_clob_creds_are_forwarded(self):
        self._clob_balance_client({1: {"balance": "1000000"}})
        creds = {"api_key": "k", "api_secret": "s", "api_passphrase": "p"}

        await self.service.get_wallet_balance(WALLET, private_key=PRIVATE_KEY, clob_creds=creds)

        self.service._get_clob_client.assert_any_call(PRIVATE_KEY, creds, signature_type=1)


class BuildPositionsFromTradesTests(_PolymarketServiceTestCase):
    """``_build_positions_from_trades`` aggregation and filtering."""

    def _trades(self):
        return [
            {
                "market": "cid-1",
                "outcome": "Yes",
                "side": "BUY",
                "size": "10",
                "price": "0.5",
                "asset_id": "tok-1",
            },
            {
                "market": "cid-1",
                "outcome": "Yes",
                "side": "SELL",
                "size": "2",
                "price": "0.6",
                "asset_id": "tok-1",
            },
            # Dust position — net size below the 0.01 threshold.
            {
                "market": "cid-2",
                "outcome": "No",
                "side": "BUY",
                "size": "0.005",
                "price": "0.5",
                "asset_id": "tok-2",
            },
            # Closed market — must be skipped.
            {
                "market": "cid-3",
                "outcome": "Yes",
                "side": "BUY",
                "size": "5",
                "price": "0.4",
                "asset_id": "tok-3",
            },
            # Market not accepting orders — must be skipped.
            {
                "market": "cid-4",
                "outcome": "Yes",
                "side": "BUY",
                "size": "5",
                "price": "0.4",
                "asset_id": "tok-4",
            },
            # Unknown side — bucket created but no size accumulated.
            {
                "market": "cid-5",
                "outcome": "Yes",
                "side": "HOLD",
                "size": "5",
                "price": "0.4",
                "asset_id": "tok-5",
            },
            # No outcome — defaults to "Unknown".
            {
                "market": "cid-1",
                "outcome": None,
                "side": "BUY",
                "size": "1",
                "price": "0.5",
                "asset_id": "tok-1b",
            },
            # No outcome on a market without a token map.
            {
                "market": "cid-6",
                "outcome": None,
                "side": "BUY",
                "size": "1",
                "price": "0.5",
                "asset_id": "tok-6",
            },
        ]

    def _clob(self, last_prices=None, midpoints=None, last_price=None):
        return _FakeClob(
            markets={
                "cid-1": {
                    "question": "Q One",
                    "market_slug": "q-one",
                    "closed": False,
                    "accepting_orders": True,
                    "tokens": [
                        {"token_id": "tok-1", "outcome": "YES token"},
                        {"token_id": "tok-1b", "outcome": "YES alt"},
                    ],
                },
                "cid-2": {
                    "question": "Q Two",
                    "closed": False,
                    "accepting_orders": True,
                },
                "cid-3": {"question": "Q Three", "closed": True},
                "cid-4": {
                    "question": "Q Four",
                    "closed": False,
                    "accepting_orders": False,
                },
                "cid-5": {
                    "question": "Q Five",
                    "closed": False,
                    "accepting_orders": True,
                },
                "cid-6": {
                    "question": "Q Six",
                    "closed": False,
                    "accepting_orders": True,
                },
            },
            last_prices=last_prices,
            midpoints=midpoints,
            last_price=last_price,
        )

    def test_aggregates_buy_sell_and_filters(self):
        clob = self._clob(last_prices=[{"token_id": "tok-1", "price": "0.7"}])

        positions = self.service._build_positions_from_trades(clob, self._trades())

        # Only cid-1/Yes, cid-1/Unknown and cid-6/Unknown survive:
        # dust (cid-2), closed (cid-3), not-accepting (cid-4) and
        # zero-net (cid-5) are filtered.
        self.assertEqual(len(positions), 3)
        yes = next(p for p in positions if p["outcome"] == "YES token")
        alt = next(p for p in positions if p["outcome"] == "YES alt")
        unknown = next(p for p in positions if p["asset_id"] == "tok-6")

        self.assertEqual(yes["title"], "Q One")
        self.assertEqual(yes["market"], "Q One")
        self.assertEqual(yes["condition_id"], "cid-1")
        self.assertEqual(yes["market_slug"], "q-one")
        self.assertEqual(yes["size"], 8.0)
        self.assertEqual(yes["avgPrice"], 0.5)
        self.assertEqual(yes["curPrice"], 0.7)
        self.assertEqual(yes["pnl"], round(8 * 0.7 - 8 * 0.5, 4))
        self.assertEqual(yes["asset_id"], "tok-1")

        # The token map renames the outcome even for the "Unknown" bucket.
        self.assertEqual(alt["size"], 1.0)
        self.assertEqual(alt["asset_id"], "tok-1b")
        self.assertEqual(alt["outcome"], "YES alt")

        self.assertEqual(unknown["size"], 1.0)
        self.assertEqual(unknown["outcome"], "Unknown")

    def test_market_resolution_is_cached(self):
        clob = self._clob()
        self.service._build_positions_from_trades(clob, self._trades())
        self.assertIn("cid-1", self.market_cache)
        self.assertIn("cid-3", self.market_cache)

    def test_midpoint_fallback_when_bulk_prices_empty(self):
        clob = self._clob(
            last_prices=[],
            midpoints=[{"token_id": "tok-1", "mid": "0.65"}],
        )
        positions = self.service._build_positions_from_trades(clob, self._trades())
        yes = next(p for p in positions if p["asset_id"] == "tok-1")
        self.assertEqual(yes["curPrice"], 0.65)

    def test_midpoint_fallback_when_bulk_price_is_zero(self):
        clob = self._clob(
            last_prices=[{"token_id": "tok-1", "price": "0"}],
            midpoints=[{"token_id": "tok-1", "mid": "0.65"}],
        )
        positions = self.service._build_positions_from_trades(clob, self._trades())
        yes = next(p for p in positions if p["asset_id"] == "tok-1")
        self.assertEqual(yes["curPrice"], 0.65)

    def test_midpoint_fallback_when_bulk_prices_raise(self):
        clob = self._clob(
            last_prices=RuntimeError("bulk failed"),
            midpoints=[{"token_id": "tok-1", "mid": "0.65"}],
        )
        positions = self.service._build_positions_from_trades(clob, self._trades())
        yes = next(p for p in positions if p["asset_id"] == "tok-1")
        self.assertEqual(yes["curPrice"], 0.65)

    def test_midpoint_failure_falls_back_to_last_trade_price(self):
        clob = self._clob(
            last_prices=[],
            midpoints=RuntimeError("mid failed"),
            last_price={"price": "0.8"},
        )
        positions = self.service._build_positions_from_trades(clob, self._trades())
        yes = next(p for p in positions if p["asset_id"] == "tok-1")
        self.assertEqual(yes["curPrice"], 0.8)

    def test_last_trade_price_non_dict_is_ignored(self):
        clob = self._clob(last_prices=[], midpoints=[], last_price="not-a-dict")
        positions = self.service._build_positions_from_trades(clob, self._trades())
        yes = next(p for p in positions if p["asset_id"] == "tok-1")
        # Falls all the way back to the average buy price.
        self.assertEqual(yes["curPrice"], 0.5)

    def test_last_trade_price_failure_falls_back_to_avg_price(self):
        clob = self._clob(last_prices=[], midpoints=[], last_price=RuntimeError("boom"))
        positions = self.service._build_positions_from_trades(clob, self._trades())
        yes = next(p for p in positions if p["asset_id"] == "tok-1")
        self.assertEqual(yes["curPrice"], 0.5)

    def test_position_without_asset_id_uses_avg_price(self):
        trades = [
            {
                "market": "cid-1",
                "outcome": "Yes",
                "side": "BUY",
                "size": "4",
                "price": "0.25",
            }
        ]
        clob = self._clob(last_prices=[])
        positions = self.service._build_positions_from_trades(clob, trades)
        self.assertEqual(len(positions), 1)
        self.assertIsNone(positions[0]["asset_id"])
        self.assertEqual(positions[0]["curPrice"], 0.25)

    def test_positions_sorted_by_invested_value_desc(self):
        trades = [
            {
                "market": "cid-1",
                "outcome": "Yes",
                "side": "BUY",
                "size": "2",
                "price": "0.5",
                "asset_id": "tok-1",
            },
            {
                "market": "cid-2",
                "outcome": "No",
                "side": "BUY",
                "size": "10",
                "price": "0.5",
                "asset_id": "tok-2",
            },
        ]
        clob = self._clob(last_prices=[])
        clob._markets["cid-2"] = {
            "question": "Q Two",
            "closed": False,
            "accepting_orders": True,
        }
        positions = self.service._build_positions_from_trades(clob, trades)
        self.assertEqual([p["condition_id"] for p in positions], ["cid-2", "cid-1"])

    def test_empty_trades(self):
        clob = self._clob()
        self.assertEqual(self.service._build_positions_from_trades(clob, []), [])

    def test_market_info_missing_uses_condition_id_as_title(self):
        trades = [
            {
                "market": "cid-x",
                "outcome": "Yes",
                "side": "BUY",
                "size": "3",
                "price": "0.5",
                "asset_id": "tok-x",
            }
        ]
        clob = self._clob(last_prices=[])
        positions = self.service._build_positions_from_trades(clob, trades)
        self.assertEqual(positions[0]["title"], "cid-x")
        self.assertEqual(positions[0]["market"], "cid-x")


class GetPositionsTests(_PolymarketServiceTestCase):
    """``get_positions`` authenticated path + Data API fallback."""

    def _patch_clob(self, clob):
        self.service._get_clob_client = MagicMock(return_value=clob)

    async def test_authenticated_path_builds_from_trades(self):
        clob = _FakeClob(
            markets={
                "cid-1": {
                    "question": "Q One",
                    "closed": False,
                    "accepting_orders": True,
                }
            },
            last_prices=[],
        )
        self._patch_clob(clob)
        trades = [
            {
                "market": "cid-1",
                "outcome": "Yes",
                "side": "BUY",
                "size": "5",
                "price": "0.5",
                "asset_id": "tok-1",
            }
        ]
        clob._trades = trades

        result = await self.service.get_positions(WALLET, private_key=PRIVATE_KEY)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["condition_id"], "cid-1")
        self.assertEqual(result[0]["size"], 5.0)

    async def test_authenticated_path_empty_trades_falls_back(self):
        clob = _FakeClob(trades=[])
        self._patch_clob(clob)
        self._patch_http(
            {
                "data-api.polymarket.com/positions": _FakeResponse(
                    200,
                    [
                        {
                            "size": "10",
                            "avgPrice": "0.5",
                            "curPrice": "0.7",
                            "title": "D",
                        }
                    ],
                )
            }
        )

        result = await self.service.get_positions(WALLET, private_key=PRIVATE_KEY)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "D")

    async def test_authenticated_path_client_failure_falls_back(self):
        self.service._get_clob_client = MagicMock(side_effect=RuntimeError("no creds"))
        self._patch_http(
            {
                "data-api.polymarket.com/positions": _FakeResponse(
                    200, [{"size": "1", "avgPrice": "0.5"}]
                )
            }
        )

        result = await self.service.get_positions(WALLET, private_key=PRIVATE_KEY)
        self.assertEqual(len(result), 1)

    async def test_authenticated_path_trades_failure_falls_back(self):
        clob = _FakeClob(trades=RuntimeError("trades failed"))
        self._patch_clob(clob)
        self._patch_http(
            {
                "data-api.polymarket.com/positions": _FakeResponse(
                    200, [{"size": "1", "avgPrice": "0.5"}]
                )
            }
        )

        result = await self.service.get_positions(WALLET, private_key=PRIVATE_KEY)
        self.assertEqual(len(result), 1)

    async def test_data_api_list_response(self):
        self._patch_http(
            {
                "data-api.polymarket.com/positions": _FakeResponse(
                    200,
                    [
                        {
                            "size": "10",
                            "avgPrice": "0.5",
                            "curPrice": "0.7",
                            "title": "A",
                            "outcome": "Yes",
                        }
                    ],
                )
            }
        )

        result = await self.service.get_positions(WALLET)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "A")
        url, params = self.http_calls[0]
        self.assertEqual(params, {"user": WALLET.lower()})

    async def test_data_api_dict_response_with_positions_key(self):
        self._patch_http(
            {
                "data-api.polymarket.com/positions": _FakeResponse(
                    200,
                    {"positions": [{"size": "10", "avgPrice": "0.5", "title": "B"}]},
                )
            }
        )

        result = await self.service.get_positions(WALLET)
        self.assertEqual(result[0]["title"], "B")

    async def test_data_api_dict_without_positions_key(self):
        self._patch_http({"data-api.polymarket.com/positions": _FakeResponse(200, {"other": []})})
        self.assertEqual(await self.service.get_positions(WALLET), [])

    async def test_data_api_non_200(self):
        self._patch_http({"data-api.polymarket.com/positions": _FakeResponse(500, None)})
        self.assertEqual(await self.service.get_positions(WALLET), [])

    async def test_data_api_exception(self):
        self._patch_http(
            {"data-api.polymarket.com/positions": _FakeResponse(200, RuntimeError("json boom"))}
        )
        self.assertEqual(await self.service.get_positions(WALLET), [])

    async def test_data_api_empty_list(self):
        self._patch_http({"data-api.polymarket.com/positions": _FakeResponse(200, [])})
        self.assertEqual(await self.service.get_positions(WALLET), [])


class GetTradeHistoryTests(_PolymarketServiceTestCase):
    """``get_trade_history`` authenticated + public fallbacks."""

    def _patch_clob(self, clob):
        self.service._get_clob_client = MagicMock(return_value=clob)

    async def test_authenticated_path(self):
        clob = _FakeClob(
            markets={"cid-1": {"question": "Q One", "market_slug": "q-one"}},
            trades=[
                {
                    "id": "t1",
                    "market": "cid-1",
                    "outcome": "Yes",
                    "side": "BUY",
                    "size": "5",
                    "price": "0.5",
                    "match_time": "1700000000",
                }
            ],
        )
        self._patch_clob(clob)

        result = await self.service.get_trade_history(WALLET, private_key=PRIVATE_KEY)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "t1")
        self.assertEqual(result[0]["market"], "Q One")
        self.assertEqual(result[0]["market_slug"], "q-one")
        self.assertEqual(result[0]["side"], "BUY")

    async def test_authenticated_path_failure_falls_back(self):
        self.service._get_clob_client = MagicMock(side_effect=RuntimeError("auth failed"))
        self._patch_http(
            {
                f"{POLYMARKET_CLOB_API}/data/trades": _FakeResponse(
                    200,
                    [
                        {
                            "id": "f1",
                            "market": "M",
                            "side": "buy",
                            "size": "2",
                            "price": "0.5",
                        }
                    ],
                )
            }
        )

        result = await self.service.get_trade_history(WALLET, private_key=PRIVATE_KEY)

        self.assertEqual([t["id"] for t in result], ["f1"])
        self.assertEqual(result[0]["side"], "BUY")
        self.assertEqual(result[0]["type"], "TRADE")
        self.assertEqual(result[0]["status"], "FILLED")

    async def test_first_endpoint_empty_falls_through_to_second(self):
        self._patch_http(
            {
                f"{POLYMARKET_CLOB_API}/data/trades": _FakeResponse(200, []),
                f"{POLYMARKET_CLOB_API}/data/activity": _FakeResponse(
                    200,
                    [
                        {
                            "id": "a1",
                            "title": "T",
                            "side": "sell",
                            "size": "1",
                            "price": "0.4",
                            "type": "FILL",
                            "status": "done",
                            "createdAt": "2026-01-01",
                            "feeAmount": "0.01",
                            "pnl": "1.5",
                        }
                    ],
                ),
            }
        )

        result = await self.service.get_trade_history(WALLET)

        self.assertEqual([t["id"] for t in result], ["a1"])
        self.assertEqual(result[0]["market"], "T")
        self.assertEqual(result[0]["type"], "FILL")
        self.assertEqual(result[0]["status"], "DONE")
        self.assertEqual(result[0]["timestamp"], "2026-01-01")
        self.assertEqual(result[0]["fee"], 0.01)
        self.assertEqual(result[0]["pnl"], 1.5)

    async def test_first_endpoint_non_200_uses_second(self):
        self._patch_http(
            {
                f"{POLYMARKET_CLOB_API}/data/trades": _FakeResponse(500, None),
                f"{POLYMARKET_CLOB_API}/data/activity": _FakeResponse(
                    200, [{"id": "a2", "size": "1", "price": "0.5"}]
                ),
            }
        )
        result = await self.service.get_trade_history(WALLET)
        self.assertEqual([t["id"] for t in result], ["a2"])

    async def test_payload_dict_with_trades_key(self):
        self._patch_http(
            {
                f"{POLYMARKET_CLOB_API}/data/trades": _FakeResponse(
                    200,
                    {"trades": [{"id": "d1", "size": "1", "price": "0.5"}]},
                )
            }
        )
        result = await self.service.get_trade_history(WALLET)
        self.assertEqual([t["id"] for t in result], ["d1"])

    async def test_payload_dict_with_data_key(self):
        self._patch_http(
            {
                f"{POLYMARKET_CLOB_API}/data/trades": _FakeResponse(
                    200,
                    {"data": [{"id": "d2", "size": "1", "price": "0.5"}]},
                )
            }
        )
        result = await self.service.get_trade_history(WALLET)
        self.assertEqual([t["id"] for t in result], ["d2"])

    async def test_duplicate_ids_are_deduplicated(self):
        self._patch_http(
            {
                f"{POLYMARKET_CLOB_API}/data/trades": _FakeResponse(
                    200,
                    [
                        {"id": "dup", "size": "1", "price": "0.5"},
                        {"id": "dup", "size": "2", "price": "0.6"},
                        {"id": "other", "size": "1", "price": "0.5"},
                    ],
                )
            }
        )
        result = await self.service.get_trade_history(WALLET)
        self.assertEqual([t["id"] for t in result], ["dup", "other"])

    async def test_non_dict_items_are_skipped(self):
        self._patch_http(
            {
                f"{POLYMARKET_CLOB_API}/data/trades": _FakeResponse(
                    200, ["junk", 42, {"id": "ok", "size": "1", "price": "0.5"}]
                )
            }
        )
        result = await self.service.get_trade_history(WALLET)
        self.assertEqual([t["id"] for t in result], ["ok"])

    async def test_pnl_none_when_absent(self):
        self._patch_http(
            {
                f"{POLYMARKET_CLOB_API}/data/trades": _FakeResponse(
                    200, [{"id": "p", "size": "1", "price": "0.5"}]
                )
            }
        )
        result = await self.service.get_trade_history(WALLET)
        self.assertIsNone(result[0]["pnl"])

    async def test_all_endpoints_fail_derives_from_positions(self):
        self._patch_http(
            {
                f"{POLYMARKET_CLOB_API}/data/trades": _FakeResponse(500, None),
                f"{POLYMARKET_CLOB_API}/data/activity": _FakeResponse(500, None),
            }
        )
        positions = [
            {
                "id": "p1",
                "title": "T",
                "market": "M",
                "outcome": "Yes",
                "size": "10",
                "avgPrice": "0.5",
                "pnl": "1.5",
                "updatedAt": "2026-01-02",
            }
        ]
        self.service.get_positions = AsyncMock(return_value=positions)

        result = await self.service.get_trade_history(WALLET)

        self.service.get_positions.assert_awaited_once_with(WALLET)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "p1")
        self.assertEqual(result[0]["market"], "T")
        self.assertEqual(result[0]["side"], "BUY")
        self.assertEqual(result[0]["size"], 10.0)
        self.assertEqual(result[0]["price"], 0.5)
        self.assertEqual(result[0]["type"], "POSITION")
        self.assertEqual(result[0]["status"], "OPEN")
        self.assertEqual(result[0]["timestamp"], "2026-01-02")
        self.assertEqual(result[0]["fee"], 0.0)
        self.assertEqual(result[0]["pnl"], 1.5)

    async def test_positions_derived_trades_sorted_desc(self):
        self._patch_http(
            {
                f"{POLYMARKET_CLOB_API}/data/trades": _FakeResponse(200, []),
                f"{POLYMARKET_CLOB_API}/data/activity": _FakeResponse(200, []),
            }
        )
        positions = [
            {
                "title": "Old",
                "size": "1",
                "avgPrice": "0.5",
                "pnl": 0.0,
                "createdAt": "2026-01-01",
            },
            {
                "title": "New",
                "size": "1",
                "avgPrice": "0.5",
                "pnl": 0.0,
                "createdAt": "2026-01-03",
            },
        ]
        self.service.get_positions = AsyncMock(return_value=positions)

        result = await self.service.get_trade_history(WALLET)
        self.assertEqual([t["market"] for t in result], ["New", "Old"])

    async def test_positions_failure_returns_empty(self):
        self._patch_http(
            {
                f"{POLYMARKET_CLOB_API}/data/trades": _FakeResponse(200, []),
                f"{POLYMARKET_CLOB_API}/data/activity": _FakeResponse(200, []),
            }
        )
        self.service.get_positions = AsyncMock(side_effect=RuntimeError("positions down"))
        self.assertEqual(await self.service.get_trade_history(WALLET), [])

    async def test_limit_is_applied(self):
        self._patch_http(
            {
                f"{POLYMARKET_CLOB_API}/data/trades": _FakeResponse(
                    200,
                    [{"id": f"t{i}", "size": "1", "price": "0.5"} for i in range(10)],
                )
            }
        )
        result = await self.service.get_trade_history(WALLET, limit=3)
        self.assertEqual(len(result), 3)

    async def test_endpoint_exception_continues_to_next(self):
        def boom(url, params):
            if "trades" in url:
                raise RuntimeError("endpoint down")
            return _FakeResponse(200, [{"id": "a9", "size": "1", "price": "0.5"}])

        self._patch_http(
            {
                f"{POLYMARKET_CLOB_API}/data/trades": boom,
                f"{POLYMARKET_CLOB_API}/data/activity": boom,
            }
        )
        result = await self.service.get_trade_history(WALLET)
        self.assertEqual([t["id"] for t in result], ["a9"])

    async def test_both_endpoints_empty_and_no_positions(self):
        self._patch_http(
            {
                f"{POLYMARKET_CLOB_API}/data/trades": _FakeResponse(200, []),
                f"{POLYMARKET_CLOB_API}/data/activity": _FakeResponse(200, []),
            }
        )
        self.service.get_positions = AsyncMock(return_value=[])
        self.assertEqual(await self.service.get_trade_history(WALLET), [])


class GetActiveMarketsTests(_PolymarketServiceTestCase):
    """``get_active_markets`` Gamma normalisation and caching."""

    def _events(self):
        return [
            {
                "title": "Event A",
                "slug": "event-a",
                "image": "img-a",
                "volume": 1000,
                "liquidity": 500,
                "volume24hr": 100,
                "tags": [{"slug": "sports"}],
                "markets": [
                    {
                        "question": "Q A",
                        "conditionId": "cid-a",
                        "endDateIso": "2026-01-01",
                        "bestAsk": "0.6",
                        "bestBid": "0.55",
                        "lastTradePrice": "0.58",
                    },
                    {
                        "question": "Q B",
                        "conditionId": "cid-b",
                        "bestBid": "0.3",
                    },
                    {
                        "question": "Q C",
                        "conditionId": "cid-c",
                        "lastTradePrice": "0.45",
                    },
                    {
                        "question": "Q D",
                        "conditionId": "cid-d",
                        "outcomePrices": '["0.2", "0.8"]',
                    },
                    {
                        "question": "Q E",
                        "conditionId": "cid-e",
                        "outcomePrices": [0.25, 0.75],
                    },
                    {
                        "question": "Q F",
                        "conditionId": "cid-f",
                        "outcomePrices": "not-json",
                    },
                    {
                        "question": "Q G",
                        "conditionId": "cid-g",
                        "outcomePrices": ["a", "b"],
                    },
                    {
                        "question": "Q H",
                        "conditionId": "cid-h",
                        "outcomePrices": ["1.0", "0.0"],
                    },
                    {
                        "question": "Q I",
                        "conditionId": "cid-i",
                        "outcomePrices": ["0.0", "1.0"],
                    },
                    {
                        "question": "Q J",
                        "conditionId": "cid-j",
                        "groupItemTitle": "Before July 2026",
                        "bestAsk": "0.5",
                    },
                ],
            },
            {"title": "No markets", "slug": "e-nm", "markets": []},
        ]

    def _patch_events(self, events, status_code=200):
        self._patch_http({f"{POLYMARKET_GAMMA_API}/events": _FakeResponse(status_code, events)})

    async def test_normalises_and_filters_markets(self):
        self._patch_events(self._events())

        result = await self.service.get_active_markets()

        # Q H (resolved yes) and Q I (resolved no) are filtered.
        self.assertEqual(
            [m["question"] for m in result],
            ["Q A", "Q B", "Q C", "Q D", "Q E", "Q F", "Q G", "Q J"],
        )

        a = result[0]
        self.assertEqual(a["outcomePrices"], ["0.6", "0.4"])
        self.assertEqual(a["bestAsk"], "0.6")
        self.assertEqual(a["bestBid"], "0.55")
        self.assertEqual(a["lastTradePrice"], "0.58")
        self.assertEqual(a["condition_id"], "cid-a")
        self.assertEqual(a["end_date_iso"], "2026-01-01")
        self.assertEqual(a["_event_title"], "Event A")
        self.assertEqual(a["_event_slug"], "event-a")
        self.assertEqual(a["_event_image"], "img-a")
        self.assertEqual(a["_event_volume"], 1000)
        self.assertEqual(a["_event_liquidity"], 500)
        self.assertEqual(a["_event_volume_24hr"], 100)
        self.assertEqual(a["_event_tags"], [{"slug": "sports"}])
        self.assertEqual(a["groupItemTitle"], "")

        # bestBid fallback
        self.assertEqual(result[1]["outcomePrices"], ["0.3", "0.7"])
        # lastTradePrice fallback
        self.assertEqual(result[2]["outcomePrices"], ["0.45", "0.55"])
        # outcomePrices as a JSON string
        self.assertEqual(result[3]["outcomePrices"], ["0.2", "0.8"])
        # outcomePrices as a list
        self.assertEqual(result[4]["outcomePrices"], ["0.25", "0.75"])
        # invalid JSON string falls back to 0.5/0.5
        self.assertEqual(result[5]["outcomePrices"], ["0.5", "0.5"])
        # non-numeric list falls back to 0.5/0.5
        self.assertEqual(result[6]["outcomePrices"], ["0.5", "0.5"])
        # existing groupItemTitle is preserved
        self.assertEqual(result[7]["groupItemTitle"], "Before July 2026")

    async def test_result_is_cached_with_60s_ttl(self):
        self._patch_events(self._events())

        first = await self.service.get_active_markets()
        second = await self.service.get_active_markets()

        self.assertIs(first, second)
        self.assertEqual(len(self.http_instances), 1)
        self.assertEqual(
            self.cache.set_calls_for("active_markets:60"),
            [("active_markets:60", first, 60)],
        )

    async def test_cache_hit_short_circuits_http(self):
        cached = [{"question": "Cached"}]
        self.cache.store["active_markets:60"] = cached

        result = await self.service.get_active_markets()

        self.assertIs(result, cached)
        self.assertEqual(self.http_instances, [])

    async def test_empty_list_is_cached_and_served(self):
        self._patch_events([])

        first = await self.service.get_active_markets()
        second = await self.service.get_active_markets()

        self.assertEqual(first, [])
        self.assertIs(first, second)

    async def test_limit_truncates(self):
        self._patch_events(self._events())
        result = await self.service.get_active_markets(limit=2)
        self.assertEqual(len(result), 2)
        self.assertEqual(self.cache.set_calls_for("active_markets:2")[0][2], 60)

    async def test_non_200_returns_empty(self):
        self._patch_events([], status_code=500)
        self.assertEqual(await self.service.get_active_markets(), [])
        self.assertEqual(self.cache.set_calls, [])

    async def test_non_list_payload_returns_empty(self):
        self._patch_events({"events": []})
        self.assertEqual(await self.service.get_active_markets(), [])

    async def test_exception_returns_empty(self):
        self._patch_http(
            {f"{POLYMARKET_GAMMA_API}/events": _FakeResponse(200, RuntimeError("json boom"))}
        )
        self.assertEqual(await self.service.get_active_markets(), [])

    async def test_non_numeric_best_ask_returns_empty(self):
        # float(best_ask) raises for a non-numeric string and the
        # whole fetch is abandoned (documented current behaviour).
        self._patch_events(
            [
                {
                    "title": "E",
                    "markets": [{"question": "Q", "bestAsk": "abc"}],
                }
            ]
        )
        self.assertEqual(await self.service.get_active_markets(), [])

    async def test_last_trade_price_outside_unit_range_is_ignored(self):
        self._patch_events(
            [
                {
                    "title": "E",
                    "markets": [{"question": "Q", "lastTradePrice": "1.5"}],
                }
            ]
        )
        result = await self.service.get_active_markets()
        self.assertEqual(result[0]["outcomePrices"], ["0.5", "0.5"])


class GetNewestMarketsTests(_PolymarketServiceTestCase):
    """``get_newest_markets`` noise filtering and caching."""

    def _events(self):
        return [
            {
                "title": "BTC up or down 5m",
                "liquidity": 1000,
                "startDate": "2026-01-01",
                "markets": [{"question": "noise", "bestAsk": "0.5"}],
            },
            {
                "title": "ETH updown",
                "liquidity": 1000,
                "markets": [{"question": "noise2", "bestAsk": "0.5"}],
            },
            {
                "title": "Thin",
                "liquidity": 10,
                "markets": [{"question": "thin", "bestAsk": "0.5"}],
            },
            {
                "title": "Good",
                "slug": "good",
                "image": "img",
                "volume": 1,
                "liquidity": 100,
                "volume24hr": 2,
                "tags": [],
                "startDate": "2026-02-01",
                "markets": [
                    {
                        "question": "Good Q",
                        "conditionId": "cid-good",
                        "bestAsk": "0.55",
                    }
                ],
            },
        ]

    async def test_noise_events_are_filtered(self):
        self._patch_http({f"{POLYMARKET_GAMMA_API}/events": _FakeResponse(200, self._events())})

        result = await self.service.get_newest_markets()

        self.assertEqual(len(result), 1)
        m = result[0]
        self.assertEqual(m["question"], "Good Q")
        self.assertEqual(m["outcomePrices"], ["0.55", "0.45"])
        self.assertEqual(m["_event_title"], "Good")
        self.assertEqual(m["_event_start_date"], "2026-02-01")
        self.assertEqual(m["_event_slug"], "good")
        self.assertEqual(m["_event_image"], "img")
        self.assertEqual(m["_event_volume"], 1)
        self.assertEqual(m["_event_liquidity"], 100)
        self.assertEqual(m["_event_volume_24hr"], 2)
        self.assertEqual(m["_event_tags"], [])
        self.assertEqual(m["condition_id"], "cid-good")

    async def test_result_is_cached_with_60s_ttl(self):
        self._patch_http({f"{POLYMARKET_GAMMA_API}/events": _FakeResponse(200, self._events())})

        first = await self.service.get_newest_markets()
        second = await self.service.get_newest_markets()

        self.assertIs(first, second)
        self.assertEqual(len(self.http_instances), 1)
        self.assertEqual(
            self.cache.set_calls_for("newest_markets:60"),
            [("newest_markets:60", first, 60)],
        )

    async def test_cache_hit_short_circuits_http(self):
        cached = [{"question": "Cached"}]
        self.cache.store["newest_markets:60"] = cached
        result = await self.service.get_newest_markets()
        self.assertIs(result, cached)
        self.assertEqual(self.http_instances, [])

    async def test_non_200_returns_empty(self):
        self._patch_http({f"{POLYMARKET_GAMMA_API}/events": _FakeResponse(503, None)})
        self.assertEqual(await self.service.get_newest_markets(), [])

    async def test_non_list_payload_returns_empty(self):
        self._patch_http({f"{POLYMARKET_GAMMA_API}/events": _FakeResponse(200, {"data": []})})
        self.assertEqual(await self.service.get_newest_markets(), [])

    async def test_exception_returns_empty(self):
        self._patch_http(
            {f"{POLYMARKET_GAMMA_API}/events": _FakeResponse(200, RuntimeError("boom"))}
        )
        self.assertEqual(await self.service.get_newest_markets(), [])

    async def test_event_without_markets_is_skipped(self):
        events = [
            {"title": "Empty", "liquidity": 100, "markets": []},
            {
                "title": "Good",
                "liquidity": 100,
                "markets": [
                    {
                        "question": "Good Q",
                        "conditionId": "cid-good",
                        "bestAsk": "0.55",
                    }
                ],
            },
        ]
        self._patch_http({f"{POLYMARKET_GAMMA_API}/events": _FakeResponse(200, events)})
        result = await self.service.get_newest_markets()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["question"], "Good Q")

    async def test_price_building_branches(self):
        events = [
            {
                "title": "Good",
                "liquidity": 100,
                "markets": [
                    # bestBid fallback + endDateIso mapping.
                    {
                        "question": "QB",
                        "conditionId": "cid-b",
                        "bestBid": "0.4",
                        "endDateIso": "2026-03-01",
                    },
                    # lastTradePrice fallback.
                    {
                        "question": "QL",
                        "conditionId": "cid-l",
                        "lastTradePrice": "0.3",
                    },
                    # outcomePrices as a JSON string.
                    {
                        "question": "QS",
                        "conditionId": "cid-s",
                        "outcomePrices": '["0.2", "0.8"]',
                    },
                    # outcomePrices as a list.
                    {
                        "question": "QP",
                        "conditionId": "cid-p",
                        "outcomePrices": [0.25, 0.75],
                    },
                    # outcomePrices as invalid JSON.
                    {
                        "question": "QI",
                        "conditionId": "cid-i",
                        "outcomePrices": "junk",
                    },
                    # outcomePrices as a non-numeric list.
                    {
                        "question": "QN",
                        "conditionId": "cid-n",
                        "outcomePrices": ["a", "b"],
                    },
                ],
            }
        ]
        self._patch_http({f"{POLYMARKET_GAMMA_API}/events": _FakeResponse(200, events)})

        result = await self.service.get_newest_markets()

        by_question = {m["question"]: m for m in result}
        self.assertEqual(by_question["QB"]["outcomePrices"], ["0.4", "0.6"])
        self.assertEqual(by_question["QB"]["end_date_iso"], "2026-03-01")
        self.assertEqual(by_question["QL"]["outcomePrices"], ["0.3", "0.7"])
        self.assertEqual(by_question["QS"]["outcomePrices"], ["0.2", "0.8"])
        self.assertEqual(by_question["QP"]["outcomePrices"], ["0.25", "0.75"])
        self.assertEqual(by_question["QI"]["outcomePrices"], ["0.5", "0.5"])
        self.assertEqual(by_question["QN"]["outcomePrices"], ["0.5", "0.5"])

    async def test_limit_truncates(self):
        events = [
            {
                "title": f"E{i}",
                "liquidity": 100,
                "markets": [
                    {
                        "question": f"Q{i}",
                        "conditionId": f"cid-{i}",
                        "bestAsk": "0.5",
                    }
                ],
            }
            for i in range(5)
        ]
        self._patch_http({f"{POLYMARKET_GAMMA_API}/events": _FakeResponse(200, events)})
        result = await self.service.get_newest_markets(limit=2)
        self.assertEqual(len(result), 2)


class GetHighPnlMarketsTests(_PolymarketServiceTestCase):
    """``get_high_pnl_markets`` filtering and sorting."""

    async def test_filters_and_sorts_by_pnl_potential(self):
        markets = [
            {"question": "A", "outcomePrices": ["0.3", "0.7"]},
            {"question": "B", "outcomePrices": ["0.95", "0.05"]},
            {"question": "C", "outcomePrices": ["0.1", "0.2"]},
            {"question": "D", "outcomePrices": ["0.5"]},
            {"question": "E", "outcomePrices": []},
            {"question": "F", "outcomePrices": ["abc", "0.5"]},
        ]
        self.service.get_active_markets = AsyncMock(return_value=markets)

        result = await self.service.get_high_pnl_markets(limit=10)

        # B: yes 0.95 >= 0.90 → dropped.  A (0.7), C (0.9) and
        # D / E / F (0.5 each) remain — E's missing NO price
        # defaults to 1 - yes_p, not 1.0.
        self.assertEqual([m["question"] for m in result], ["C", "A", "D", "E", "F"])
        self.assertEqual(result[0]["pnl_potential"], 0.9)
        self.assertEqual(result[1]["pnl_potential"], 0.7)
        self.assertEqual(result[2]["pnl_potential"], 0.5)
        self.service.get_active_markets.assert_awaited_once_with(limit=20)

    async def test_limit_truncates(self):
        markets = [{"question": f"Q{i}", "outcomePrices": ["0.1", "0.2"]} for i in range(5)]
        self.service.get_active_markets = AsyncMock(return_value=markets)
        result = await self.service.get_high_pnl_markets(limit=2)
        self.assertEqual(len(result), 2)

    async def test_empty_upstream(self):
        self.service.get_active_markets = AsyncMock(return_value=[])
        self.assertEqual(await self.service.get_high_pnl_markets(), [])


class GetSmartMoneyAnalysisTests(_PolymarketServiceTestCase):
    """``get_smart_money_analysis`` whale detection."""

    def _stats(self, top_traders):
        return {
            "yes_traders": 5,
            "no_traders": 3,
            "yes_volume": 1000.0,
            "no_volume": 500.0,
            "side_ratio": {"yes": 66.7, "no": 33.3},
            "top_traders": top_traders,
        }

    def _trader(self, address, total, yes, no, lean):
        return {
            "short_address": address,
            "total_volume": total,
            "yes_volume": yes,
            "no_volume": no,
            "lean": lean,
        }

    async def test_mixed_bias(self):
        stats = self._stats(
            [
                self._trader("@0.600", 1000, 800, 0, "YES"),
                self._trader("@0.700", 600, 0, 600, "NO"),
                self._trader("@0.500", 100, 100, 0, "YES"),
            ]
        )
        self.service.get_market_trader_stats = AsyncMock(return_value=stats)

        result = await self.service.get_smart_money_analysis("cid-1")

        self.assertEqual(result["condition_id"], "cid-1")
        # Whales: the two orders above $500.
        self.assertEqual(result["whale_count"], 2)
        self.assertEqual(result["total_whale_volume"], 1400.0)
        self.assertEqual(result["yes_whale_pct"], 57.1)
        self.assertEqual(result["no_whale_pct"], 42.9)
        self.assertEqual(result["whale_bias"], "MIXED")
        self.assertEqual(result["yes_traders"], 5)
        self.assertEqual(result["no_traders"], 3)
        self.assertEqual(result["yes_volume"], 1000.0)
        self.assertEqual(result["no_volume"], 500.0)
        self.assertEqual(result["side_ratio"], {"yes": 66.7, "no": 33.3})
        self.assertEqual(len(result["top_traders"]), 3)
        self.assertIn("@0.600", result["context_text"])
        self.assertIn("Whale orders", result["context_text"])
        self.assertIn("YES whale volume", result["context_text"])
        self.assertIn("Order book participants", result["context_text"])

    async def test_yes_bias(self):
        stats = self._stats(
            [
                self._trader("@0.600", 1000, 800, 0, "YES"),
                self._trader("@0.700", 600, 100, 0, "YES"),
            ]
        )
        self.service.get_market_trader_stats = AsyncMock(return_value=stats)
        result = await self.service.get_smart_money_analysis("cid-1")
        self.assertEqual(result["whale_bias"], "YES")
        self.assertEqual(result["yes_whale_pct"], 100.0)
        self.assertEqual(result["no_whale_pct"], 0.0)

    async def test_no_bias(self):
        stats = self._stats(
            [
                self._trader("@0.600", 1000, 0, 800, "NO"),
                self._trader("@0.700", 600, 0, 100, "NO"),
            ]
        )
        self.service.get_market_trader_stats = AsyncMock(return_value=stats)
        result = await self.service.get_smart_money_analysis("cid-1")
        self.assertEqual(result["whale_bias"], "NO")

    async def test_no_whales_defaults_to_fifty_fifty(self):
        stats = self._stats([self._trader("@0.500", 100, 100, 0, "YES")])
        self.service.get_market_trader_stats = AsyncMock(return_value=stats)
        result = await self.service.get_smart_money_analysis("cid-1")
        self.assertEqual(result["whale_count"], 0)
        self.assertEqual(result["total_whale_volume"], 0.0)
        self.assertEqual(result["yes_whale_pct"], 50.0)
        self.assertEqual(result["no_whale_pct"], 50.0)
        self.assertEqual(result["whale_bias"], "MIXED")

    async def test_top_traders_capped_at_six(self):
        traders = [self._trader(f"@{i}", 1000 + i, 1000, 0, "YES") for i in range(10)]
        stats = self._stats(traders)
        self.service.get_market_trader_stats = AsyncMock(return_value=stats)
        result = await self.service.get_smart_money_analysis("cid-1")
        self.assertEqual(len(result["top_traders"]), 6)
        # The context text lists up to eight.
        self.assertEqual(result["context_text"].count("  - @"), 8)

    async def test_empty_stats(self):
        self.service.get_market_trader_stats = AsyncMock(return_value=self._stats([]))
        result = await self.service.get_smart_money_analysis("cid-1")
        self.assertEqual(result["whale_count"], 0)
        self.assertEqual(result["top_traders"], [])


class GetMarketCategoriesTests(_PolymarketServiceTestCase):
    """``get_market_categories`` returns the static catalog."""

    def test_returns_all_categories(self):
        categories = self.service.get_market_categories()
        self.assertIs(categories, PolymarketService.MARKET_CATEGORIES)
        self.assertGreater(len(categories), 0)
        for category in categories:
            self.assertIn("id", category)
            self.assertIn("label", category)


class GetMarketsByCategoryTests(_PolymarketServiceTestCase):
    """``get_markets_by_category`` Gamma + CLOB fallback."""

    def _gamma_events(self):
        return [
            {
                "title": "Sports Event",
                "slug": "sports-event",
                "tags": [{"slug": "nba"}],
                "markets": [
                    {
                        "question": "Q1",
                        "conditionId": "cid-1",
                        "bestAsk": "0.6",
                        "clobTokenIds": '["t1","t2"]',
                        "outcomes": '["Yes","No"]',
                    },
                    {
                        "question": "Q2",
                        "conditionId": "cid-2",
                        "bestAsk": "0.995",
                    },
                ],
            },
            {
                "title": "Politics Event",
                "tags": [{"slug": "politics"}],
                "markets": [{"question": "Q3"}],
            },
            {
                "title": "No markets",
                "tags": [{"slug": "nba"}],
                "markets": [],
            },
        ]

    def _patch_gamma(self, events, status_code=200):
        self._patch_http({f"{POLYMARKET_GAMMA_API}/events": _FakeResponse(status_code, events)})

    async def test_tag_filter_and_normalisation(self):
        events = self._gamma_events()
        self._patch_gamma(events)

        result = await self.service.get_markets_by_category("sports", limit=20)

        # Only the NBA event matches the sports tag; Q2 is resolved
        # (p0 >= 0.995); the market-less event is kept as-is.
        self.assertEqual(len(result), 2)
        q1, event = result

        self.assertEqual(q1["question"], "Q1")
        self.assertEqual(q1["condition_id"], "cid-1")
        self.assertEqual(q1["clob_token_ids"], '["t1","t2"]')
        self.assertEqual(
            q1["tokens"],
            [
                {"token_id": "t1", "outcome": "Yes"},
                {"token_id": "t2", "outcome": "No"},
            ],
        )
        self.assertEqual(q1["outcomePrices"], ["0.6", "0.4"])
        self.assertEqual(q1["_event_title"], "Sports Event")
        self.assertEqual(q1["_event_slug"], "sports-event")

        self.assertIs(event, events[2])

    async def test_best_bid_and_last_trade_fallbacks(self):
        events = [
            {
                "title": "E",
                "tags": [{"slug": "nba"}],
                "markets": [
                    {"question": "QB", "bestBid": "0.4"},
                    {"question": "QL", "lastTradePrice": "0.3"},
                    {"question": "QS", "outcomePrices": '["0.2","0.8"]'},
                    {"question": "QI", "outcomePrices": "junk"},
                ],
            }
        ]
        self._patch_gamma(events)

        result = await self.service.get_markets_by_category("sports")

        self.assertEqual(result[0]["outcomePrices"], ["0.4", "0.6"])
        self.assertEqual(result[1]["outcomePrices"], ["0.3", "0.7"])
        self.assertEqual(result[2]["outcomePrices"], ["0.2", "0.8"])
        self.assertEqual(result[3]["outcomePrices"], ["0.5", "0.5"])

    async def test_no_matching_events_returns_empty(self):
        # Gamma returned 200, so the (possibly empty) result is
        # returned directly — the CLOB fallback only runs when
        # Gamma itself fails.
        self._patch_gamma(
            [{"title": "Politics", "tags": [{"slug": "politics"}], "markets": [{"question": "Q"}]}]
        )

        result = await self.service.get_markets_by_category("sports")
        self.assertEqual(result, [])

    async def test_gamma_non_200_falls_back_to_clob_list(self):
        self._patch_gamma([], status_code=500)
        self._patch_http(
            {f"{POLYMARKET_CLOB_API}/markets": _FakeResponse(200, [{"question": "CLOB Q"}])}
        )
        result = await self.service.get_markets_by_category("sports")
        self.assertEqual(result, [{"question": "CLOB Q"}])

    async def test_clob_dict_with_data_key(self):
        self._patch_gamma([], status_code=500)
        self._patch_http(
            {f"{POLYMARKET_CLOB_API}/markets": _FakeResponse(200, {"data": [{"question": "D"}]})}
        )
        result = await self.service.get_markets_by_category("sports")
        self.assertEqual(result, [{"question": "D"}])

    async def test_clob_dict_with_markets_key(self):
        self._patch_gamma([], status_code=500)
        self._patch_http(
            {f"{POLYMARKET_CLOB_API}/markets": _FakeResponse(200, {"markets": [{"question": "M"}]})}
        )
        result = await self.service.get_markets_by_category("sports")
        self.assertEqual(result, [{"question": "M"}])

    async def test_clob_empty_dict_returns_empty(self):
        self._patch_gamma([], status_code=500)
        self._patch_http({f"{POLYMARKET_CLOB_API}/markets": _FakeResponse(200, {})})
        self.assertEqual(await self.service.get_markets_by_category("sports"), [])

    async def test_clob_non_200_returns_empty(self):
        self._patch_gamma([], status_code=500)
        self._patch_http({f"{POLYMARKET_CLOB_API}/markets": _FakeResponse(500, None)})
        self.assertEqual(await self.service.get_markets_by_category("sports"), [])

    async def test_exception_returns_empty(self):
        self._patch_http(
            {f"{POLYMARKET_GAMMA_API}/events": _FakeResponse(200, RuntimeError("boom"))}
        )
        self.assertEqual(await self.service.get_markets_by_category("sports"), [])

    async def test_end_date_iso_and_invalid_outcome_prices(self):
        events = [
            {
                "title": "E",
                "tags": [{"slug": "nba"}],
                "markets": [
                    {
                        "question": "QD",
                        "conditionId": "cid-d",
                        "bestAsk": "0.5",
                        "endDateIso": "2026-04-01",
                    },
                    {
                        # No book prices → the outcomePrices
                        # fallback path processes the raw list.
                        "question": "QN",
                        "conditionId": "cid-n",
                        "outcomePrices": ["a", "b"],
                    },
                ],
            }
        ]
        self._patch_gamma(events)

        result = await self.service.get_markets_by_category("sports")

        self.assertEqual(result[0]["end_date_iso"], "2026-04-01")
        # Non-numeric outcomePrices fall back to 0.5/0.5.
        self.assertEqual(result[1]["outcomePrices"], ["0.5", "0.5"])

    async def test_limit_truncates(self):
        events = [
            {
                "title": "E",
                "tags": [{"slug": "nba"}],
                "markets": [
                    {
                        "question": f"Q{i}",
                        "conditionId": f"cid-{i}",
                        "bestAsk": "0.5",
                    }
                    for i in range(10)
                ],
            }
        ]
        self._patch_gamma(events)
        result = await self.service.get_markets_by_category("sports", limit=3)
        self.assertEqual(len(result), 3)


class GetHttpClientTests(_PolymarketServiceTestCase):
    """``_get_http_client`` shared-client lifecycle."""

    def setUp(self):
        super().setUp()
        saved = PolymarketService._shared_http_client
        self.addCleanup(lambda: setattr(PolymarketService, "_shared_http_client", saved))
        PolymarketService._shared_http_client = None

    def test_creates_client_when_none(self):
        sentinel = MagicMock()
        factory = MagicMock(return_value=sentinel)
        fake_httpx = MagicMock()
        fake_httpx.AsyncClient = factory

        with patch.object(polymarket_service, "httpx", fake_httpx):
            client = self.service._get_http_client()

        self.assertIs(client, sentinel)
        self.assertIs(PolymarketService._shared_http_client, sentinel)
        factory.assert_called_once()

    def test_reuses_existing_client(self):
        sentinel = MagicMock()
        sentinel.is_closed = False
        PolymarketService._shared_http_client = sentinel
        factory = MagicMock()
        fake_httpx = MagicMock()
        fake_httpx.AsyncClient = factory

        with patch.object(polymarket_service, "httpx", fake_httpx):
            client = self.service._get_http_client()

        self.assertIs(client, sentinel)
        factory.assert_not_called()

    def test_recreates_closed_client(self):
        closed = MagicMock()
        closed.is_closed = True
        PolymarketService._shared_http_client = closed
        fresh = MagicMock()
        fresh.is_closed = False
        factory = MagicMock(return_value=fresh)
        fake_httpx = MagicMock()
        fake_httpx.AsyncClient = factory

        with patch.object(polymarket_service, "httpx", fake_httpx):
            client = self.service._get_http_client()

        self.assertIs(client, fresh)
        factory.assert_called_once()


class SearchAllMarketsTests(_PolymarketServiceTestCase):
    """``search_all_markets`` pagination, dedup and caching."""

    def _event(self, idx, question, cid, best_ask="0.5"):
        return {
            "id": f"evt-{idx}",
            "slug": f"evt-{idx}-slug",
            "title": f"Event {idx}",
            "tags": [{"slug": "sports"}],
            "markets": [
                {
                    "question": question,
                    "conditionId": cid,
                    "bestAsk": best_ask,
                }
            ],
        }

    def _single_page(self, events):
        def handler(url, params):
            return _FakeResponse(200, events)

        return handler

    async def test_single_page_fetch_and_cache(self):
        events = [self._event(1, "Will A win?", "cid-1")]
        self._patch_http({f"{POLYMARKET_GAMMA_API}/events": self._single_page(events)})

        result = await self.service.search_all_markets()

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["offset"], 0)
        self.assertFalse(result["has_more"])
        self.assertEqual(result["markets"][0]["question"], "Will A win?")
        self.assertEqual(result["markets"][0]["outcomePrices"], ["0.5", "0.5"])
        # Cached for 30s under the (sort, tag) key.
        self.assertEqual(
            self.cache.set_calls_for("search_all_markets:volume24hr:"),
            [("search_all_markets:volume24hr:", result["markets"], 30)],
        )

    async def test_cache_hit_skips_http(self):
        cached = [{"question": "Cached"}]
        self.cache.store["search_all_markets:volume24hr:"] = cached

        result = await self.service.search_all_markets()

        self.assertEqual(result["markets"], cached)
        self.assertEqual(result["total"], 1)
        self.assertEqual(self.http_instances, [])

    async def test_text_query_filters_cached_list(self):
        cached = [
            {"question": "Will it rain?", "_event_title": "Weather"},
            {"question": "Who wins?", "_event_title": "Sports"},
        ]
        self.cache.store["search_all_markets:volume24hr:"] = cached

        result = await self.service.search_all_markets(query="rain")

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["markets"][0]["question"], "Will it rain?")

    async def test_query_matches_event_title(self):
        # The title is only consulted when the market has no
        # question of its own.
        cached = [
            {"_event_title": "Super Bowl"},
            {"question": "Q2", "_event_title": "Other"},
        ]
        self.cache.store["search_all_markets:volume24hr:"] = cached

        result = await self.service.search_all_markets(query="super")

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["markets"][0]["_event_title"], "Super Bowl")

    async def test_offset_and_limit_pagination(self):
        cached = [{"question": f"Q{i}"} for i in range(10)]
        self.cache.store["search_all_markets:volume24hr:"] = cached

        result = await self.service.search_all_markets(limit=3, offset=2)

        self.assertEqual(result["total"], 10)
        self.assertEqual(result["offset"], 2)
        self.assertEqual([m["question"] for m in result["markets"]], ["Q2", "Q3", "Q4"])
        self.assertTrue(result["has_more"])

    async def test_tag_filter(self):
        events = [
            {
                "id": "e1",
                "tags": [{"slug": "nba"}],
                "markets": [{"question": "Sports Q", "conditionId": "cid-1"}],
            },
            {
                "id": "e2",
                "tags": [{"slug": "politics"}],
                "markets": [{"question": "Politics Q", "conditionId": "cid-2"}],
            },
        ]
        self._patch_http({f"{POLYMARKET_GAMMA_API}/events": self._single_page(events)})

        result = await self.service.search_all_markets(tag="sports")

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["markets"][0]["question"], "Sports Q")
        self.assertEqual(
            self.cache.set_calls_for("search_all_markets:volume24hr:sports"),
            [
                (
                    "search_all_markets:volume24hr:sports",
                    result["markets"],
                    30,
                )
            ],
        )

    async def test_resolved_markets_are_filtered(self):
        events = [
            {
                "id": "e1",
                "tags": [],
                "markets": [
                    {"question": "Resolved", "conditionId": "cid-1", "bestAsk": "0.999"},
                    {"question": "Live", "conditionId": "cid-2", "bestAsk": "0.5"},
                ],
            }
        ]
        self._patch_http({f"{POLYMARKET_GAMMA_API}/events": self._single_page(events)})
        result = await self.service.search_all_markets()
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["markets"][0]["question"], "Live")

    async def test_dedup_by_condition_id(self):
        events = [
            {
                "id": "e1",
                "tags": [],
                "markets": [
                    {"question": "Q", "conditionId": "cid-1", "bestAsk": "0.5"},
                ],
            },
            {
                "id": "e2",
                "tags": [],
                "markets": [
                    {"question": "Q", "conditionId": "cid-1", "bestAsk": "0.5"},
                ],
            },
        ]
        self._patch_http({f"{POLYMARKET_GAMMA_API}/events": self._single_page(events)})
        result = await self.service.search_all_markets()
        self.assertEqual(result["total"], 1)

    async def test_event_without_markets_is_skipped(self):
        events = [
            {"id": "e1", "tags": [], "markets": []},
            {
                "id": "e2",
                "tags": [],
                "markets": [{"question": "Q", "conditionId": "cid-1", "bestAsk": "0.5"}],
            },
        ]
        self._patch_http({f"{POLYMARKET_GAMMA_API}/events": self._single_page(events)})
        result = await self.service.search_all_markets()
        self.assertEqual(result["total"], 1)

    async def test_price_building_branches(self):
        events = [
            {
                "id": "e1",
                "tags": [],
                "markets": [
                    # endDateIso mapping.
                    {
                        "question": "QD",
                        "conditionId": "cid-d",
                        "bestAsk": "0.5",
                        "endDateIso": "2026-05-01",
                    },
                    # outcomePrices as a JSON string.
                    {"question": "QS", "conditionId": "cid-s", "outcomePrices": '["0.2", "0.8"]'},
                    # outcomePrices as a list.
                    {"question": "QP", "conditionId": "cid-p", "outcomePrices": [0.25, 0.75]},
                    # outcomePrices as invalid JSON.
                    {"question": "QI", "conditionId": "cid-i", "outcomePrices": "junk"},
                    # outcomePrices as a non-numeric list.
                    {"question": "QN", "conditionId": "cid-n", "outcomePrices": ["a", "b"]},
                    # No price information at all.
                    {"question": "QE", "conditionId": "cid-e"},
                ],
            }
        ]
        self._patch_http({f"{POLYMARKET_GAMMA_API}/events": self._single_page(events)})

        result = await self.service.search_all_markets()

        by_question = {m["question"]: m for m in result["markets"]}
        self.assertEqual(by_question["QD"]["end_date_iso"], "2026-05-01")
        self.assertEqual(by_question["QS"]["outcomePrices"], ["0.2", "0.8"])
        self.assertEqual(by_question["QP"]["outcomePrices"], ["0.25", "0.75"])
        self.assertEqual(by_question["QI"]["outcomePrices"], ["0.5", "0.5"])
        self.assertEqual(by_question["QN"]["outcomePrices"], ["0.5", "0.5"])
        self.assertEqual(by_question["QE"]["outcomePrices"], ["0.5", "0.5"])

    async def test_non_200_first_page_returns_empty(self):
        self._patch_http({f"{POLYMARKET_GAMMA_API}/events": _FakeResponse(500, None)})
        result = await self.service.search_all_markets()
        self.assertEqual(
            result,
            {"markets": [], "total": 0, "offset": 0, "has_more": False},
        )

    async def test_non_200_later_page_stops_pagination(self):
        calls = {"n": 0}

        def handler(url, params):
            calls["n"] += 1
            if calls["n"] == 1:
                # A full page keeps pagination going.
                return _FakeResponse(
                    200,
                    [self._event(i, f"Q{i}", f"cid-{i}") for i in range(200)],
                )
            return _FakeResponse(500, None)

        self._patch_http({f"{POLYMARKET_GAMMA_API}/events": handler})
        result = await self.service.search_all_markets()
        self.assertEqual(result["total"], 200)
        self.assertEqual(calls["n"], 2)

    async def test_duplicate_page_fingerprint_stops_pagination(self):
        # Two consecutive full pages whose first event shares an
        # id and length produce the same fingerprint.
        page = [self._event(i, f"Q{i}", f"cid-{i}") for i in range(200)]

        def handler(url, params):
            return _FakeResponse(200, page)

        self._patch_http({f"{POLYMARKET_GAMMA_API}/events": handler})
        result = await self.service.search_all_markets()
        # Second page has the same fingerprint → stop.
        self.assertEqual(result["total"], 200)
        self.assertEqual(len(self.http_instances[0].calls), 2)

    async def test_empty_events_page_stops(self):
        self._patch_http({f"{POLYMARKET_GAMMA_API}/events": _FakeResponse(200, [])})
        result = await self.service.search_all_markets()
        self.assertEqual(result["total"], 0)

    async def test_non_list_events_stops(self):
        self._patch_http({f"{POLYMARKET_GAMMA_API}/events": _FakeResponse(200, {"data": []})})
        result = await self.service.search_all_markets()
        self.assertEqual(result["total"], 0)

    async def test_market_cap_is_respected(self):
        # Each event carries 100 markets so the 6000-market cap is
        # hit on the very first page.
        def handler(url, params):
            return _FakeResponse(
                200,
                [
                    {
                        "id": f"evt-{i}",
                        "tags": [],
                        "markets": [
                            {
                                "question": f"Q{i}-{j}",
                                "conditionId": f"cid-{i}-{j}",
                                "bestAsk": "0.5",
                            }
                            for j in range(100)
                        ],
                    }
                    for i in range(200)
                ],
            )

        self._patch_http({f"{POLYMARKET_GAMMA_API}/events": handler})
        result = await self.service.search_all_markets()
        self.assertEqual(result["total"], 6000)

    async def test_exception_returns_error_dict(self):
        def handler(url, params):
            raise RuntimeError("network down")

        self._patch_http({f"{POLYMARKET_GAMMA_API}/events": handler})
        result = await self.service.search_all_markets(offset=5)
        self.assertEqual(
            result,
            {"markets": [], "total": 0, "offset": 5, "has_more": False},
        )

    async def test_sort_is_forwarded(self):
        seen_params = []

        def handler(url, params):
            seen_params.append(params)
            return _FakeResponse(200, [])

        self._patch_http({f"{POLYMARKET_GAMMA_API}/events": handler})
        await self.service.search_all_markets(sort="startDate")
        self.assertEqual(seen_params[0]["order"], "startDate")

    async def test_empty_sort_defaults_to_volume24hr(self):
        seen_params = []

        def handler(url, params):
            seen_params.append(params)
            return _FakeResponse(200, [])

        self._patch_http({f"{POLYMARKET_GAMMA_API}/events": handler})
        await self.service.search_all_markets(sort="")
        self.assertEqual(seen_params[0]["order"], "volume24hr")


class GetMarketTraderStatsTests(_PolymarketServiceTestCase):
    """``get_market_trader_stats`` order-book analytics."""

    def _routes(self, market_data=None, gamma_data=None, book=None):
        return {
            f"{POLYMARKET_CLOB_API}/markets/": _FakeResponse(200, market_data or {}),
            f"{POLYMARKET_GAMMA_API}/markets": _FakeResponse(200, gamma_data or []),
            f"{POLYMARKET_CLOB_API}/book": _FakeResponse(200, book or {"bids": [], "asks": []}),
        }

    async def test_full_stats(self):
        self._patch_http(
            self._routes(
                market_data={
                    "tokens": [
                        {"token_id": "tok-yes", "outcome": "Yes", "price": "0.65"},
                        {"token_id": "tok-no", "outcome": "No", "price": "0.35"},
                    ]
                },
                gamma_data=[{"volumeNum": "1000"}],
                book={
                    "bids": [
                        {"size": "100", "price": "0.6"},
                        {"size": "50", "price": "0.55"},
                    ],
                    "asks": [{"size": "80", "price": "0.7"}],
                },
            )
        )

        stats = await self.service.get_market_trader_stats("cid-1")

        self.assertEqual(stats["condition_id"], "cid-1")
        self.assertEqual(stats["yes_traders"], 2)
        self.assertEqual(stats["no_traders"], 1)
        self.assertEqual(stats["total_trades"], 3)
        self.assertEqual(stats["yes_volume"], 650.0)
        self.assertEqual(stats["no_volume"], 350.0)
        self.assertEqual(stats["side_ratio"], {"yes": 65.0, "no": 35.0})

        top = stats["top_traders"]
        self.assertEqual(len(top), 3)
        # Sorted by size descending.
        self.assertEqual(top[0]["address"], "Order-1")
        self.assertEqual(top[0]["short_address"], "@0.600")
        self.assertEqual(top[0]["yes_volume"], 60.0)
        self.assertEqual(top[0]["no_volume"], 0)
        self.assertEqual(top[0]["total_volume"], 60.0)
        self.assertEqual(top[0]["lean"], "YES")
        self.assertEqual(top[1]["address"], "Order-2")
        self.assertEqual(top[1]["no_volume"], 56.0)
        self.assertEqual(top[1]["lean"], "NO")
        self.assertEqual(top[2]["yes_volume"], 27.5)

        recent = stats["recent_trades"]
        self.assertEqual(len(recent), 3)
        self.assertEqual(recent[0]["side"], "BUY")
        self.assertEqual(recent[0]["outcome"], "Yes")
        self.assertEqual(recent[2]["side"], "SELL")

    async def test_non_200_market_response(self):
        self._patch_http({f"{POLYMARKET_CLOB_API}/markets/": _FakeResponse(404, None)})
        stats = await self.service.get_market_trader_stats("cid-1")
        self.assertEqual(stats["yes_traders"], 0)
        self.assertEqual(stats["top_traders"], [])
        self.assertEqual(stats["side_ratio"], {"yes": 50.0, "no": 50.0})

    async def test_no_yes_token_skips_book(self):
        self._patch_http(
            self._routes(
                market_data={"tokens": [{"token_id": "tok-no", "outcome": "No", "price": "0.5"}]},
                gamma_data=[{"volumeNum": "100"}],
            )
        )
        stats = await self.service.get_market_trader_stats("cid-1")
        self.assertEqual(stats["yes_traders"], 0)
        self.assertEqual(stats["no_traders"], 0)
        # Volume split evenly when only one price is known.
        self.assertEqual(stats["yes_volume"], 50.0)
        self.assertEqual(stats["no_volume"], 50.0)

    async def test_gamma_failure_falls_back_to_book_depth(self):
        self._patch_http(
            self._routes(
                market_data={"tokens": [{"token_id": "tok-yes", "outcome": "Yes", "price": "0.5"}]},
                gamma_data=RuntimeError("gamma down"),
                book={
                    "bids": [{"size": "100", "price": "0.6"}],
                    "asks": [{"size": "80", "price": "0.7"}],
                },
            )
        )
        stats = await self.service.get_market_trader_stats("cid-1")
        self.assertEqual(stats["yes_volume"], 60.0)
        self.assertEqual(stats["no_volume"], 56.0)

    async def test_gamma_non_200_falls_back_to_book_depth(self):
        self._patch_http(
            self._routes(
                market_data={"tokens": [{"token_id": "tok-yes", "outcome": "Yes", "price": "0.5"}]},
                gamma_data=None,
                book={
                    "bids": [{"size": "100", "price": "0.6"}],
                    "asks": [],
                },
            )
        )
        # gamma returns 200 with an empty list → total_volume 0.
        stats = await self.service.get_market_trader_stats("cid-1")
        self.assertEqual(stats["yes_volume"], 60.0)
        self.assertEqual(stats["no_volume"], 0.0)

    async def test_book_non_200_yields_empty_book(self):
        self._patch_http(
            self._routes(
                market_data={"tokens": [{"token_id": "tok-yes", "outcome": "Yes", "price": "0.5"}]},
                gamma_data=[{"volumeNum": "100"}],
                book=None,
            )
        )
        # Override the book route with a non-200.
        self._patch_http(
            {
                f"{POLYMARKET_CLOB_API}/markets/": _FakeResponse(
                    200,
                    {"tokens": [{"token_id": "tok-yes", "outcome": "Yes", "price": "0.5"}]},
                ),
                f"{POLYMARKET_GAMMA_API}/markets": _FakeResponse(200, [{"volumeNum": "100"}]),
                f"{POLYMARKET_CLOB_API}/book": _FakeResponse(500, None),
            }
        )
        stats = await self.service.get_market_trader_stats("cid-1")
        self.assertEqual(stats["yes_traders"], 0)
        self.assertEqual(stats["yes_volume"], 50.0)

    async def test_zero_price_sum_splits_volume_evenly(self):
        self._patch_http(
            self._routes(
                market_data={
                    "tokens": [
                        {"token_id": "tok-yes", "outcome": "Yes", "price": "0"},
                        {"token_id": "tok-no", "outcome": "No", "price": "0"},
                    ]
                },
                gamma_data=[{"volumeNum": "100"}],
            )
        )
        stats = await self.service.get_market_trader_stats("cid-1")
        self.assertEqual(stats["yes_volume"], 50.0)
        self.assertEqual(stats["no_volume"], 50.0)

    async def test_top_traders_capped_at_ten(self):
        bids = [{"size": str(100 - i), "price": "0.5"} for i in range(15)]
        self._patch_http(
            self._routes(
                market_data={"tokens": [{"token_id": "tok-yes", "outcome": "Yes", "price": "0.5"}]},
                gamma_data=[{"volumeNum": "1000"}],
                book={"bids": bids, "asks": []},
            )
        )
        stats = await self.service.get_market_trader_stats("cid-1")
        self.assertEqual(len(stats["top_traders"]), 10)
        self.assertEqual(len(stats["recent_trades"]), 10)

    async def test_book_fetch_exception_yields_empty_book(self):
        def book_handler(url, params):
            raise RuntimeError("book down")

        self._patch_http(
            {
                f"{POLYMARKET_CLOB_API}/markets/": _FakeResponse(
                    200,
                    {"tokens": [{"token_id": "tok-yes", "outcome": "Yes", "price": "0.5"}]},
                ),
                f"{POLYMARKET_GAMMA_API}/markets": _FakeResponse(200, [{"volumeNum": "100"}]),
                f"{POLYMARKET_CLOB_API}/book": book_handler,
            }
        )
        stats = await self.service.get_market_trader_stats("cid-1")
        self.assertEqual(stats["yes_traders"], 0)
        self.assertEqual(stats["no_traders"], 0)
        # Volume falls back to the even split.
        self.assertEqual(stats["yes_volume"], 50.0)
        self.assertEqual(stats["no_volume"], 50.0)

    async def test_exception_returns_default_stats(self):
        def handler(url, params):
            raise RuntimeError("network down")

        self._patch_http({f"{POLYMARKET_CLOB_API}/markets/": handler})
        stats = await self.service.get_market_trader_stats("cid-1")
        self.assertEqual(stats["yes_traders"], 0)
        self.assertEqual(stats["top_traders"], [])


class GetMarketPricesTests(_PolymarketServiceTestCase):
    """``get_market_prices`` order-book price extraction."""

    async def test_extracts_yes_and_no_prices(self):
        self._patch_http(
            {
                f"{POLYMARKET_CLOB_API}/order_book": _FakeResponse(
                    200,
                    {
                        "bids": [{"outcome": "Yes", "price": "0.62"}],
                        "asks": [{"outcome": "No", "price": "0.38"}],
                    },
                )
            }
        )
        prices = await self.service.get_market_prices("cid-1")
        self.assertEqual(prices, {"yes_price": 0.62, "no_price": 0.38})

    async def test_empty_book_keeps_defaults(self):
        self._patch_http(
            {f"{POLYMARKET_CLOB_API}/order_book": _FakeResponse(200, {"bids": [], "asks": []})}
        )
        prices = await self.service.get_market_prices("cid-1")
        self.assertEqual(prices, {"yes_price": 0.5, "no_price": 0.5})

    async def test_non_200_keeps_defaults(self):
        self._patch_http({f"{POLYMARKET_CLOB_API}/order_book": _FakeResponse(500, None)})
        prices = await self.service.get_market_prices("cid-1")
        self.assertEqual(prices, {"yes_price": 0.5, "no_price": 0.5})

    async def test_exception_keeps_defaults(self):
        self._patch_http(
            {f"{POLYMARKET_CLOB_API}/order_book": _FakeResponse(200, RuntimeError("boom"))}
        )
        prices = await self.service.get_market_prices("cid-1")
        self.assertEqual(prices, {"yes_price": 0.5, "no_price": 0.5})


class GetHistoricalWinStatsTests(_PolymarketServiceTestCase):
    """``_get_historical_win_stats`` closed-position accounting."""

    def _activity_pages(self, pages):
        def handler(url, params):
            offset = params.get("offset", 0)
            page = pages.get(offset)
            if page is None:
                return _FakeResponse(200, [])
            return _FakeResponse(200, page)

        return handler

    async def test_win_loss_redeem_and_merge_accounting(self):
        self._patch_http(
            {
                f"{POLYMARKET_DATA_API}/activity": self._activity_pages(
                    {
                        0: [
                            # Win: buy 100, sell 150.
                            {
                                "conditionId": "mkt-a",
                                "type": "TRADE",
                                "side": "BUY",
                                "usdcSize": 100,
                            },
                            {
                                "conditionId": "mkt-a",
                                "type": "TRADE",
                                "side": "SELL",
                                "usdcSize": 150,
                            },
                            # Loss: buy 100, sell 50.
                            {
                                "conditionId": "mkt-b",
                                "type": "TRADE",
                                "side": "BUY",
                                "usdcSize": 100,
                            },
                            {
                                "conditionId": "mkt-b",
                                "type": "TRADE",
                                "side": "SELL",
                                "usdcSize": 50,
                            },
                            # Redemption → closed win.
                            {"conditionId": "mkt-c", "type": "REDEEM", "usdcSize": 10},
                            # Merge with buy exposure → closed non-win.
                            {"conditionId": "mkt-d", "type": "MERGE", "usdcSize": 5},
                            {
                                "conditionId": "mkt-d",
                                "type": "TRADE",
                                "side": "BUY",
                                "usdcSize": 50,
                            },
                            # Open position only → historical, not closed.
                            {
                                "conditionId": "mkt-e",
                                "type": "TRADE",
                                "side": "BUY",
                                "usdcSize": 75,
                            },
                            # Sell-only → not a historical position.
                            {
                                "conditionId": "mkt-f",
                                "type": "TRADE",
                                "side": "SELL",
                                "usdcSize": 20,
                            },
                        ]
                    }
                )
            }
        )

        result = await self.service._get_historical_win_stats(WALLET)

        self.assertTrue(result["history_fetch_ok"])
        # a, b, c, d, e are historical positions.
        self.assertEqual(result["total_positions_history"], 5)
        # a (win), c (redeem) are wins.
        self.assertEqual(result["wins_positions_history"], 2)
        self.assertEqual(result["win_rate"], 40.0)

    async def test_result_is_cached(self):
        self._patch_http({f"{POLYMARKET_DATA_API}/activity": self._activity_pages({0: []})})

        first = await self.service._get_historical_win_stats(WALLET)
        second = await self.service._get_historical_win_stats(WALLET)

        self.assertIs(first, second)
        self.assertEqual(len(self.http_instances), 1)
        self.assertIn(WALLET.lower(), self.hr_cache)

    async def test_cache_hit_returns_cached_data(self):
        cached = {"win_rate": 12.3}
        self.hr_cache[WALLET.lower()] = {
            "data": cached,
            "ts": datetime.now(UTC).timestamp(),
        }

        result = await self.service._get_historical_win_stats(WALLET)

        self.assertIs(result, cached)
        self.assertEqual(self.http_instances, [])

    async def test_stale_cache_is_refetched(self):
        self.hr_cache[WALLET.lower()] = {
            "data": {"win_rate": 12.3},
            "ts": datetime.now(UTC).timestamp() - 10_000,
        }
        self._patch_http({f"{POLYMARKET_DATA_API}/activity": self._activity_pages({0: []})})

        result = await self.service._get_historical_win_stats(WALLET)

        self.assertNotEqual(result, {"win_rate": 12.3})
        self.assertEqual(len(self.http_instances), 1)

    async def test_empty_wallet_returns_defaults(self):
        result = await self.service._get_historical_win_stats("")
        self.assertEqual(
            result,
            {
                "wins_positions_history": 0,
                "total_positions_history": 0,
                "win_rate": 0.0,
                "history_fetch_ok": False,
            },
        )
        self.assertEqual(self.http_instances, [])

    async def test_non_200_stops_pagination(self):
        self._patch_http({f"{POLYMARKET_DATA_API}/activity": _FakeResponse(500, None)})
        result = await self.service._get_historical_win_stats(WALLET)
        self.assertFalse(result["history_fetch_ok"])
        self.assertEqual(result["total_positions_history"], 0)

    async def test_non_list_payload_stops(self):
        self._patch_http({f"{POLYMARKET_DATA_API}/activity": _FakeResponse(200, {"data": []})})
        result = await self.service._get_historical_win_stats(WALLET)
        self.assertFalse(result["history_fetch_ok"])

    async def test_non_dict_rows_are_skipped(self):
        self._patch_http(
            {f"{POLYMARKET_DATA_API}/activity": self._activity_pages({0: ["junk", 42, None]})}
        )
        result = await self.service._get_historical_win_stats(WALLET)
        self.assertTrue(result["history_fetch_ok"])
        self.assertEqual(result["total_positions_history"], 0)

    async def test_rows_without_market_id_are_skipped(self):
        self._patch_http(
            {
                f"{POLYMARKET_DATA_API}/activity": self._activity_pages(
                    {0: [{"type": "TRADE", "side": "BUY", "usdcSize": 10}]}
                )
            }
        )
        result = await self.service._get_historical_win_stats(WALLET)
        self.assertEqual(result["total_positions_history"], 0)

    async def test_pagination_walks_multiple_pages(self):
        seen_offsets = []

        def handler(url, params):
            seen_offsets.append(params.get("offset", 0))
            # Full pages keep the pagination going.
            return _FakeResponse(
                200,
                [
                    {"conditionId": f"mkt-{params.get('offset')}-{i}", "type": "REDEEM"}
                    for i in range(500)
                ],
            )

        self._patch_http({f"{POLYMARKET_DATA_API}/activity": handler})
        result = await self.service._get_historical_win_stats(WALLET)
        # max_pages is 20; every page is full so all pages are walked.
        self.assertEqual(len(seen_offsets), 20)
        self.assertEqual(result["total_positions_history"], 20 * 500)
        self.assertEqual(result["wins_positions_history"], 20 * 500)

    async def test_exception_returns_defaults(self):
        def handler(url, params):
            raise RuntimeError("network down")

        self._patch_http({f"{POLYMARKET_DATA_API}/activity": handler})
        result = await self.service._get_historical_win_stats(WALLET)
        self.assertFalse(result["history_fetch_ok"])

    async def test_market_id_falls_back_to_other_keys(self):
        self._patch_http(
            {
                f"{POLYMARKET_DATA_API}/activity": self._activity_pages(
                    {
                        0: [
                            {"market": "mkt-m", "type": "REDEEM"},
                            {"condition_id": "mkt-c", "type": "REDEEM"},
                        ]
                    }
                )
            }
        )
        result = await self.service._get_historical_win_stats(WALLET)
        self.assertEqual(result["total_positions_history"], 2)
        self.assertEqual(result["wins_positions_history"], 2)

    async def test_value_and_amount_fallbacks(self):
        self._patch_http(
            {
                f"{POLYMARKET_DATA_API}/activity": self._activity_pages(
                    {
                        0: [
                            {"conditionId": "mkt-v", "type": "TRADE", "side": "BUY", "value": 100},
                            {"conditionId": "mkt-v", "type": "TRADE", "side": "SELL", "value": 150},
                            {"conditionId": "mkt-a", "type": "TRADE", "side": "BUY", "amount": 100},
                            {"conditionId": "mkt-a", "type": "TRADE", "side": "SELL", "amount": 50},
                        ]
                    }
                )
            }
        )
        result = await self.service._get_historical_win_stats(WALLET)
        self.assertEqual(result["total_positions_history"], 2)
        self.assertEqual(result["wins_positions_history"], 1)


class GetPortfolioSummaryTests(_PolymarketServiceTestCase):
    """``get_portfolio_summary`` rollup."""

    def _patch_parts(self, balance, positions, history):
        self.service.get_wallet_balance = AsyncMock(return_value=balance)
        self.service.get_positions = AsyncMock(return_value=positions)
        self.service._get_historical_win_stats = AsyncMock(return_value=history)

    async def test_full_summary(self):
        balance = {
            "wallet_address": WALLET,
            "matic_balance": 1.5,
            "usdc_balance": 100.0,
            "chain": "polygon",
        }
        positions = [
            {"title": "P1", "size": 10, "avgPrice": 0.5, "curPrice": 0.7, "pnl": 2.0},
            {"title": "P2", "size": 5, "avgPrice": 0.4, "curPrice": 0.3, "pnl": -0.5},
            # Invalid size → skipped by the rollup.
            {"title": "P3", "size": "abc", "avgPrice": 0.4, "curPrice": 0.3, "pnl": 0},
            # pnl None → skipped by the win counter.
            {"title": "P4", "size": 3, "avgPrice": 0.5, "curPrice": 0.6, "pnl": None},
        ]
        history = {
            "wins_positions_history": 3,
            "total_positions_history": 10,
            "win_rate": 30.0,
            "history_fetch_ok": True,
        }
        self._patch_parts(balance, positions, history)

        result = await self.service.get_portfolio_summary(WALLET)

        self.service.get_wallet_balance.assert_awaited_once_with(
            WALLET, private_key=None, clob_creds=None
        )
        self.service.get_positions.assert_awaited_once_with(
            WALLET, private_key=None, clob_creds=None
        )

        self.assertEqual(result["wallet_address"], WALLET)
        self.assertEqual(result["usdc_balance"], 100.0)
        self.assertEqual(result["matic_balance"], 1.5)
        # P1, P2 and P4 have a positive size; P3's size is invalid.
        self.assertEqual(result["active_positions"], 3)
        self.assertEqual(result["total_positions"], 4)
        self.assertEqual(result["total_invested"], 8.5)
        self.assertEqual(result["total_current_value"], 10.3)
        self.assertEqual(result["total_pnl"], 1.8)
        self.assertEqual(result["pnl_percentage"], 21.18)
        self.assertEqual(result["win_rate"], 30.0)
        self.assertEqual(result["wins_positions_history"], 3)
        self.assertEqual(result["total_positions_history"], 10)
        self.assertEqual(result["wins_positions"], 3)
        self.assertEqual(result["resolved_trades"], 10)
        self.assertEqual(len(result["positions"]), 4)

    async def test_positions_capped_at_twenty(self):
        positions = [
            {"title": f"P{i}", "size": 1, "avgPrice": 0.5, "curPrice": 0.5, "pnl": 0.0}
            for i in range(25)
        ]
        self._patch_parts(
            {"usdc_balance": 0.0, "matic_balance": 0.0},
            positions,
            {"history_fetch_ok": True},
        )
        result = await self.service.get_portfolio_summary(WALLET)
        self.assertEqual(len(result["positions"]), 20)

    async def test_history_unavailable_falls_back_to_current_wins(self):
        positions = [
            {"title": "P1", "size": 1, "avgPrice": 0.5, "curPrice": 0.7, "pnl": 0.2},
            {"title": "P2", "size": 1, "avgPrice": 0.5, "curPrice": 0.4, "pnl": -0.1},
        ]
        self._patch_parts(
            {"usdc_balance": 0.0, "matic_balance": 0.0},
            positions,
            {"history_fetch_ok": False},
        )
        result = await self.service.get_portfolio_summary(WALLET)
        self.assertEqual(result["wins_positions_history"], 1)
        self.assertEqual(result["total_positions_history"], 2)
        self.assertEqual(result["win_rate"], 50.0)

    async def test_zero_investment_yields_zero_pnl_pct(self):
        positions = [
            {"title": "P1", "size": 0, "avgPrice": 0.5, "curPrice": 0.7, "pnl": 0.0},
        ]
        self._patch_parts(
            {"usdc_balance": 0.0, "matic_balance": 0.0},
            positions,
            {"history_fetch_ok": True},
        )
        result = await self.service.get_portfolio_summary(WALLET)
        self.assertEqual(result["pnl_percentage"], 0.0)
        self.assertEqual(result["active_positions"], 0)

    async def test_negative_pnl(self):
        positions = [
            {"title": "P1", "size": 10, "avgPrice": 0.5, "curPrice": 0.3, "pnl": -2.0},
        ]
        self._patch_parts(
            {"usdc_balance": 0.0, "matic_balance": 0.0},
            positions,
            {"history_fetch_ok": True},
        )
        result = await self.service.get_portfolio_summary(WALLET)
        self.assertEqual(result["total_invested"], 5.0)
        self.assertEqual(result["total_current_value"], 3.0)
        self.assertEqual(result["total_pnl"], -2.0)
        self.assertEqual(result["pnl_percentage"], -40.0)

    async def test_credentials_are_forwarded(self):
        creds = {"api_key": "k", "api_secret": "s", "api_passphrase": "p"}
        self._patch_parts(
            {"usdc_balance": 0.0, "matic_balance": 0.0},
            [],
            {"history_fetch_ok": True},
        )
        await self.service.get_portfolio_summary(WALLET, private_key=PRIVATE_KEY, clob_creds=creds)
        self.service.get_wallet_balance.assert_awaited_once_with(
            WALLET, private_key=PRIVATE_KEY, clob_creds=creds
        )
        self.service.get_positions.assert_awaited_once_with(
            WALLET, private_key=PRIVATE_KEY, clob_creds=creds
        )


class GetPolymarketServiceTests(_PolymarketServiceTestCase):
    """Module-level singleton."""

    def setUp(self):
        super().setUp()
        saved = polymarket_service._polymarket_service
        self.addCleanup(lambda: setattr(polymarket_service, "_polymarket_service", saved))
        polymarket_service._polymarket_service = None

    def test_first_call_creates_and_second_reuses(self):
        first = get_polymarket_service()
        second = get_polymarket_service()
        self.assertIsInstance(first, PolymarketService)
        self.assertIs(first, second)

    def test_existing_instance_is_returned(self):
        sentinel = PolymarketService()
        polymarket_service._polymarket_service = sentinel
        self.assertIs(get_polymarket_service(), sentinel)


if __name__ == "__main__":
    unittest.main()
