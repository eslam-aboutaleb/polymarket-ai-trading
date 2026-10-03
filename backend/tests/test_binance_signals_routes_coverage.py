"""Coverage tests for the Binance signals routes (app/api/routes/binance_signals.py).

All HTTP calls to the Binance web3 API are mocked at the ``httpx.AsyncClient``
boundary (or the module-level ``_binance_post`` / ``_binance_get`` helpers),
so the tests exercise route behaviour, caching, response shaping and
validation without network access.
"""

import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.api.routes import binance_signals
from app.api.routes.auth import get_current_user_from_token
from app.main import app

WALLET = "0x" + "ab" * 20


def _mock_httpx_client(response_json=None, post_exc=None, get_exc=None):
    """Build an httpx.AsyncClient double.

    ``response_json`` is returned by ``resp.json()``; ``post_exc`` /
    ``get_exc`` optionally make the respective verb raise.
    """
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=response_json)

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    if post_exc is not None:
        client.post = AsyncMock(side_effect=post_exc)
    else:
        client.post = AsyncMock(return_value=resp)
    if get_exc is not None:
        client.get = AsyncMock(side_effect=get_exc)
    else:
        client.get = AsyncMock(return_value=resp)
    return client


class CacheHelperTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(binance_signals._cache.clear)
        binance_signals._cache.clear()

    def test_cache_set_then_get(self):
        binance_signals._cache_set("k", {"a": 1})
        self.assertEqual(binance_signals._cache_get("k"), {"a": 1})

    def test_cache_miss(self):
        self.assertIsNone(binance_signals._cache_get("missing"))

    def test_cache_expired(self):
        binance_signals._cache["old"] = (time.time() - 400, "stale")
        self.assertIsNone(binance_signals._cache_get("old"))

    def test_cache_falsy_value_is_not_cached(self):
        # A cached ``None`` entry is treated as a miss by callers
        # (``cached is not None`` guard), so a fresh fetch happens.
        binance_signals._cache_set("k", None)
        self.assertIsNone(binance_signals._cache_get("k"))


class BinancePostTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(binance_signals._cache.clear)
        binance_signals._cache.clear()

    def test_post_returns_data_key(self):
        client = _mock_httpx_client({"data": [{"id": 1}]})
        with patch("app.api.routes.binance_signals.httpx.AsyncClient", lambda **kw: client):
            result = asyncio_result(binance_signals._binance_post("/path", {"a": 1}))
        self.assertEqual(result, [{"id": 1}])
        self.assertEqual(
            client.post.await_args.args[0],
            "https://web3.binance.com/path",
        )
        kwargs = client.post.await_args.kwargs
        self.assertEqual(kwargs["json"], {"a": 1})
        self.assertEqual(
            kwargs["headers"]["User-Agent"],
            "PolymarketBot/1.0",
        )

    def test_post_returns_whole_payload_without_data_key(self):
        client = _mock_httpx_client({"rows": [{"id": 2}]})
        with patch("app.api.routes.binance_signals.httpx.AsyncClient", lambda **kw: client):
            result = asyncio_result(binance_signals._binance_post("/path", {"a": 1}))
        self.assertEqual(result, {"rows": [{"id": 2}]})

    def test_post_returns_none_on_error(self):
        client = _mock_httpx_client(None, post_exc=RuntimeError("network down"))
        with patch("app.api.routes.binance_signals.httpx.AsyncClient", lambda **kw: client):
            result = asyncio_result(binance_signals._binance_post("/path", {"a": 1}))
        self.assertIsNone(result)

    def test_post_caches_result(self):
        client = _mock_httpx_client({"data": [{"id": 1}]})
        with patch("app.api.routes.binance_signals.httpx.AsyncClient", lambda **kw: client):
            first = asyncio_result(
                binance_signals._binance_post(
                    "/path",
                    {"a": 1},
                    cache_key="pk",
                )
            )
            second = asyncio_result(
                binance_signals._binance_post(
                    "/path",
                    {"a": 1},
                    cache_key="pk",
                )
            )
        self.assertEqual(first, [{"id": 1}])
        self.assertEqual(second, [{"id": 1}])
        # Only the first call hit the network
        self.assertEqual(client.post.await_count, 1)

    def test_post_no_cache_key_skips_cache(self):
        client = _mock_httpx_client({"data": [{"id": 1}]})
        with patch("app.api.routes.binance_signals.httpx.AsyncClient", lambda **kw: client):
            asyncio_result(binance_signals._binance_post("/path", {"a": 1}))
            asyncio_result(binance_signals._binance_post("/path", {"a": 1}))
        self.assertEqual(client.post.await_count, 2)
        self.assertEqual(len(binance_signals._cache), 0)


class BinanceGetTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(binance_signals._cache.clear)
        binance_signals._cache.clear()

    def test_get_returns_data_key(self):
        client = _mock_httpx_client({"data": [{"id": 1}]})
        with patch("app.api.routes.binance_signals.httpx.AsyncClient", lambda **kw: client):
            result = asyncio_result(binance_signals._binance_get("/path", {"q": "x"}))
        self.assertEqual(result, [{"id": 1}])
        self.assertEqual(
            client.get.await_args.args[0],
            "https://web3.binance.com/path",
        )
        self.assertEqual(client.get.await_args.kwargs["params"], {"q": "x"})

    def test_get_returns_none_on_error(self):
        client = _mock_httpx_client(None, get_exc=RuntimeError("network down"))
        with patch("app.api.routes.binance_signals.httpx.AsyncClient", lambda **kw: client):
            result = asyncio_result(binance_signals._binance_get("/path", {"q": "x"}))
        self.assertIsNone(result)

    def test_get_caches_result(self):
        client = _mock_httpx_client({"data": [{"id": 1}]})
        with patch("app.api.routes.binance_signals.httpx.AsyncClient", lambda **kw: client):
            first = asyncio_result(binance_signals._binance_get("/path", cache_key="gk"))
            second = asyncio_result(binance_signals._binance_get("/path", cache_key="gk"))
        self.assertEqual(first, [{"id": 1}])
        self.assertEqual(second, [{"id": 1}])
        self.assertEqual(client.get.await_count, 1)


class BinanceSignalRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.user = {"user_id": 1, "wallet_address": WALLET, "is_admin": False}
        app.dependency_overrides[get_current_user_from_token] = lambda: self.user
        self.addCleanup(app.dependency_overrides.clear)
        self.addCleanup(binance_signals._cache.clear)
        binance_signals._cache.clear()

    def _patch_settings(self, enabled):
        return patch(
            "app.api.routes.binance_signals.settings",
            MagicMock(binance_skills_enabled=enabled),
        )

    # ── GET /api/binance/signals/smart-money ──────────────────

    def test_smart_money_signals_list_data(self):
        with (
            self._patch_settings(True),
            patch(
                "app.api.routes.binance_signals._binance_post",
                AsyncMock(return_value=[{"token_address": "0x1"}]),
            ) as mock_post,
        ):
            response = self.client.get("/api/binance/signals/smart-money")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [{"token_address": "0x1"}])
        payload = mock_post.await_args.args[1]
        self.assertEqual(payload["chainId"], "1")
        self.assertEqual(payload["type"], "ALL")
        self.assertEqual(payload["pageSize"], 20)
        self.assertEqual(
            mock_post.await_args.kwargs["cache_key"],
            "smart_signals_1_20",
        )

    def test_smart_money_signals_dict_rows(self):
        with (
            self._patch_settings(True),
            patch(
                "app.api.routes.binance_signals._binance_post",
                AsyncMock(return_value={"rows": [{"a": 1}, {"b": 2}]}),
            ),
        ):
            response = self.client.get("/api/binance/signals/smart-money")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [{"a": 1}, {"b": 2}])

    def test_smart_money_signals_dict_signals(self):
        with (
            self._patch_settings(True),
            patch(
                "app.api.routes.binance_signals._binance_post",
                AsyncMock(return_value={"signals": [{"s": 1}]}),
            ),
        ):
            response = self.client.get("/api/binance/signals/smart-money")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [{"s": 1}])

    def test_smart_money_signals_none_returns_empty(self):
        with (
            self._patch_settings(True),
            patch(
                "app.api.routes.binance_signals._binance_post",
                AsyncMock(return_value=None),
            ),
        ):
            response = self.client.get("/api/binance/signals/smart-money")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_smart_money_signals_chain_mapping(self):
        with (
            self._patch_settings(True),
            patch(
                "app.api.routes.binance_signals._binance_post",
                AsyncMock(return_value=[]),
            ) as mock_post,
        ):
            response = self.client.get(
                "/api/binance/signals/smart-money",
                params={"chain": "polygon", "limit": 5},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_post.await_args.args[1]["chainId"], "137")
        self.assertEqual(mock_post.await_args.args[1]["pageSize"], 5)
        self.assertEqual(
            mock_post.await_args.kwargs["cache_key"],
            "smart_signals_137_5",
        )

    def test_smart_money_signals_unknown_chain_defaults_to_ethereum(self):
        with (
            self._patch_settings(True),
            patch(
                "app.api.routes.binance_signals._binance_post",
                AsyncMock(return_value=[]),
            ) as mock_post,
        ):
            response = self.client.get(
                "/api/binance/signals/smart-money",
                params={"chain": "unknown-chain"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_post.await_args.args[1]["chainId"], "1")

    def test_smart_money_signals_chain_case_insensitive(self):
        with (
            self._patch_settings(True),
            patch(
                "app.api.routes.binance_signals._binance_post",
                AsyncMock(return_value=[]),
            ) as mock_post,
        ):
            self.client.get(
                "/api/binance/signals/smart-money",
                params={"chain": "BSC"},
            )
        self.assertEqual(mock_post.await_args.args[1]["chainId"], "56")

    def test_smart_money_signals_limit_slices(self):
        rows = [{"i": i} for i in range(10)]
        with (
            self._patch_settings(True),
            patch(
                "app.api.routes.binance_signals._binance_post",
                AsyncMock(return_value=rows),
            ),
        ):
            response = self.client.get(
                "/api/binance/signals/smart-money",
                params={"limit": 3},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 3)

    def test_smart_money_signals_disabled(self):
        with self._patch_settings(False):
            response = self.client.get("/api/binance/signals/smart-money")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["detail"],
            "Binance Skills integration is disabled",
        )

    def test_smart_money_signals_requires_auth(self):
        app.dependency_overrides.pop(get_current_user_from_token, None)
        try:
            response = self.client.get("/api/binance/signals/smart-money")
        finally:
            app.dependency_overrides[get_current_user_from_token] = lambda: self.user
        self.assertEqual(response.status_code, 401)

    def test_smart_money_signals_limit_validation(self):
        for bad in (0, 51):
            response = self.client.get(
                "/api/binance/signals/smart-money",
                params={"limit": bad},
            )
            self.assertEqual(response.status_code, 422)

    # ── GET /api/binance/signals/active-buys ──────────────────

    def test_active_buys_list_data(self):
        with (
            self._patch_settings(True),
            patch(
                "app.api.routes.binance_signals._binance_post",
                AsyncMock(return_value=[{"token": "t"}]),
            ) as mock_post,
        ):
            response = self.client.get("/api/binance/signals/active-buys")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [{"token": "t"}])
        self.assertEqual(mock_post.await_args.args[1]["type"], "BUY")
        self.assertEqual(
            mock_post.await_args.kwargs["cache_key"],
            "active_buys_1_10",
        )

    def test_active_buys_dict_rows(self):
        with (
            self._patch_settings(True),
            patch(
                "app.api.routes.binance_signals._binance_post",
                AsyncMock(return_value={"rows": [{"a": 1}]}),
            ),
        ):
            response = self.client.get("/api/binance/signals/active-buys")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [{"a": 1}])

    def test_active_buys_none_returns_empty(self):
        with (
            self._patch_settings(True),
            patch(
                "app.api.routes.binance_signals._binance_post",
                AsyncMock(return_value=None),
            ),
        ):
            response = self.client.get("/api/binance/signals/active-buys")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_active_buys_disabled(self):
        with self._patch_settings(False):
            response = self.client.get("/api/binance/signals/active-buys")
        self.assertEqual(response.status_code, 503)

    def test_active_buys_limit_validation(self):
        for bad in (0, 31):
            response = self.client.get(
                "/api/binance/signals/active-buys",
                params={"limit": bad},
            )
            self.assertEqual(response.status_code, 422)

    # ── GET /api/binance/rankings/social-hype ─────────────────

    def test_social_hype_list_data(self):
        with (
            self._patch_settings(True),
            patch(
                "app.api.routes.binance_signals._binance_post",
                AsyncMock(return_value=[{"rank": 1}]),
            ) as mock_post,
        ):
            response = self.client.get("/api/binance/rankings/social-hype")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [{"rank": 1}])
        self.assertEqual(
            mock_post.await_args.kwargs["cache_key"],
            "social_hype_20",
        )

    def test_social_hype_dict_rankings(self):
        with (
            self._patch_settings(True),
            patch(
                "app.api.routes.binance_signals._binance_post",
                AsyncMock(return_value={"rankings": [{"r": 1}]}),
            ),
        ):
            response = self.client.get("/api/binance/rankings/social-hype")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [{"r": 1}])

    def test_social_hype_none_returns_empty(self):
        with (
            self._patch_settings(True),
            patch(
                "app.api.routes.binance_signals._binance_post",
                AsyncMock(return_value=None),
            ),
        ):
            response = self.client.get("/api/binance/rankings/social-hype")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_social_hype_disabled(self):
        with self._patch_settings(False):
            response = self.client.get("/api/binance/rankings/social-hype")
        self.assertEqual(response.status_code, 503)

    def test_social_hype_limit_validation(self):
        response = self.client.get(
            "/api/binance/rankings/social-hype",
            params={"limit": 0},
        )
        self.assertEqual(response.status_code, 422)

    # ── GET /api/binance/rankings/trending ────────────────────

    def test_trending_list_data(self):
        with (
            self._patch_settings(True),
            patch(
                "app.api.routes.binance_signals._binance_post",
                AsyncMock(return_value=[{"t": 1}]),
            ) as mock_post,
        ):
            response = self.client.get("/api/binance/rankings/trending")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [{"t": 1}])
        self.assertEqual(
            mock_post.await_args.kwargs["cache_key"],
            "trending_20",
        )

    def test_trending_dict_rankings(self):
        with (
            self._patch_settings(True),
            patch(
                "app.api.routes.binance_signals._binance_post",
                AsyncMock(return_value={"rankings": [{"r": 1}]}),
            ),
        ):
            response = self.client.get("/api/binance/rankings/trending")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [{"r": 1}])

    def test_trending_none_returns_empty(self):
        with (
            self._patch_settings(True),
            patch(
                "app.api.routes.binance_signals._binance_post",
                AsyncMock(return_value=None),
            ),
        ):
            response = self.client.get("/api/binance/rankings/trending")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_trending_disabled(self):
        with self._patch_settings(False):
            response = self.client.get("/api/binance/rankings/trending")
        self.assertEqual(response.status_code, 503)

    def test_trending_limit_validation(self):
        response = self.client.get(
            "/api/binance/rankings/trending",
            params={"limit": 51},
        )
        self.assertEqual(response.status_code, 422)

    # ── GET /api/binance/rankings/smart-money-inflow ──────────

    def test_smart_money_inflow_list_data(self):
        with (
            self._patch_settings(True),
            patch(
                "app.api.routes.binance_signals._binance_post",
                AsyncMock(return_value=[{"i": 1}]),
            ) as mock_post,
        ):
            response = self.client.get(
                "/api/binance/rankings/smart-money-inflow",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [{"i": 1}])
        self.assertEqual(
            mock_post.await_args.kwargs["cache_key"],
            "smart_inflow_20",
        )

    def test_smart_money_inflow_dict_rankings(self):
        with (
            self._patch_settings(True),
            patch(
                "app.api.routes.binance_signals._binance_post",
                AsyncMock(return_value={"rankings": [{"r": 1}]}),
            ),
        ):
            response = self.client.get(
                "/api/binance/rankings/smart-money-inflow",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [{"r": 1}])

    def test_smart_money_inflow_none_returns_empty(self):
        with (
            self._patch_settings(True),
            patch(
                "app.api.routes.binance_signals._binance_post",
                AsyncMock(return_value=None),
            ),
        ):
            response = self.client.get(
                "/api/binance/rankings/smart-money-inflow",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_smart_money_inflow_disabled(self):
        with self._patch_settings(False):
            response = self.client.get(
                "/api/binance/rankings/smart-money-inflow",
            )
        self.assertEqual(response.status_code, 503)

    def test_smart_money_inflow_limit_validation(self):
        response = self.client.get(
            "/api/binance/rankings/smart-money-inflow",
            params={"limit": 0},
        )
        self.assertEqual(response.status_code, 422)

    # ── GET /api/binance/rankings/pnl-leaderboard ─────────────

    def test_pnl_leaderboard_list_data(self):
        with (
            self._patch_settings(True),
            patch(
                "app.api.routes.binance_signals._binance_post",
                AsyncMock(return_value=[{"p": 1}]),
            ) as mock_post,
        ):
            response = self.client.get(
                "/api/binance/rankings/pnl-leaderboard",
                params={"period": "30d"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [{"p": 1}])
        self.assertEqual(
            mock_post.await_args.args[1]["periodType"],
            "30d",
        )
        self.assertEqual(
            mock_post.await_args.kwargs["cache_key"],
            "pnl_leaders_30d_20",
        )

    def test_pnl_leaderboard_dict_rankings(self):
        with (
            self._patch_settings(True),
            patch(
                "app.api.routes.binance_signals._binance_post",
                AsyncMock(return_value={"rankings": [{"r": 1}]}),
            ),
        ):
            response = self.client.get("/api/binance/rankings/pnl-leaderboard")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [{"r": 1}])

    def test_pnl_leaderboard_none_returns_empty(self):
        with (
            self._patch_settings(True),
            patch(
                "app.api.routes.binance_signals._binance_post",
                AsyncMock(return_value=None),
            ),
        ):
            response = self.client.get("/api/binance/rankings/pnl-leaderboard")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_pnl_leaderboard_disabled(self):
        with self._patch_settings(False):
            response = self.client.get("/api/binance/rankings/pnl-leaderboard")
        self.assertEqual(response.status_code, 503)

    def test_pnl_leaderboard_limit_validation(self):
        response = self.client.get(
            "/api/binance/rankings/pnl-leaderboard",
            params={"limit": 0},
        )
        self.assertEqual(response.status_code, 422)

    # ── GET /api/binance/token/search ─────────────────────────

    def test_token_search_list_data(self):
        with (
            self._patch_settings(True),
            patch(
                "app.api.routes.binance_signals._binance_post",
                AsyncMock(return_value=[{"token": "PEPE"}]),
            ) as mock_post,
        ):
            response = self.client.get(
                "/api/binance/token/search",
                params={"query": "Pepe"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [{"token": "PEPE"}])
        self.assertEqual(mock_post.await_args.args[1]["keyword"], "Pepe")
        self.assertEqual(
            mock_post.await_args.kwargs["cache_key"],
            "token_search_pepe",
        )

    def test_token_search_dict_tokens(self):
        with (
            self._patch_settings(True),
            patch(
                "app.api.routes.binance_signals._binance_post",
                AsyncMock(return_value={"tokens": [{"t": 1}]}),
            ),
        ):
            response = self.client.get(
                "/api/binance/token/search",
                params={"query": "x"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [{"t": 1}])

    def test_token_search_dict_rows(self):
        with (
            self._patch_settings(True),
            patch(
                "app.api.routes.binance_signals._binance_post",
                AsyncMock(return_value={"rows": [{"r": 1}]}),
            ),
        ):
            response = self.client.get(
                "/api/binance/token/search",
                params={"query": "x"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [{"r": 1}])

    def test_token_search_none_returns_empty(self):
        with (
            self._patch_settings(True),
            patch(
                "app.api.routes.binance_signals._binance_post",
                AsyncMock(return_value=None),
            ),
        ):
            response = self.client.get(
                "/api/binance/token/search",
                params={"query": "x"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_token_search_disabled(self):
        with self._patch_settings(False):
            response = self.client.get(
                "/api/binance/token/search",
                params={"query": "x"},
            )
        self.assertEqual(response.status_code, 503)

    def test_token_search_requires_query(self):
        response = self.client.get("/api/binance/token/search")
        self.assertEqual(response.status_code, 422)

    def test_token_search_slices_to_ten(self):
        rows = [{"i": i} for i in range(20)]
        with (
            self._patch_settings(True),
            patch(
                "app.api.routes.binance_signals._binance_post",
                AsyncMock(return_value=rows),
            ),
        ):
            response = self.client.get(
                "/api/binance/token/search",
                params={"query": "x"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 10)

    # ── GET /api/binance/token/data ───────────────────────────

    def test_token_data_returns_payload(self):
        with (
            self._patch_settings(True),
            patch(
                "app.api.routes.binance_signals._binance_post",
                AsyncMock(return_value={"price": 0.5}),
            ) as mock_post,
        ):
            response = self.client.get(
                "/api/binance/token/data",
                params={"address": "0x123", "chain": "base"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"price": 0.5})
        self.assertEqual(
            mock_post.await_args.args[1],
            {"chainId": "8453", "address": "0x123"},
        )
        self.assertEqual(
            mock_post.await_args.kwargs["cache_key"],
            "token_data_8453_0x123",
        )

    def test_token_data_none_returns_empty_dict(self):
        with (
            self._patch_settings(True),
            patch(
                "app.api.routes.binance_signals._binance_post",
                AsyncMock(return_value=None),
            ),
        ):
            response = self.client.get(
                "/api/binance/token/data",
                params={"address": "0x123"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {})

    def test_token_data_disabled(self):
        with self._patch_settings(False):
            response = self.client.get(
                "/api/binance/token/data",
                params={"address": "0x123"},
            )
        self.assertEqual(response.status_code, 503)

    def test_token_data_requires_address(self):
        response = self.client.get("/api/binance/token/data")
        self.assertEqual(response.status_code, 422)


class BinanceDashboardTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.user = {"user_id": 1, "wallet_address": WALLET, "is_admin": False}
        app.dependency_overrides[get_current_user_from_token] = lambda: self.user
        self.addCleanup(app.dependency_overrides.clear)
        self.addCleanup(binance_signals._cache.clear)
        binance_signals._cache.clear()

    def _patch_settings(self, enabled):
        return patch(
            "app.api.routes.binance_signals.settings",
            MagicMock(binance_skills_enabled=enabled),
        )

    def test_dashboard_disabled(self):
        with self._patch_settings(False):
            response = self.client.get("/api/binance/dashboard")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["enabled"])
        self.assertEqual(data["smart_money_signals"], [])
        self.assertEqual(data["social_hype"], [])
        self.assertEqual(data["trending_tokens"], [])
        self.assertEqual(data["smart_money_inflow"], [])
        self.assertEqual(data["pnl_leaderboard"], [])
        self.assertIn("fetched_at", data)

    def test_dashboard_with_list_results(self):
        results = [
            [{"s": 1}],
            [{"h": 1}],
            [{"t": 1}],
            [{"i": 1}],
            [{"p": 1}],
        ]
        with (
            self._patch_settings(True),
            patch(
                "app.api.routes.binance_signals._binance_post",
                AsyncMock(side_effect=results),
            ),
        ):
            response = self.client.get("/api/binance/dashboard")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["enabled"])
        self.assertEqual(data["smart_money_signals"], [{"s": 1}])
        self.assertEqual(data["social_hype"], [{"h": 1}])
        self.assertEqual(data["trending_tokens"], [{"t": 1}])
        self.assertEqual(data["smart_money_inflow"], [{"i": 1}])
        self.assertEqual(data["pnl_leaderboard"], [{"p": 1}])

    def test_dashboard_with_dict_results(self):
        results = [
            {"signals": [{"s": 1}]},
            {"rankings": [{"h": 1}]},
            {"rows": [{"t": 1}]},
            {"rows": [{"i": 1}]},
            {"rankings": [{"p": 1}]},
        ]
        with (
            self._patch_settings(True),
            patch(
                "app.api.routes.binance_signals._binance_post",
                AsyncMock(side_effect=results),
            ),
        ):
            response = self.client.get("/api/binance/dashboard")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["smart_money_signals"], [{"s": 1}])
        self.assertEqual(data["social_hype"], [{"h": 1}])
        self.assertEqual(data["trending_tokens"], [{"t": 1}])
        self.assertEqual(data["smart_money_inflow"], [{"i": 1}])
        self.assertEqual(data["pnl_leaderboard"], [{"p": 1}])

    def test_dashboard_with_none_and_exceptions(self):
        results = [
            None,
            RuntimeError("boom"),
            "unexpected-string",
            {"rows": None},
            {"rankings": None},
        ]
        with (
            self._patch_settings(True),
            patch(
                "app.api.routes.binance_signals._binance_post",
                AsyncMock(side_effect=results),
            ),
        ):
            response = self.client.get("/api/binance/dashboard")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["enabled"])
        self.assertEqual(data["smart_money_signals"], [])
        self.assertEqual(data["social_hype"], [])
        self.assertEqual(data["trending_tokens"], [])
        self.assertEqual(data["smart_money_inflow"], [])
        self.assertEqual(data["pnl_leaderboard"], [])

    def test_dashboard_requires_auth(self):
        app.dependency_overrides.pop(get_current_user_from_token, None)
        try:
            response = self.client.get("/api/binance/dashboard")
        finally:
            app.dependency_overrides[get_current_user_from_token] = lambda: self.user
        self.assertEqual(response.status_code, 401)

    def test_dashboard_slices_lists_to_ten(self):
        results = [[{"i": i} for i in range(25)] for _ in range(5)]
        with (
            self._patch_settings(True),
            patch(
                "app.api.routes.binance_signals._binance_post",
                AsyncMock(side_effect=results),
            ),
        ):
            response = self.client.get("/api/binance/dashboard")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data["smart_money_signals"]), 10)


def asyncio_result(coro):
    """Run a coroutine to completion on a fresh event loop."""
    import asyncio

    return asyncio.new_event_loop().run_until_complete(coro)


if __name__ == "__main__":
    unittest.main()
