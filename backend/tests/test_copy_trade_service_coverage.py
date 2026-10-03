"""Coverage tests for ``app.services.copy_trade_service``.

Every external boundary is replaced with a double:

* the CLOB exchange client (``py_clob_client`` symbols are patched on
  their source modules, mirroring ``test_copy_trade_sell_floor.py``),
* the Polygon RPC / web3 proxy lookup,
* the DB session (a chainable ``FakeDB`` returning configured rows),
* the credential store, the idempotency store and the safety-cap gate
  (patched in the ``copy_trade_service`` namespace),
* the kelly sizing helpers and ``PolymarketService`` balance queries.

No network, database or exchange is touched.
"""

import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import app.services.copy_trade_service as copy_trade_service
from app.models.assessment import Assessment
from app.models.followed_trader import FollowedTrader
from app.models.trade_history import TradeHistory
from app.models.user import User
from app.models.user_settings import RiskMode, UserSettings
from app.models.user_trade import UserTrade
from app.security.credential_store import CredentialStoreError

WALLET = "0x" + "ab" * 20
TRADER_WALLET = "0x" + "cd" * 20
OTHER_WALLET = "0x" + "ef" * 20
TOKEN_ID = "0xtoken-yes"
MARKET_ID = "0xmarket-1"


# ────────────── Test doubles ──────────────


class FakeQuery:
    """Chainable stand-in for a SQLAlchemy query."""

    def __init__(self, db, key):
        self.db = db
        self.key = key

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def outerjoin(self, *args, **kwargs):
        return self

    def offset(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def first(self):
        result = self.db.first_results.get(self.key)
        if isinstance(result, list):
            return result[0] if result else None
        return result

    def all(self):
        result = self.db.all_results.get(self.key)
        if result is not None:
            return result
        fallback = self.db.first_results.get(self.key)
        if isinstance(fallback, list):
            return fallback
        return []

    def scalar(self):
        return self.db.scalar_result


class FakeDB:
    """In-memory DB session double."""

    def __init__(self, first_results=None, all_results=None, scalar_result=0.0):
        self.first_results = first_results or {}
        self.all_results = all_results or {}
        self.scalar_result = scalar_result
        self.added = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0

    def query(self, *args):
        key = args[0] if len(args) == 1 else tuple(args)
        return FakeQuery(self, key)

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        return None

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def refresh(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = 1

    def close(self):
        self.closed += 1


def make_settings(**overrides):
    """A UserSettings with copy-trading enabled by default."""
    values = {
        "user_id": 1,
        "copy_trading_enabled": True,
        "risk_mode": RiskMode.MAX_POSITION_DAILY_LOSS.value,
        "max_position_size": 100.0,
        "daily_loss_limit": 500.0,
        "mirror_percentage": 10.0,
        "fixed_trade_amount": 50.0,
        "kelly_fraction": 0.25,
        "require_ai_approval": False,
        "simulation_mode": False,
    }
    values.update(overrides)
    return UserSettings(**values)


def make_user(**overrides):
    values = {"id": 1, "wallet_address": WALLET, "is_admin": False}
    values.update(overrides)
    return User(**values)


def make_followed(**overrides):
    """A FollowedTrader-like double with every accessed attribute."""
    values = {
        "sizing_mode": None,
        "fixed_trade_amount_override": None,
        "copy_wallet_mode": None,
        "copy_wallet_percentage": None,
        "copy_wallet_fixed_amount": None,
        "max_position_size": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_plan(**overrides):
    plan = {
        "configured_sizing_mode": copy_trade_service.SIZING_INHERIT_GLOBAL,
        "configured_copy_wallet_mode": copy_trade_service.COPY_WALLET_DYNAMIC_PCT,
        "trade_size_pre_safety": 50.0,
        "sizing_mode_applied": "inherit_global",
        "copy_wallet_mode_applied": None,
        "trader_trade_notional": 100.0,
        "trader_wallet_balance": None,
        "copy_wallet_base": None,
        "ratio": None,
        "warning": None,
        "rejection": None,
        "details": {},
    }
    plan.update(overrides)
    return plan


def clear_proxy_cache():
    copy_trade_service._proxy_address_cache.clear()


class CopyTradeServiceTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clear_proxy_cache()
        self.addCleanup(clear_proxy_cache)


# ────────────── JSON helpers ──────────────


class JsonHelpersTests(unittest.TestCase):
    def test_dumps_loads_roundtrip_with_orjson(self):
        payload = {"a": 1, "b": [1, 2, 3]}
        raw = copy_trade_service._json_dumps(payload)
        self.assertIsInstance(raw, str)
        self.assertEqual(copy_trade_service._json_loads(raw), payload)

    def test_stdlib_fallback_when_orjson_unavailable(self):
        with (
            patch.object(copy_trade_service, "ORJSON_AVAILABLE", False),
            patch.object(copy_trade_service, "_orjson", None),
        ):
            raw = copy_trade_service._json_dumps({"k": "v"})
            self.assertEqual(raw, '{"k": "v"}')
            self.assertEqual(copy_trade_service._json_loads(raw), {"k": "v"})


# ────────────── Proxy address cache ──────────────


class ProxyCacheTests(CopyTradeServiceTestCase):
    def test_cleanup_removes_expired_entries(self):
        now = 1000.0
        copy_trade_service._set_cached_proxy("0xold", "0xp1", now_ts=now)
        copy_trade_service._set_cached_proxy("0xnew", "0xp2", now_ts=now + 5)
        with patch.object(copy_trade_service, "_PROXY_CACHE_TTL_SECONDS", 10):
            copy_trade_service._cleanup_proxy_address_cache(now_ts=now + 11)
        self.assertIsNone(copy_trade_service._get_cached_proxy("0xold", now_ts=now + 11))
        self.assertEqual(copy_trade_service._get_cached_proxy("0xnew", now_ts=now + 11), "0xp2")

    def test_cleanup_evicts_oldest_when_over_max_entries(self):
        now = 1000.0
        with patch.object(copy_trade_service, "_PROXY_CACHE_MAX_ENTRIES", 2):
            for i in range(3):
                copy_trade_service._set_cached_proxy(f"0xeoa{i}", f"0xp{i}", now_ts=now)
            copy_trade_service._cleanup_proxy_address_cache(now_ts=now)
        self.assertEqual(len(copy_trade_service._proxy_address_cache), 2)
        self.assertNotIn("0xeoa0", copy_trade_service._proxy_address_cache)

    def test_cleanup_with_default_timestamp(self):
        copy_trade_service._set_cached_proxy("0xeoa", "0xproxy")
        # No explicit timestamp → time() is used internally.
        copy_trade_service._cleanup_proxy_address_cache()
        self.assertIn("0xeoa", copy_trade_service._proxy_address_cache)

    def test_get_cached_proxy_miss(self):
        self.assertIsNone(copy_trade_service._get_cached_proxy("0xmissing"))

    def test_get_cached_proxy_hit_moves_to_end(self):
        now = 1000.0
        copy_trade_service._set_cached_proxy("0xeoa", "0xproxy", now_ts=now)
        copy_trade_service._set_cached_proxy("0xother", "0xotherproxy", now_ts=now)
        self.assertEqual(copy_trade_service._get_cached_proxy("0xeoa", now_ts=now), "0xproxy")
        self.assertEqual(list(copy_trade_service._proxy_address_cache.keys())[-1], "0xeoa")

    def test_get_cached_proxy_expired(self):
        now = 1000.0
        copy_trade_service._set_cached_proxy("0xeoa", "0xproxy", now_ts=now)
        with patch.object(copy_trade_service, "_PROXY_CACHE_TTL_SECONDS", 5):
            result = copy_trade_service._get_cached_proxy("0xeoa", now_ts=now + 6)
        self.assertIsNone(result)
        self.assertNotIn("0xeoa", copy_trade_service._proxy_address_cache)

    def test_set_cached_proxy_overwrites(self):
        now = 1000.0
        copy_trade_service._set_cached_proxy("0xeoa", "0xfirst", now_ts=now)
        copy_trade_service._set_cached_proxy("0xeoa", "0xsecond", now_ts=now + 1)
        self.assertEqual(copy_trade_service._get_cached_proxy("0xeoa", now_ts=now + 1), "0xsecond")


# ────────────── Proxy wallet resolution ──────────────


class GetPolyProxyWalletTests(CopyTradeServiceTestCase):
    def test_returns_cached_proxy_without_web3(self):
        copy_trade_service._set_cached_proxy(WALLET.lower(), "0xcached")
        with patch("web3.Web3") as web3_mock:
            result = copy_trade_service._get_poly_proxy_wallet_address(WALLET)
        self.assertEqual(result, "0xcached")
        web3_mock.assert_not_called()

    def test_all_rpcs_unreachable_returns_none(self):
        web3_mock = MagicMock()
        web3_mock.return_value.is_connected.return_value = False
        with patch("web3.Web3", web3_mock):
            result = copy_trade_service._get_poly_proxy_wallet_address(WALLET)
        self.assertIsNone(result)

    def test_resolves_and_caches_proxy(self):
        web3_mock = MagicMock()
        web3_mock.to_checksum_address.side_effect = lambda a: a
        connected = MagicMock()
        connected.is_connected.return_value = True
        web3_mock.return_value = connected
        contract = MagicMock()
        contract.functions.getPolyProxyWalletAddress.return_value.call.return_value = "0xproxy123"
        connected.eth.contract.return_value = contract
        with patch("web3.Web3", web3_mock):
            result = copy_trade_service._get_poly_proxy_wallet_address(WALLET)
        self.assertEqual(result, "0xproxy123")
        self.assertEqual(copy_trade_service._get_cached_proxy(WALLET.lower()), "0xproxy123")

    def test_zero_proxy_address_returns_none(self):
        # The service compares against a 0x + 40-zero literal.
        zero_address = "0x" + "0" * 40
        web3_mock = MagicMock()
        web3_mock.to_checksum_address.side_effect = lambda a: a
        connected = MagicMock()
        connected.is_connected.return_value = True
        web3_mock.return_value = connected
        contract = MagicMock()
        contract.functions.getPolyProxyWalletAddress.return_value.call.return_value = zero_address
        connected.eth.contract.return_value = contract
        with patch("web3.Web3", web3_mock):
            result = copy_trade_service._get_poly_proxy_wallet_address(WALLET)
        self.assertIsNone(result)

    def test_exception_returns_none(self):
        web3_mock = MagicMock()
        web3_mock.side_effect = RuntimeError("boom")
        with patch("web3.Web3", web3_mock):
            result = copy_trade_service._get_poly_proxy_wallet_address(WALLET)
        self.assertIsNone(result)

    def test_exception_outside_rpc_loop_returns_none(self):
        # An error raised outside the per-RPC suppress block
        # (e.g. address checksumming) is caught by the outer
        # handler and reported as an unresolved proxy.
        web3_mock = MagicMock()
        web3_mock.to_checksum_address.side_effect = RuntimeError("bad address")
        connected = MagicMock()
        connected.is_connected.return_value = True
        web3_mock.return_value = connected
        with patch("web3.Web3", web3_mock):
            result = copy_trade_service._get_poly_proxy_wallet_address(WALLET)
        self.assertIsNone(result)


# ────────────── Order placement ──────────────


def _fake_exchange(post_response=None):
    client = MagicMock(name="ClobClient")
    client.create_or_derive_api_creds.return_value = {"apiKey": "derived"}
    client.create_market_order.return_value = "signed-market"
    client.create_order.return_value = "signed-limit"
    client.get_tick_size.return_value = "0.01"
    if post_response is not None:
        client.post_order.return_value = post_response
    return client


class PlaceOrderTests(CopyTradeServiceTestCase):
    def _patch_exchange(self, client, proxy="0xproxy"):
        proxy_lookup = MagicMock(return_value=proxy)
        signer = MagicMock()
        signer.return_value.address.return_value = "0xeoa"
        patchers = [
            patch("py_clob_client.client.ClobClient", return_value=client),
            patch("py_clob_client.clob_types.ApiCreds", MagicMock()),
            patch("py_clob_client.clob_types.AssetType", MagicMock()),
            patch("py_clob_client.clob_types.BalanceAllowanceParams", MagicMock()),
            patch("py_clob_client.clob_types.MarketOrderArgs", MagicMock()),
            patch("py_clob_client.clob_types.OrderArgs", MagicMock()),
            patch("py_clob_client.clob_types.OrderType", MagicMock()),
            patch("py_clob_client.order_builder.constants.BUY", "BUY"),
            patch("py_clob_client.order_builder.constants.SELL", "SELL"),
            patch("py_clob_client.signer.Signer", signer),
            patch(
                "app.services.copy_trade_service._get_poly_proxy_wallet_address",
                proxy_lookup,
            ),
            patch(
                "app.services.copy_trade_service.order_landed_on_clob",
                MagicMock(return_value=""),
            ),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        return proxy_lookup

    def test_buy_success_with_stored_creds(self):
        client = _fake_exchange(post_response={"orderID": "0xorder", "status": "matched"})
        self._patch_exchange(client)
        creds = {
            "api_key": "k",
            "api_secret": "s",
            "api_passphrase": "p",
        }
        result = copy_trade_service._place_order_on_polymarket(
            "0xpk", creds, TOKEN_ID, "BUY", 0.5, 10.0
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["order_hash"], "0xorder")
        self.assertEqual(result["status"], "matched")
        client.set_api_creds.assert_called()
        client.create_or_derive_api_creds.assert_not_called()

    def test_buy_success_with_fresh_creds_and_no_proxy(self):
        client = _fake_exchange(post_response={"order_id": "0xorder2"})
        self._patch_exchange(client, proxy=None)
        result = copy_trade_service._place_order_on_polymarket(
            "0xpk", None, TOKEN_ID, "buy", 0.5, 10.0
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["order_hash"], "0xorder2")
        self.assertEqual(result["status"], "submitted")
        client.create_or_derive_api_creds.assert_called()

    def test_sell_success(self):
        client = _fake_exchange(post_response={"orderID": "0xsell"})
        self._patch_exchange(client)
        result = copy_trade_service._place_order_on_polymarket(
            "0xpk", None, TOKEN_ID, "SELL", 0.5, 10.0
        )
        self.assertTrue(result["success"])
        client.update_balance_allowance.assert_called()
        client.create_market_order.assert_called()
        client.get_tick_size.assert_called_with(TOKEN_ID)

    def test_sell_allowance_failure_is_tolerated(self):
        client = _fake_exchange(post_response={"orderID": "0xsell"})
        client.update_balance_allowance.side_effect = RuntimeError("allowance boom")
        self._patch_exchange(client)
        result = copy_trade_service._place_order_on_polymarket(
            "0xpk", None, TOKEN_ID, "SELL", 0.5, 10.0
        )
        self.assertTrue(result["success"])

    def test_zero_price_sell_uses_minimum_tick(self):
        client = _fake_exchange(post_response={"orderID": "0xsell"})
        market_args = MagicMock()
        with (
            patch("py_clob_client.client.ClobClient", return_value=client),
            patch("py_clob_client.clob_types.ApiCreds", MagicMock()),
            patch("py_clob_client.clob_types.AssetType", MagicMock()),
            patch("py_clob_client.clob_types.BalanceAllowanceParams", MagicMock()),
            patch("py_clob_client.clob_types.MarketOrderArgs", market_args),
            patch("py_clob_client.clob_types.OrderArgs", MagicMock()),
            patch("py_clob_client.clob_types.OrderType", MagicMock()),
            patch("py_clob_client.order_builder.constants.BUY", "BUY"),
            patch("py_clob_client.order_builder.constants.SELL", "SELL"),
            patch("py_clob_client.signer.Signer", MagicMock()),
            patch(
                "app.services.copy_trade_service._get_poly_proxy_wallet_address",
                MagicMock(return_value=None),
            ),
        ):
            result = copy_trade_service._place_order_on_polymarket(
                "0xpk", None, TOKEN_ID, "SELL", 0.0, 10.0
            )
        self.assertTrue(result["success"])
        market_args.assert_called_once_with(token_id=TOKEN_ID, amount=10.0, side="SELL", price=0.01)

    def test_buy_non_positive_size_fails(self):
        client = _fake_exchange()
        self._patch_exchange(client)
        result = copy_trade_service._place_order_on_polymarket(
            "0xpk", None, TOKEN_ID, "BUY", 0.5, 0.0
        )
        self.assertFalse(result["success"])
        self.assertIn("BUY size must be > 0", result["error"])

    def test_buy_non_positive_price_fails(self):
        client = _fake_exchange()
        self._patch_exchange(client)
        result = copy_trade_service._place_order_on_polymarket(
            "0xpk", None, TOKEN_ID, "BUY", 0.0, 10.0
        )
        self.assertFalse(result["success"])
        self.assertIn("BUY price must be > 0", result["error"])

    def test_rejection_with_retryable_keyword_exhausts_attempts(self):
        client = _fake_exchange(
            post_response={"success": False, "errorMsg": "insufficient balance"}
        )
        self._patch_exchange(client)
        result = copy_trade_service._place_order_on_polymarket(
            "0xpk", None, TOKEN_ID, "BUY", 0.5, 10.0
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("insufficient balance", result["error"])

    def test_rejection_non_retryable_verified_landed(self):
        client = _fake_exchange(post_response={"success": False, "errorMsg": "invalid tick size"})
        self._patch_exchange(client)
        with patch(
            "app.services.copy_trade_service.order_landed_on_clob",
            MagicMock(return_value="0xlanded"),
        ):
            result = copy_trade_service._place_order_on_polymarket(
                "0xpk", None, TOKEN_ID, "BUY", 0.5, 10.0
            )
        self.assertTrue(result["success"])
        self.assertEqual(result["order_hash"], "0xlanded")
        self.assertTrue(result["verified_after_error"])

    def test_rejection_non_retryable_ambiguous_outcome(self):
        client = _fake_exchange(post_response={"success": False, "errorMsg": "invalid tick size"})
        self._patch_exchange(client)
        with patch(
            "app.services.copy_trade_service.order_landed_on_clob",
            MagicMock(return_value=None),
        ):
            result = copy_trade_service._place_order_on_polymarket(
                "0xpk", None, TOKEN_ID, "BUY", 0.5, 10.0
            )
        self.assertFalse(result["success"])
        self.assertIn("Ambiguous order outcome", result["error"])

    def test_rejection_non_retryable_confirmed_not_landed(self):
        client = _fake_exchange(post_response={"success": False, "errorMsg": "invalid tick size"})
        self._patch_exchange(client)
        result = copy_trade_service._place_order_on_polymarket(
            "0xpk", None, TOKEN_ID, "BUY", 0.5, 10.0
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "invalid tick size")

    def test_rejection_with_error_key_and_default_reason(self):
        client = _fake_exchange(post_response={"success": False, "error": "bad order"})
        self._patch_exchange(client)
        result = copy_trade_service._place_order_on_polymarket(
            "0xpk", None, TOKEN_ID, "BUY", 0.5, 10.0
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "bad order")

    def test_rejection_without_reason_uses_default_message(self):
        client = _fake_exchange(post_response={"success": False})
        self._patch_exchange(client)
        result = copy_trade_service._place_order_on_polymarket(
            "0xpk", None, TOKEN_ID, "BUY", 0.5, 10.0
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "order rejected by exchange")

    def test_exception_with_retryable_keyword_and_no_signed_order(self):
        client = _fake_exchange()
        client.create_order.side_effect = RuntimeError("signature verification failed")
        self._patch_exchange(client)
        result = copy_trade_service._place_order_on_polymarket(
            "0xpk", None, TOKEN_ID, "BUY", 0.5, 10.0
        )
        self.assertFalse(result["success"])
        self.assertIn("signature verification failed", result["error"])

    def test_exception_non_retryable_without_signed_order(self):
        client = _fake_exchange()
        client.create_order.side_effect = RuntimeError("connection reset")
        self._patch_exchange(client)
        result = copy_trade_service._place_order_on_polymarket(
            "0xpk", None, TOKEN_ID, "BUY", 0.5, 10.0
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "connection reset")

    def test_stored_creds_missing_skips_to_fresh(self):
        client = _fake_exchange(post_response={"orderID": "0xorder"})
        self._patch_exchange(client)
        result = copy_trade_service._place_order_on_polymarket(
            "0xpk", None, TOKEN_ID, "BUY", 0.5, 10.0
        )
        self.assertTrue(result["success"])
        # (1, "stored") attempt is skipped when no creds are stored.
        client.create_or_derive_api_creds.assert_called()

    def test_gnosis_attempt_is_reached(self):
        client = _fake_exchange(post_response={"success": False, "errorMsg": "signature mismatch"})
        clob_class = MagicMock(return_value=client)
        proxy_lookup = MagicMock(return_value="0xproxy")
        signer = MagicMock()
        signer.return_value.address.return_value = "0xeoa"
        with (
            patch("py_clob_client.client.ClobClient", clob_class),
            patch("py_clob_client.clob_types.ApiCreds", MagicMock()),
            patch("py_clob_client.clob_types.AssetType", MagicMock()),
            patch("py_clob_client.clob_types.BalanceAllowanceParams", MagicMock()),
            patch("py_clob_client.clob_types.MarketOrderArgs", MagicMock()),
            patch("py_clob_client.clob_types.OrderArgs", MagicMock()),
            patch("py_clob_client.clob_types.OrderType", MagicMock()),
            patch("py_clob_client.order_builder.constants.BUY", "BUY"),
            patch("py_clob_client.order_builder.constants.SELL", "SELL"),
            patch("py_clob_client.signer.Signer", signer),
            patch(
                "app.services.copy_trade_service._get_poly_proxy_wallet_address",
                proxy_lookup,
            ),
            patch(
                "app.services.copy_trade_service.order_landed_on_clob",
                MagicMock(return_value=""),
            ),
        ):
            copy_trade_service._place_order_on_polymarket("0xpk", None, TOKEN_ID, "BUY", 0.5, 10.0)
        # Four (sig_type, creds_source) attempts are constructed; the
        # last one is POLY_GNOSIS_SAFE (sig_type=2).
        sig_types = [call.kwargs["signature_type"] for call in clob_class.call_args_list]
        self.assertEqual(sig_types, [1, 1, 0, 2])


# ────────────── Multi-phase execution ──────────────


class MultiPhaseTests(CopyTradeServiceTestCase):
    def _patch_single_phase(self, results):
        calls = []

        _PHASE_ARG_NAMES = (
            "private_key",
            "clob_creds",
            "token_id",
            "side",
            "price",
            "size",
        )

        def side_effect(*args, **kwargs):
            if kwargs:
                calls.append(kwargs)
            else:
                calls.append(dict(zip(_PHASE_ARG_NAMES, args, strict=False)))
            return results[len(calls) - 1]

        patcher = patch(
            "app.services.copy_trade_service._place_order_on_polymarket",
            MagicMock(side_effect=side_effect),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_disabled_phases_delegates_directly(self):
        calls = self._patch_single_phase(
            [{"success": True, "order_hash": "0x1", "status": "submitted"}]
        )
        result = copy_trade_service._place_order_multi_phase(
            "0xpk", None, TOKEN_ID, "BUY", 0.5, 10.0, enable_phases=False
        )
        self.assertTrue(result["success"])
        self.assertNotIn("phase", result)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["price"], 0.5)

    def test_phase_one_success(self):
        self._patch_single_phase([{"success": True, "order_hash": "0x1", "status": "submitted"}])
        result = copy_trade_service._place_order_multi_phase(
            "0xpk", None, TOKEN_ID, "BUY", 0.5, 10.0
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["phase"], 1)

    def test_phase_two_success_buy_adjusts_up(self):
        calls = self._patch_single_phase(
            [
                {"success": False, "error": "no fill"},
                {"success": True, "order_hash": "0x2", "status": "submitted"},
            ]
        )
        result = copy_trade_service._place_order_multi_phase(
            "0xpk", None, TOKEN_ID, "BUY", 0.5, 10.0
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["phase"], 2)
        self.assertEqual(result["price_adjustment"], 0.01)
        self.assertEqual(calls[1]["price"], 0.51)

    def test_phase_two_success_sell_adjusts_down(self):
        calls = self._patch_single_phase(
            [
                {"success": False, "error": "no fill"},
                {"success": True, "order_hash": "0x2", "status": "submitted"},
            ]
        )
        result = copy_trade_service._place_order_multi_phase(
            "0xpk", None, TOKEN_ID, "SELL", 0.5, 10.0
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["phase"], 2)
        self.assertEqual(calls[1]["price"], 0.49)

    def test_phase_three_success(self):
        calls = self._patch_single_phase(
            [
                {"success": False, "error": "no fill"},
                {"success": False, "error": "no fill"},
                {"success": False, "error": "no fill"},
                {"success": False, "error": "no fill"},
                {"success": True, "order_hash": "0x3", "status": "submitted"},
            ]
        )
        result = copy_trade_service._place_order_multi_phase(
            "0xpk", None, TOKEN_ID, "BUY", 0.5, 10.0
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["phase"], 3)
        self.assertEqual(result["original_size"], 10.0)
        self.assertEqual(calls[4]["size"], 5.0)
        self.assertEqual(calls[4]["price"], 0.55)

    def test_phase_three_skipped_for_small_size(self):
        self._patch_single_phase(
            [
                {"success": False, "error": "no fill"},
                {"success": False, "error": "no fill"},
                {"success": False, "error": "no fill"},
                {"success": False, "error": "no fill"},
            ]
        )
        result = copy_trade_service._place_order_multi_phase(
            "0xpk", None, TOKEN_ID, "BUY", 0.5, 1.0
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "all_phases_failed")

    def test_all_phases_failed(self):
        self._patch_single_phase(
            [
                {"success": False, "error": "no fill"},
                {"success": False, "error": "no fill"},
                {"success": False, "error": "no fill"},
                {"success": False, "error": "no fill"},
                {"success": False, "error": "no fill"},
            ]
        )
        result = copy_trade_service._place_order_multi_phase(
            "0xpk", None, TOKEN_ID, "BUY", 0.5, 10.0
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "all_phases_failed")
        self.assertEqual(result["error"], "All execution phases failed")

    def test_phase_two_price_clamped_to_bounds(self):
        calls = self._patch_single_phase(
            [
                {"success": False, "error": "no fill"},
                {"success": True, "order_hash": "0x2", "status": "submitted"},
            ]
        )
        copy_trade_service._place_order_multi_phase("0xpk", None, TOKEN_ID, "BUY", 0.995, 10.0)
        # 0.995 + 0.01 = 1.005 → clamped to 0.99.
        self.assertEqual(calls[1]["price"], 0.99)


# ────────────── Risk calculations ──────────────


class FixedAmountTests(unittest.TestCase):
    def test_followed_trader_override_wins(self):
        settings = make_settings(fixed_trade_amount=50.0)
        followed = make_followed(fixed_trade_amount_override=123.0)
        self.assertEqual(copy_trade_service._fixed_amount_for_trade(settings, followed), 123.0)

    def test_followed_trader_zero_override_falls_back(self):
        settings = make_settings(fixed_trade_amount=77.0)
        followed = make_followed(fixed_trade_amount_override=0.0)
        self.assertEqual(copy_trade_service._fixed_amount_for_trade(settings, followed), 77.0)

    def test_no_followed_trader_uses_settings(self):
        settings = make_settings(fixed_trade_amount=77.0)
        self.assertEqual(copy_trade_service._fixed_amount_for_trade(settings, None), 77.0)

    def test_settings_default_when_unset(self):
        settings = make_settings(fixed_trade_amount=None)
        self.assertEqual(copy_trade_service._fixed_amount_for_trade(settings, None), 50.0)


class StreakMultiplierTests(unittest.TestCase):
    def test_disabled_returns_one(self):
        settings = make_settings(
            dynamic_sizing_enabled=False, consecutive_wins=10, consecutive_losses=10
        )
        self.assertEqual(copy_trade_service._streak_sizing_multiplier(settings), 1.0)

    def test_loss_streaks(self):
        for losses, expected in ((5, 0.25), (4, 0.5), (3, 0.5), (2, 0.75)):
            settings = make_settings(dynamic_sizing_enabled=True, consecutive_losses=losses)
            self.assertEqual(copy_trade_service._streak_sizing_multiplier(settings), expected)

    def test_win_streaks(self):
        for wins, expected in ((5, 1.5), (4, 1.25), (3, 1.25), (2, 1.1)):
            settings = make_settings(dynamic_sizing_enabled=True, consecutive_wins=wins)
            self.assertEqual(copy_trade_service._streak_sizing_multiplier(settings), expected)

    def test_no_streak_returns_one(self):
        settings = make_settings(dynamic_sizing_enabled=True)
        self.assertEqual(copy_trade_service._streak_sizing_multiplier(settings), 1.0)


class CalculateTradeSizeTests(unittest.TestCase):
    def test_percentage_mirror(self):
        settings = make_settings(risk_mode=RiskMode.PERCENTAGE_MIRROR.value, mirror_percentage=25.0)
        size, rejection = copy_trade_service.calculate_trade_size(settings, 200.0, None)
        self.assertEqual(size, 50.0)
        self.assertIsNone(rejection)

    def test_percentage_mirror_clamps_percentage(self):
        settings = make_settings(risk_mode=RiskMode.PERCENTAGE_MIRROR.value, mirror_percentage=None)
        size, _ = copy_trade_service.calculate_trade_size(settings, 200.0, None)
        # mirror_percentage None → default 10%.
        self.assertEqual(size, 20.0)

    def test_fixed_amount_mode(self):
        settings = make_settings(risk_mode=RiskMode.FIXED_AMOUNT.value, fixed_trade_amount=75.0)
        size, rejection = copy_trade_service.calculate_trade_size(settings, 200.0, None)
        self.assertEqual(size, 75.0)
        self.assertIsNone(rejection)

    def test_fixed_amount_mode_default(self):
        settings = make_settings(risk_mode=RiskMode.FIXED_AMOUNT.value)
        settings.fixed_trade_amount = None
        size, _ = copy_trade_service.calculate_trade_size(settings, 200.0, None)
        self.assertEqual(size, 50.0)

    def test_default_mode_caps_at_max_position(self):
        settings = make_settings(max_position_size=30.0)
        size, rejection = copy_trade_service.calculate_trade_size(settings, 200.0, None)
        self.assertEqual(size, 30.0)
        self.assertIsNone(rejection)

    def test_default_mode_max_position_default(self):
        settings = make_settings()
        settings.max_position_size = None
        size, _ = copy_trade_service.calculate_trade_size(settings, 200.0, None)
        self.assertEqual(size, 100.0)

    def test_zero_size_rejected(self):
        settings = make_settings()
        size, rejection = copy_trade_service.calculate_trade_size(settings, 0.0, None)
        self.assertEqual(size, 0.0)
        self.assertEqual(rejection, "Calculated trade size is zero")

    def test_streak_multiplier_applied(self):
        settings = make_settings(
            dynamic_sizing_enabled=True, consecutive_losses=5, max_position_size=100.0
        )
        size, _ = copy_trade_service.calculate_trade_size(settings, 200.0, None)
        self.assertEqual(size, 25.0)


# ────────────── Sizing plan ──────────────


class PlanCopyTradeSizeTests(CopyTradeServiceTestCase):
    def _patch_kelly(self, p_market=0.6, edge_source="assessment", bankroll=1000.0):
        kelly_patcher = patch(
            "app.services.kelly_service.resolve_edge_probability",
            MagicMock(return_value=(p_market, edge_source)),
        )
        bankroll_patcher = patch(
            "app.services.kelly_service.compute_bankroll",
            AsyncMock(return_value=bankroll),
        )
        params_patcher = patch(
            "app.services.kelly_service.effective_trade_params",
            MagicMock(return_value=(p_market or 0.0, 0.5)),
        )
        fraction_patcher = patch(
            "app.services.kelly_service.kelly_fraction",
            MagicMock(return_value=0.1),
        )
        size_patcher = patch(
            "app.services.kelly_service.size",
            MagicMock(return_value=42.0),
        )
        service_patcher = patch(
            "app.services.copy_trade_service.get_polymarket_service",
            MagicMock(return_value=MagicMock()),
        )
        for patcher in (
            kelly_patcher,
            bankroll_patcher,
            params_patcher,
            fraction_patcher,
            size_patcher,
            service_patcher,
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_missing_source_notional_falls_back_to_fixed(self):
        settings = make_settings(fixed_trade_amount=60.0)
        plan = await copy_trade_service._plan_copy_trade_size(
            db=MagicMock(),
            settings=settings,
            followed_trader=None,
            trader_wallet=TRADER_WALLET,
            trader_trade_notional=0.0,
            user_wallet_address=WALLET,
            private_key=None,
            clob_creds=None,
        )
        self.assertEqual(plan["trade_size_pre_safety"], 60.0)
        self.assertEqual(plan["sizing_mode_applied"], "fixed_amount_fallback")
        self.assertEqual(
            plan["copy_wallet_mode_applied"],
            copy_trade_service.COPY_WALLET_FIXED_SNAPSHOT,
        )
        self.assertIn("notional missing", plan["warning"])

    async def test_fixed_amount_sizing_mode(self):
        settings = make_settings(fixed_trade_amount=60.0)
        followed = make_followed(
            sizing_mode=copy_trade_service.SIZING_FIXED_AMOUNT,
            fixed_trade_amount_override=80.0,
        )
        plan = await copy_trade_service._plan_copy_trade_size(
            db=MagicMock(),
            settings=settings,
            followed_trader=followed,
            trader_wallet=TRADER_WALLET,
            trader_trade_notional=100.0,
            user_wallet_address=WALLET,
            private_key=None,
            clob_creds=None,
        )
        self.assertEqual(plan["trade_size_pre_safety"], 80.0)
        self.assertEqual(plan["sizing_mode_applied"], "fixed_amount")

    async def test_kelly_sizing_with_edge(self):
        self._patch_kelly(p_market=0.6)
        settings = make_settings(kelly_fraction=0.25, max_position_size=200.0)
        followed = make_followed(
            sizing_mode=copy_trade_service.SIZING_KELLY, max_position_size=150.0
        )
        plan = await copy_trade_service._plan_copy_trade_size(
            db=MagicMock(),
            settings=settings,
            followed_trader=followed,
            trader_wallet=TRADER_WALLET,
            trader_trade_notional=100.0,
            user_wallet_address=WALLET,
            private_key=None,
            clob_creds=None,
            market_id=MARKET_ID,
            side="BUY",
            price=0.5,
        )
        self.assertEqual(plan["trade_size_pre_safety"], 42.0)
        self.assertEqual(plan["sizing_mode_applied"], "kelly")
        self.assertIsNone(plan["copy_wallet_mode_applied"])
        self.assertIsNone(plan["warning"])
        self.assertEqual(plan["details"]["kelly_p"], 0.6)
        self.assertEqual(plan["details"]["kelly_edge_source"], "assessment")
        self.assertEqual(plan["details"]["kelly_max_position_size"], 150.0)

    async def test_kelly_sizing_without_edge_warns(self):
        self._patch_kelly(p_market=None, edge_source="none")
        settings = make_settings()
        followed = make_followed(sizing_mode=copy_trade_service.SIZING_KELLY)
        plan = await copy_trade_service._plan_copy_trade_size(
            db=MagicMock(),
            settings=settings,
            followed_trader=followed,
            trader_wallet=TRADER_WALLET,
            trader_trade_notional=100.0,
            user_wallet_address=WALLET,
            private_key=None,
            clob_creds=None,
            market_id=MARKET_ID,
            side="BUY",
            price=0.5,
        )
        self.assertIn("No edge estimate", plan["warning"])
        # max_position_size falls back to the user setting when the
        # followed trader has none.
        self.assertEqual(plan["details"]["kelly_max_position_size"], 100.0)

    async def test_inherit_global_sizing(self):
        settings = make_settings(risk_mode=RiskMode.PERCENTAGE_MIRROR.value)
        plan = await copy_trade_service._plan_copy_trade_size(
            db=MagicMock(),
            settings=settings,
            followed_trader=None,
            trader_wallet=TRADER_WALLET,
            trader_trade_notional=100.0,
            user_wallet_address=WALLET,
            private_key=None,
            clob_creds=None,
        )
        self.assertEqual(plan["sizing_mode_applied"], "inherit_global:percentage_mirror")
        self.assertEqual(plan["trade_size_pre_safety"], 10.0)
        self.assertIsNone(plan["rejection"])

    async def test_unknown_sizing_mode_falls_back(self):
        settings = make_settings(fixed_trade_amount=60.0)
        followed = make_followed(sizing_mode="bogus_mode")
        plan = await copy_trade_service._plan_copy_trade_size(
            db=MagicMock(),
            settings=settings,
            followed_trader=followed,
            trader_wallet=TRADER_WALLET,
            trader_trade_notional=100.0,
            user_wallet_address=WALLET,
            private_key=None,
            clob_creds=None,
        )
        self.assertEqual(plan["sizing_mode_applied"], "fixed_amount_fallback")
        self.assertIn("Unknown sizing mode", plan["warning"])

    def _ratio_service(self, trader_balance=None, main_balance=None):
        trader_balance = trader_balance if trader_balance is not None else {"usdc_balance": "1000"}
        main_balance = main_balance if main_balance is not None else {"usdc_balance": "500"}
        service = MagicMock()
        service.get_wallet_balance = AsyncMock(side_effect=[trader_balance, main_balance])
        service_patcher = patch(
            "app.services.copy_trade_service.get_polymarket_service",
            MagicMock(return_value=service),
        )
        service_patcher.start()
        self.addCleanup(service_patcher.stop)
        return service

    async def test_ratio_sizing_success_dynamic_wallet(self):
        self._ratio_service()
        settings = make_settings()
        followed = make_followed(
            sizing_mode=copy_trade_service.SIZING_TRADER_WALLET_RATIO,
            copy_wallet_mode=copy_trade_service.COPY_WALLET_DYNAMIC_PCT,
            copy_wallet_percentage=50.0,
        )
        plan = await copy_trade_service._plan_copy_trade_size(
            db=MagicMock(),
            settings=settings,
            followed_trader=followed,
            trader_wallet=TRADER_WALLET,
            trader_trade_notional=100.0,
            user_wallet_address=WALLET,
            private_key=None,
            clob_creds=None,
        )
        # ratio = 100 / 1000 = 0.1; copy base = 500 * 50% = 250.
        self.assertEqual(plan["sizing_mode_applied"], "trader_wallet_ratio")
        self.assertEqual(plan["ratio"], 0.1)
        self.assertEqual(plan["trader_wallet_balance"], 1000.0)
        self.assertEqual(plan["copy_wallet_base"], 250.0)
        self.assertEqual(plan["trade_size_pre_safety"], 25.0)
        self.assertEqual(plan["details"]["copy_wallet_percentage"], 50.0)
        self.assertIsNone(plan["warning"])

    async def test_ratio_sizing_success_fixed_snapshot(self):
        self._ratio_service()
        settings = make_settings()
        followed = make_followed(
            sizing_mode=copy_trade_service.SIZING_TRADER_WALLET_RATIO,
            copy_wallet_mode=copy_trade_service.COPY_WALLET_FIXED_SNAPSHOT,
            copy_wallet_fixed_amount=75.0,
        )
        plan = await copy_trade_service._plan_copy_trade_size(
            db=MagicMock(),
            settings=settings,
            followed_trader=followed,
            trader_wallet=TRADER_WALLET,
            trader_trade_notional=100.0,
            user_wallet_address=WALLET,
            private_key=None,
            clob_creds=None,
        )
        self.assertEqual(plan["copy_wallet_base"], 75.0)
        self.assertEqual(plan["trade_size_pre_safety"], 7.5)
        self.assertEqual(
            plan["copy_wallet_mode_applied"],
            copy_trade_service.COPY_WALLET_FIXED_SNAPSHOT,
        )

    async def test_ratio_sizing_trader_balance_fetch_fails(self):
        self._ratio_service(trader_balance=RuntimeError("rpc down"))
        settings = make_settings(fixed_trade_amount=60.0)
        followed = make_followed(
            sizing_mode=copy_trade_service.SIZING_TRADER_WALLET_RATIO,
            copy_wallet_mode=copy_trade_service.COPY_WALLET_DYNAMIC_PCT,
        )
        plan = await copy_trade_service._plan_copy_trade_size(
            db=MagicMock(),
            settings=settings,
            followed_trader=followed,
            trader_wallet=TRADER_WALLET,
            trader_trade_notional=100.0,
            user_wallet_address=WALLET,
            private_key=None,
            clob_creds=None,
        )
        self.assertEqual(plan["sizing_mode_applied"], "fixed_amount_fallback")
        self.assertIn("trader wallet balance unavailable", plan["warning"])

    async def test_ratio_sizing_trader_balance_zero(self):
        self._ratio_service(trader_balance={"usdc_balance": "0"})
        settings = make_settings(fixed_trade_amount=60.0)
        followed = make_followed(
            sizing_mode=copy_trade_service.SIZING_TRADER_WALLET_RATIO,
            copy_wallet_mode=copy_trade_service.COPY_WALLET_DYNAMIC_PCT,
        )
        plan = await copy_trade_service._plan_copy_trade_size(
            db=MagicMock(),
            settings=settings,
            followed_trader=followed,
            trader_wallet=TRADER_WALLET,
            trader_trade_notional=100.0,
            user_wallet_address=WALLET,
            private_key=None,
            clob_creds=None,
        )
        self.assertEqual(plan["sizing_mode_applied"], "fixed_amount_fallback")

    async def test_ratio_sizing_fixed_snapshot_unset(self):
        self._ratio_service()
        settings = make_settings(fixed_trade_amount=60.0)
        followed = make_followed(
            sizing_mode=copy_trade_service.SIZING_TRADER_WALLET_RATIO,
            copy_wallet_mode=copy_trade_service.COPY_WALLET_FIXED_SNAPSHOT,
            copy_wallet_fixed_amount=0.0,
        )
        plan = await copy_trade_service._plan_copy_trade_size(
            db=MagicMock(),
            settings=settings,
            followed_trader=followed,
            trader_wallet=TRADER_WALLET,
            trader_trade_notional=100.0,
            user_wallet_address=WALLET,
            private_key=None,
            clob_creds=None,
        )
        self.assertIn("snapshot amount is not set", plan["warning"])
        self.assertEqual(plan["trade_size_pre_safety"], 60.0)

    async def test_ratio_sizing_main_balance_fetch_fails(self):
        self._ratio_service(main_balance=RuntimeError("rpc down"))
        settings = make_settings(fixed_trade_amount=60.0)
        followed = make_followed(
            sizing_mode=copy_trade_service.SIZING_TRADER_WALLET_RATIO,
            copy_wallet_mode=copy_trade_service.COPY_WALLET_DYNAMIC_PCT,
        )
        plan = await copy_trade_service._plan_copy_trade_size(
            db=MagicMock(),
            settings=settings,
            followed_trader=followed,
            trader_wallet=TRADER_WALLET,
            trader_trade_notional=100.0,
            user_wallet_address=WALLET,
            private_key=None,
            clob_creds=None,
        )
        self.assertIn("main wallet balance unavailable", plan["warning"])

    async def test_ratio_sizing_main_balance_zero(self):
        self._ratio_service(main_balance={"usdc_balance": "0"})
        settings = make_settings(fixed_trade_amount=60.0)
        followed = make_followed(
            sizing_mode=copy_trade_service.SIZING_TRADER_WALLET_RATIO,
            copy_wallet_mode=copy_trade_service.COPY_WALLET_DYNAMIC_PCT,
        )
        plan = await copy_trade_service._plan_copy_trade_size(
            db=MagicMock(),
            settings=settings,
            followed_trader=followed,
            trader_wallet=TRADER_WALLET,
            trader_trade_notional=100.0,
            user_wallet_address=WALLET,
            private_key=None,
            clob_creds=None,
        )
        self.assertIn("main wallet balance unavailable", plan["warning"])

    async def test_ratio_sizing_zero_percentage_falls_back(self):
        self._ratio_service()
        settings = make_settings(fixed_trade_amount=60.0)
        followed = make_followed(
            sizing_mode=copy_trade_service.SIZING_TRADER_WALLET_RATIO,
            copy_wallet_mode=copy_trade_service.COPY_WALLET_DYNAMIC_PCT,
            copy_wallet_percentage=0.0,
        )
        plan = await copy_trade_service._plan_copy_trade_size(
            db=MagicMock(),
            settings=settings,
            followed_trader=followed,
            trader_wallet=TRADER_WALLET,
            trader_trade_notional=100.0,
            user_wallet_address=WALLET,
            private_key=None,
            clob_creds=None,
        )
        self.assertIn("copy wallet base is zero", plan["warning"])

    async def test_ratio_sizing_percentage_clamped(self):
        self._ratio_service()
        settings = make_settings()
        followed = make_followed(
            sizing_mode=copy_trade_service.SIZING_TRADER_WALLET_RATIO,
            copy_wallet_mode=copy_trade_service.COPY_WALLET_DYNAMIC_PCT,
            copy_wallet_percentage=250.0,
        )
        plan = await copy_trade_service._plan_copy_trade_size(
            db=MagicMock(),
            settings=settings,
            followed_trader=followed,
            trader_wallet=TRADER_WALLET,
            trader_trade_notional=100.0,
            user_wallet_address=WALLET,
            private_key=None,
            clob_creds=None,
        )
        # 250% clamps to 100% → base = 500.
        self.assertEqual(plan["copy_wallet_base"], 500.0)
        self.assertEqual(plan["details"]["copy_wallet_percentage"], 100.0)


# ────────────── Trade attempt recording / streaks ──────────────


class RecordCopyTradeAttemptTests(CopyTradeServiceTestCase):
    def test_records_with_warning_and_reason(self):
        db = FakeDB()
        plan = make_plan(warning="careful", ratio=0.1, details={"ratio": 0.1})
        trade = copy_trade_service._record_copy_trade_attempt(
            db=db,
            user_id=1,
            market_id=MARKET_ID,
            side="BUY",
            trade_size=25.0,
            price=0.5,
            status="pending",
            trader_wallet=TRADER_WALLET,
            order_hash="0xhash",
            trade_history_id=7,
            plan=plan,
            reason="extra reason",
            token_id=TOKEN_ID,
        )
        self.assertIn("careful", trade.calculation_warning)
        self.assertIn("extra reason", trade.calculation_warning)
        self.assertEqual(trade.action, "buy")
        self.assertEqual(trade.token_id, TOKEN_ID)
        self.assertEqual(trade.source_trade_history_id, 7)
        self.assertEqual(trade.strategy_source, "copy")
        self.assertIsNone(trade.executed_at)
        self.assertEqual(db.added, [trade])
        self.assertEqual(db.commits, 1)
        details = copy_trade_service._json_loads(trade.calculation_details)
        self.assertEqual(details["ratio"], 0.1)
        self.assertEqual(details["trade_size_final"], 25.0)

    def test_reason_only_without_warning(self):
        db = FakeDB()
        plan = make_plan()
        trade = copy_trade_service._record_copy_trade_attempt(
            db=db,
            user_id=1,
            market_id=MARKET_ID,
            side="SELL",
            trade_size=1.0,
            price=0.5,
            status="failed",
            trader_wallet=TRADER_WALLET,
            order_hash=None,
            trade_history_id=None,
            plan=plan,
            reason="boom",
        )
        self.assertEqual(trade.calculation_warning, "boom")

    def test_executed_status_sets_executed_at(self):
        db = FakeDB()
        trade = copy_trade_service._record_copy_trade_attempt(
            db=db,
            user_id=1,
            market_id=MARKET_ID,
            side="BUY",
            trade_size=1.0,
            price=0.5,
            status="executed",
            trader_wallet=TRADER_WALLET,
            order_hash="0xh",
            trade_history_id=None,
            plan=make_plan(),
        )
        self.assertIsNotNone(trade.executed_at)


class UpdateStreakTests(unittest.TestCase):
    def test_win_increments_wins_and_resets_losses(self):
        settings = make_settings(consecutive_wins=2, consecutive_losses=4)
        copy_trade_service._update_streak(settings, won=True)
        self.assertEqual(settings.consecutive_wins, 3)
        self.assertEqual(settings.consecutive_losses, 0)

    def test_loss_increments_losses_and_resets_wins(self):
        settings = make_settings(consecutive_wins=2, consecutive_losses=4)
        copy_trade_service._update_streak(settings, won=False)
        self.assertEqual(settings.consecutive_losses, 5)
        self.assertEqual(settings.consecutive_wins, 0)


# ────────────── execute_copy_trade ──────────────


class ExecuteCopyTradeTests(CopyTradeServiceTestCase):
    def setUp(self):
        super().setUp()
        self.db = FakeDB()
        self.db.first_results[UserSettings] = make_settings()
        self.db.first_results[User] = make_user()
        self.db.first_results[FollowedTrader] = None

    def _patch_plan(self, plan=None, rejection=None):
        plan = plan or make_plan(rejection=rejection)
        patcher = patch(
            "app.services.copy_trade_service._plan_copy_trade_size",
            AsyncMock(return_value=plan),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return plan

    def _patch_caps(self, size=50.0, rejection=None):
        patcher = patch(
            "app.services.copy_trade_service._apply_global_safety_caps",
            MagicMock(return_value=(size, rejection)),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _patch_order(self, result):
        patcher = patch(
            "app.services.copy_trade_service._place_order_on_polymarket",
            MagicMock(return_value=result),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _patch_idempotency(self, prior=None):
        stored = []
        get_patcher = patch(
            "app.services.copy_trade_service.get_idempotent_order_result",
            MagicMock(return_value=prior),
        )
        get_patcher.start()
        self.addCleanup(get_patcher.stop)

        def store(db, user_id, key, result):
            stored.append((user_id, key, result))

        store_patcher = patch(
            "app.services.copy_trade_service.store_idempotent_order_result",
            MagicMock(side_effect=store),
        )
        store_patcher.start()
        self.addCleanup(store_patcher.stop)
        return stored

    def _patch_credentials(self, stored=None, error=None):
        def load(wallet_address):
            if error is not None:
                raise error
            return stored

        patcher = patch(
            "app.services.copy_trade_service.load_wallet_credentials",
            MagicMock(side_effect=load),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_copy_trading_disabled(self):
        self.db.first_results[UserSettings] = make_settings(copy_trading_enabled=False)
        result = await copy_trade_service.execute_copy_trade(
            self.db, 1, TRADER_WALLET, MARKET_ID, TOKEN_ID, "BUY", 0.5, 100.0
        )
        self.assertFalse(result["executed"])
        self.assertEqual(result["reason"], "Copy-trading disabled")

    async def test_no_settings_record(self):
        self.db.first_results[UserSettings] = None
        result = await copy_trade_service.execute_copy_trade(
            self.db, 1, TRADER_WALLET, MARKET_ID, TOKEN_ID, "BUY", 0.5, 100.0
        )
        self.assertEqual(result["reason"], "Copy-trading disabled")

    async def test_user_not_found(self):
        self.db.first_results[User] = None
        result = await copy_trade_service.execute_copy_trade(
            self.db, 1, TRADER_WALLET, MARKET_ID, TOKEN_ID, "BUY", 0.5, 100.0
        )
        self.assertEqual(result["reason"], "User not found")

    async def test_credential_store_error(self):
        self._patch_credentials(error=CredentialStoreError("store down"))
        result = await copy_trade_service.execute_copy_trade(
            self.db, 1, TRADER_WALLET, MARKET_ID, TOKEN_ID, "BUY", 0.5, 100.0
        )
        self.assertIn("Credential store unavailable", result["reason"])

    async def test_plan_rejection(self):
        self._patch_credentials(stored=None)
        self._patch_plan(rejection="Calculated trade size is zero")
        result = await copy_trade_service.execute_copy_trade(
            self.db, 1, TRADER_WALLET, MARKET_ID, TOKEN_ID, "BUY", 0.5, 100.0
        )
        self.assertFalse(result["executed"])
        self.assertEqual(result["reason"], "Calculated trade size is zero")

    async def test_safety_caps_rejection_records_rejected_trade(self):
        self._patch_credentials(stored=None)
        self._patch_plan()
        self._patch_caps(rejection="Daily loss limit reached")
        result = await copy_trade_service.execute_copy_trade(
            self.db, 1, TRADER_WALLET, MARKET_ID, TOKEN_ID, "BUY", 0.5, 100.0
        )
        self.assertFalse(result["executed"])
        self.assertEqual(result["reason"], "Daily loss limit reached")
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["trade_size"], 0.0)
        self.assertEqual(result["trade_id"], 1)
        rejected = self.db.added[0]
        self.assertEqual(rejected.status, "rejected")
        self.assertIn("Daily loss limit reached", rejected.calculation_warning)

    async def test_ai_gate_rejects_avoid(self):
        self.db.first_results[UserSettings] = make_settings(require_ai_approval=True)
        self._patch_credentials(stored=None)
        self._patch_plan()
        self._patch_caps()
        assessment = {"recommendation": "avoid", "ai_score": 30.0}
        result = await copy_trade_service.execute_copy_trade(
            self.db,
            1,
            TRADER_WALLET,
            MARKET_ID,
            TOKEN_ID,
            "BUY",
            0.5,
            100.0,
            trade_history_id=7,
            assessment=assessment,
        )
        self.assertFalse(result["executed"])
        self.assertEqual(result["reason"], "AI recommends: avoid")
        self.assertEqual(result["status"], "rejected")
        # Assessment persisted against the source trade.
        self.assertTrue(any(isinstance(a, Assessment) for a in self.db.added))

    async def test_ai_gate_rejects_skip_without_history(self):
        self.db.first_results[UserSettings] = make_settings(require_ai_approval=True)
        self._patch_credentials(stored=None)
        self._patch_plan()
        self._patch_caps()
        result = await copy_trade_service.execute_copy_trade(
            self.db,
            1,
            TRADER_WALLET,
            MARKET_ID,
            TOKEN_ID,
            "BUY",
            0.5,
            100.0,
            assessment={"recommendation": "skip"},
        )
        self.assertEqual(result["reason"], "AI recommends: skip")

    async def test_ai_gate_accepts_hold(self):
        self.db.first_results[UserSettings] = make_settings(require_ai_approval=True)
        self._patch_credentials(stored={"private_key": "0xpk", "clob_creds": None})
        self._patch_plan()
        self._patch_caps()
        self._patch_order({"success": True, "order_hash": "0x1", "status": "submitted"})
        self._patch_idempotency()
        result = await copy_trade_service.execute_copy_trade(
            self.db,
            1,
            TRADER_WALLET,
            MARKET_ID,
            TOKEN_ID,
            "BUY",
            0.5,
            100.0,
            trade_history_id=7,
            assessment={"recommendation": "hold"},
        )
        self.assertTrue(result["executed"])
        self.assertEqual(result["status"], "pending")

    async def test_no_credentials_rejected(self):
        self._patch_credentials(stored=None)
        self._patch_plan()
        self._patch_caps()
        result = await copy_trade_service.execute_copy_trade(
            self.db, 1, TRADER_WALLET, MARKET_ID, TOKEN_ID, "BUY", 0.5, 100.0
        )
        self.assertFalse(result["executed"])
        self.assertEqual(result["reason"], "No trading credentials (re-login required)")
        self.assertEqual(result["status"], "rejected")

    async def test_simulation_mode(self):
        self.db.first_results[UserSettings] = make_settings(simulation_mode=True)
        self._patch_credentials(stored=None)
        self._patch_plan()
        self._patch_caps()
        result = await copy_trade_service.execute_copy_trade(
            self.db, 1, TRADER_WALLET, MARKET_ID, TOKEN_ID, "BUY", 0.5, 100.0
        )
        self.assertTrue(result["executed"])
        self.assertTrue(result["simulated"])
        self.assertEqual(result["status"], "simulated")
        simulated = self.db.added[0]
        self.assertEqual(simulated.status, "simulated")
        # Streak counters advance on simulated success.
        self.assertEqual(self.db.first_results[UserSettings].consecutive_wins, 1)

    async def test_idempotent_replay(self):
        self._patch_credentials(stored={"private_key": "0xpk", "clob_creds": None})
        self._patch_plan()
        self._patch_caps()
        prior = {"executed": True, "trade_id": 99, "order_hash": "0xprior"}
        self._patch_idempotency(prior=prior)
        result = await copy_trade_service.execute_copy_trade(
            self.db,
            1,
            TRADER_WALLET,
            MARKET_ID,
            TOKEN_ID,
            "BUY",
            0.5,
            100.0,
            trade_history_id=7,
        )
        self.assertEqual(result, prior)

    async def test_order_success_stores_idempotent_result(self):
        self._patch_credentials(stored={"private_key": "0xpk", "clob_creds": None})
        self._patch_plan()
        self._patch_caps()
        self._patch_order({"success": True, "order_hash": "0x1", "status": "submitted"})
        stored = self._patch_idempotency()
        result = await copy_trade_service.execute_copy_trade(
            self.db,
            1,
            TRADER_WALLET,
            MARKET_ID,
            TOKEN_ID,
            "BUY",
            0.5,
            100.0,
            trade_history_id=7,
        )
        self.assertTrue(result["executed"])
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["order_hash"], "0x1")
        self.assertEqual(result["trade_id"], 1)
        # Streak advanced and result stored under the derived key.
        self.assertEqual(self.db.first_results[UserSettings].consecutive_wins, 1)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0][1], "copy-trade:7")
        self.assertEqual(stored[0][2]["order_hash"], "0x1")

    async def test_order_failure_advances_loss_streak(self):
        self._patch_credentials(stored={"private_key": "0xpk", "clob_creds": None})
        self._patch_plan()
        self._patch_caps()
        self._patch_order({"success": False, "error": "rejected by exchange"})
        self._patch_idempotency()
        result = await copy_trade_service.execute_copy_trade(
            self.db,
            1,
            TRADER_WALLET,
            MARKET_ID,
            TOKEN_ID,
            "BUY",
            0.5,
            100.0,
            trade_history_id=7,
        )
        self.assertFalse(result["executed"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "rejected by exchange")
        self.assertEqual(self.db.first_results[UserSettings].consecutive_losses, 1)

    async def test_explicit_idempotency_key_used(self):
        self._patch_credentials(stored={"private_key": "0xpk", "clob_creds": None})
        self._patch_plan()
        self._patch_caps()
        self._patch_order({"success": True, "order_hash": "0x1", "status": "submitted"})
        stored = self._patch_idempotency()
        await copy_trade_service.execute_copy_trade(
            self.db,
            1,
            TRADER_WALLET,
            MARKET_ID,
            TOKEN_ID,
            "BUY",
            0.5,
            100.0,
            idempotency_key="custom-key",
        )
        self.assertEqual(stored[0][1], "custom-key")

    async def test_no_idempotency_key_skips_store(self):
        self._patch_credentials(stored={"private_key": "0xpk", "clob_creds": None})
        self._patch_plan()
        self._patch_caps()
        self._patch_order({"success": True, "order_hash": "0x1", "status": "submitted"})
        stored = self._patch_idempotency()
        await copy_trade_service.execute_copy_trade(
            self.db, 1, TRADER_WALLET, MARKET_ID, TOKEN_ID, "BUY", 0.5, 100.0
        )
        self.assertEqual(stored, [])


# ────────────── Assessment persistence ──────────────


class SaveAssessmentTests(CopyTradeServiceTestCase):
    def test_saves_assessment(self):
        db = FakeDB()
        copy_trade_service._save_assessment(
            db,
            7,
            {
                "ai_score": 80.0,
                "reasoning": "strong",
                "recommendation": "strong_buy",
                "risk_level": "low",
                "market_sentiment": "bullish",
                "confidence": 90.0,
            },
        )
        self.assertEqual(len(db.added), 1)
        record = db.added[0]
        self.assertEqual(record.trade_history_id, 7)
        self.assertEqual(record.ai_score, 80.0)
        self.assertEqual(record.recommendation, "strong_buy")
        self.assertEqual(record.confidence, 90.0)

    def test_defaults_and_fallback_keys(self):
        db = FakeDB()
        copy_trade_service._save_assessment(db, 7, {"confidence": 55.0, "analysis": "text"})
        record = db.added[0]
        self.assertEqual(record.ai_score, 55.0)
        self.assertEqual(record.reasoning, "text")
        self.assertEqual(record.recommendation, "hold")
        self.assertEqual(record.risk_level, "medium")

    def test_commit_failure_rolls_back(self):
        db = FakeDB()
        db.commit = MagicMock(side_effect=RuntimeError("db down"))
        copy_trade_service._save_assessment(db, 7, {"recommendation": "buy"})
        self.assertEqual(db.rollbacks, 1)


# ────────────── History / evaluation / PnL ──────────────


class HistoryTests(CopyTradeServiceTestCase):
    def _trade(self, **overrides):
        values = {
            "id": 1,
            "user_id": 1,
            "market_id": MARKET_ID,
            "action": "buy",
            "amount": 10.0,
            "price": 0.5,
            "status": "pending",
            "order_hash": "0xh",
            "copied_from_wallet": TRADER_WALLET,
            "source_trade_history_id": 7,
            "trader_trade_notional": 100.0,
            "trader_wallet_balance": 1000.0,
            "copy_wallet_base": 250.0,
            "sizing_mode_applied": "inherit_global",
            "copy_wallet_mode_applied": None,
            "calculation_warning": None,
            "calculation_details": "{}",
            "pnl": None,
            "executed_at": None,
            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        }
        values.update(overrides)
        return UserTrade(**values)

    async def test_get_copy_trade_history(self):
        trade = self._trade(executed_at=datetime(2026, 1, 2, tzinfo=UTC), pnl=1.5)
        db = FakeDB(all_results={UserTrade: [trade]})
        history = await copy_trade_service.get_copy_trade_history(db, 1, limit=5)
        self.assertEqual(len(history), 1)
        row = history[0]
        self.assertEqual(row["id"], 1)
        self.assertEqual(row["pnl"], 1.5)
        self.assertEqual(row["executed_at"], "2026-01-02T00:00:00+00:00")
        self.assertEqual(row["created_at"], "2026-01-01T00:00:00+00:00")

    async def test_get_copy_trade_history_empty(self):
        db = FakeDB(all_results={UserTrade: []})
        self.assertEqual(await copy_trade_service.get_copy_trade_history(db, 1), [])

    def _source_trade(self, **overrides):
        values = {
            "id": 10,
            "market_id": MARKET_ID,
            "wallet_address": TRADER_WALLET.lower(),
            "order_type": "buy",
            "amount": 100.0,
            "price": 0.5,
            "notional_usdc": 50.0,
            "source_trade_id_ext": "ext-1",
            "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
        }
        values.update(overrides)
        return TradeHistory(**values)

    async def test_get_copy_trade_evaluation_with_copy(self):
        source = self._source_trade()
        copy = self._trade(
            source_trade_history_id=10,
            calculation_details=copy_trade_service._json_dumps({"ratio": 0.25}),
            executed_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
        db = FakeDB(
            all_results={
                TradeHistory: [source],
                UserTrade: [copy],
            }
        )
        rows = await copy_trade_service.get_copy_trade_evaluation(db, 1, TRADER_WALLET)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["source_trade_id"], 10)
        self.assertEqual(row["source_trade_notional"], 50.0)
        self.assertEqual(row["copy_trade_id"], 1)
        self.assertEqual(row["ratio"], 0.25)
        self.assertEqual(row["copy_status"], "pending")
        self.assertEqual(row["side"], "BUY")

    async def test_get_copy_trade_evaluation_not_copied(self):
        source = self._source_trade(notional_usdc=None)
        db = FakeDB(all_results={TradeHistory: [source], UserTrade: []})
        rows = await copy_trade_service.get_copy_trade_evaluation(db, 1, TRADER_WALLET)
        row = rows[0]
        self.assertEqual(row["copy_status"], "not_copied")
        self.assertIsNone(row["copy_trade_id"])
        # notional_usdc None → amount * price fallback.
        self.assertEqual(row["source_trade_notional"], 50.0)

    async def test_get_copy_trade_evaluation_ratio_fallback(self):
        source = self._source_trade()
        copy = self._trade(
            source_trade_history_id=10,
            calculation_details="not-json{",
            trader_trade_notional=100.0,
            trader_wallet_balance=400.0,
        )
        db = FakeDB(all_results={TradeHistory: [source], UserTrade: [copy]})
        rows = await copy_trade_service.get_copy_trade_evaluation(db, 1, TRADER_WALLET)
        # Invalid JSON details → ratio recomputed from notional/balance.
        self.assertEqual(rows[0]["ratio"], 0.25)

    async def test_get_copy_trade_evaluation_no_timestamp(self):
        source = self._source_trade(timestamp=None)
        db = FakeDB(all_results={TradeHistory: [source], UserTrade: []})
        rows = await copy_trade_service.get_copy_trade_evaluation(db, 1, TRADER_WALLET)
        self.assertIsNone(rows[0]["source_timestamp"])

    async def test_get_daily_copy_pnl(self):
        db = FakeDB(scalar_result=-12.5)
        pnl = await copy_trade_service.get_daily_copy_pnl(db, 1)
        self.assertEqual(pnl, -12.5)

    async def test_get_daily_copy_pnl_none_result(self):
        db = FakeDB(scalar_result=None)
        self.assertEqual(await copy_trade_service.get_daily_copy_pnl(db, 1), 0.0)


# ────────────── Aggregated trades ──────────────


class ExecuteAggregatedTradeTests(CopyTradeServiceTestCase):
    def _patch_session(self, db):
        patcher = patch("app.services.copy_trade_service.SessionLocal", MagicMock(return_value=db))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _patch_decrypt(self, private_key="0xpk"):
        # NOTE: get_decrypted_private_key does not actually exist in
        # app.security.credential_store (see bug report); create=True
        # lets the tests exercise the intended code paths.
        patcher = patch(
            "app.security.credential_store.get_decrypted_private_key",
            MagicMock(return_value=private_key),
            create=True,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _patch_multi_phase(self, result):
        patcher = patch(
            "app.services.copy_trade_service._place_order_multi_phase",
            MagicMock(return_value=result),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_invalid_params(self):
        for aggregated in (
            {},
            {"user_id": 1, "aggregated_size": 0, "vwap_price": 0.5},
            {"user_id": 1, "aggregated_size": 1, "vwap_price": 0},
        ):
            result = await copy_trade_service.execute_aggregated_trade(aggregated)
            self.assertFalse(result["success"])
            self.assertEqual(result["error"], "Invalid aggregated trade parameters")

    async def test_user_not_found(self):
        db = FakeDB(first_results={User: None})
        self._patch_session(db)
        result = await copy_trade_service.execute_aggregated_trade(
            {
                "user_id": 1,
                "token_id": TOKEN_ID,
                "side": "BUY",
                "aggregated_size": 5.0,
                "vwap_price": 0.5,
            }
        )
        self.assertFalse(result["success"])
        self.assertIn("not found", result["error"])
        self.assertEqual(db.closed, 1)

    async def test_no_private_key(self):
        db = FakeDB(first_results={User: make_user()})
        self._patch_session(db)
        self._patch_decrypt(private_key=None)
        result = await copy_trade_service.execute_aggregated_trade(
            {
                "user_id": 1,
                "token_id": TOKEN_ID,
                "side": "BUY",
                "aggregated_size": 5.0,
                "vwap_price": 0.5,
            }
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "No private key available")

    async def test_success(self):
        db = FakeDB(first_results={User: make_user()})
        self._patch_session(db)
        self._patch_decrypt()
        self._patch_multi_phase({"success": True, "order_hash": "0xagg", "phase": 2})
        result = await copy_trade_service.execute_aggregated_trade(
            {
                "user_id": 1,
                "token_id": TOKEN_ID,
                "side": "BUY",
                "aggregated_size": 5.0,
                "vwap_price": 0.5,
            }
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["order_hash"], "0xagg")
        self.assertEqual(db.closed, 1)

    async def test_exception_returns_error(self):
        db = FakeDB(first_results={User: make_user()})
        db.query = MagicMock(side_effect=RuntimeError("db exploded"))
        self._patch_session(db)
        result = await copy_trade_service.execute_aggregated_trade(
            {
                "user_id": 1,
                "token_id": TOKEN_ID,
                "side": "BUY",
                "aggregated_size": 5.0,
                "vwap_price": 0.5,
            }
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "db exploded")
        self.assertEqual(db.closed, 1)

    async def test_missing_get_decrypted_private_key_is_caught(self):
        # BUG: app.security.credential_store has no
        # get_decrypted_private_key, so the lazy import inside
        # execute_aggregated_trade raises ImportError for every
        # valid user. The broad except handler converts it into a
        # failed result instead of crashing the caller.
        db = FakeDB(first_results={User: make_user()})
        self._patch_session(db)
        result = await copy_trade_service.execute_aggregated_trade(
            {
                "user_id": 1,
                "token_id": TOKEN_ID,
                "side": "BUY",
                "aggregated_size": 5.0,
                "vwap_price": 0.5,
            }
        )
        self.assertFalse(result["success"])
        self.assertIn("get_decrypted_private_key", result["error"])
        self.assertEqual(db.closed, 1)


if __name__ == "__main__":
    unittest.main()
