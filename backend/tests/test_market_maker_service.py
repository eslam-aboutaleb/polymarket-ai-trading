"""Tests for :mod:`app.services.market_maker_service`.

Two layers are covered:

* **Pure strategy maths** — ``_validate_max_bands``, ``_compute_bands_orders``
  and ``_compute_amm_orders`` are total functions of a midpoint plus a config
  namespace, so every branch (band-count ceiling, price-bound pruning,
  collateral caps, the AMM curve's ``share_delta`` gate, the
  ``num_bands or 5`` default) is asserted on exact order lists.
* **I/O boundary helpers** — ``_fetch_midpoint``, ``_get_open_orders``,
  ``_cancel_order``, ``_cancel_all_orders`` and ``_place_maker_order`` talk to
  httpx / ``py_clob_client`` / the credential store. Every one of them is
  driven with fakes so no socket, chain node or key is ever touched, and the
  tests assert the exact request the module would have sent.

The orchestration layer (``_sync_market_maker``, ``_maker_loop`` and the
``start``/``stop`` registry) is covered with a scripted session double so the
persisted counters, status transitions and guard-rail returns are checked
rather than merely executed.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from py_clob_client.clob_types import OpenOrderParams

import app.services.market_maker_service as market_maker_service
from app.services.market_maker_service import MAX_BANDS

CLOB_API = market_maker_service.POLYMARKET_CLOB_API


# ─────────────── config doubles ───────────────


def _config(**overrides) -> SimpleNamespace:
    """A MarketMakerConfig stand-in with every field the calculators read."""
    base = {
        "id": 1,
        "user_id": 7,
        "condition_id": "cond-1",
        "token_id_yes": "token-yes",
        "token_id_no": "token-no",
        "strategy": "bands",
        "enabled": True,
        "num_bands": 3,
        "min_spread": 0.02,
        "max_spread": 0.10,
        "band_order_size": 10.0,
        "amm_liquidity": 1000.0,
        "max_collateral": 1000.0,
        "sync_interval_seconds": 30,
        "min_order_size": 0.1,
        "min_price": 0.01,
        "max_price": 0.99,
        "status": "idle",
        "last_sync_at": None,
        "last_error": None,
        "total_orders_placed": 0,
        "total_orders_cancelled": 0,
        "total_volume_usdc": 0.0,
        "current_open_orders": 0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _user(user_id: int = 7, wallet: str = "0xwallet") -> SimpleNamespace:
    return SimpleNamespace(id=user_id, wallet_address=wallet)


def _no_paper_settings() -> SimpleNamespace:
    """A UserSettings double with paper mode off."""
    return SimpleNamespace(simulation_mode=False)


def _session(first_results=(), all_results=()) -> MagicMock:
    """A session double whose ``.first()``/``.all()`` results are scripted.

    ``db.query(...)`` always returns the same chainable mock, so a positional
    ``side_effect`` list reads as "the n-th row this session is asked for".
    """
    db = MagicMock(name="db")
    query = MagicMock(name="query")
    db.query.return_value = query
    if first_results:
        results = list(first_results)

        def _first():
            # SQLAlchemy semantics: once the scripted rows are
            # exhausted, further .first() calls return None
            # (e.g. the per-user settings lookup). Exception
            # instances in the script are raised, matching the
            # old list-based side_effect behaviour.
            if not results:
                return None
            result = results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        query.filter.return_value.first.side_effect = _first
    else:
        query.filter.return_value.first.return_value = None
    if all_results is not None:
        query.filter.return_value.all.return_value = list(all_results)
    else:
        query.filter.return_value.all.return_value = []
    return db


# ─────────────── httpx doubles ───────────────


class _FakeAsyncResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self) -> dict:
        return self._payload


class _FakeAsyncClient:
    """Stands in for ``httpx.AsyncClient``; records constructor + GET args."""

    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.init_kwargs: dict = {}
        self.get_calls: list[tuple[str, dict | None]] = []

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False

    async def get(self, url, params=None):
        self.get_calls.append((url, params))
        if self.error is not None:
            raise self.error
        return self.response


def _async_client_patch(client: _FakeAsyncClient):
    def _factory(**kwargs):
        client.init_kwargs = kwargs
        return client

    return patch("httpx.AsyncClient", _factory)


# ─────────────── py_clob_client doubles ───────────────


def _fake_clob(orders=None, error: Exception | None = None) -> MagicMock:
    clob = MagicMock(name="ClobClient")
    clob.create_or_derive_api_creds.return_value = {"apiKey": "derived"}
    clob.get_orders.return_value = orders
    if error is not None:
        clob.get_orders.side_effect = error
    return clob


# ─────────────── band-count validation ───────────────


class ValidateMaxBandsTests(unittest.TestCase):
    def test_allows_exactly_max_bands(self):
        # Boundary: the ceiling itself is legal, only ``> MAX_BANDS`` raises.
        self.assertIsNone(market_maker_service._validate_max_bands(MAX_BANDS))

    def test_rejects_one_above_max_bands(self):
        with self.assertRaises(ValueError) as ctx:
            market_maker_service._validate_max_bands(MAX_BANDS + 1)
        self.assertEqual(str(ctx.exception), "Max 20 bands allowed")

    def test_allows_zero_and_negative_band_counts(self):
        # Only the upper bound is guarded; the floor is handled by callers.
        self.assertIsNone(market_maker_service._validate_max_bands(0))
        self.assertIsNone(market_maker_service._validate_max_bands(-5))


# ─────────────── bands strategy ───────────────


class BandsOrderCalculationTests(unittest.TestCase):
    def test_symmetric_bands_around_midpoint(self):
        config = _config(num_bands=3, min_spread=0.02, max_spread=0.10)

        orders = market_maker_service._compute_bands_orders(0.5, config)

        self.assertEqual(
            orders,
            [
                {"side": "BUY", "price": 0.48, "size": 10.0, "token_id": "token-yes", "band": 0},
                {"side": "SELL", "price": 0.52, "size": 10.0, "token_id": "token-yes", "band": 0},
                {"side": "BUY", "price": 0.44, "size": 10.0, "token_id": "token-yes", "band": 1},
                {"side": "SELL", "price": 0.56, "size": 10.0, "token_id": "token-yes", "band": 1},
                {"side": "BUY", "price": 0.4, "size": 10.0, "token_id": "token-yes", "band": 2},
                {"side": "SELL", "price": 0.6, "size": 10.0, "token_id": "token-yes", "band": 2},
            ],
        )

    def test_single_band_ignores_max_spread(self):
        # num_bands == 1 forces a zero step, so the band sits at min_spread
        # even though max_spread is far away.
        config = _config(num_bands=1, min_spread=0.02, max_spread=0.50)

        orders = market_maker_service._compute_bands_orders(0.5, config)

        self.assertEqual([o["price"] for o in orders], [0.48, 0.52])
        self.assertEqual([o["band"] for o in orders], [0, 0])

    def test_zero_num_bands_returns_no_orders(self):
        self.assertEqual(
            market_maker_service._compute_bands_orders(0.5, _config(num_bands=0)),
            [],
        )

    def test_non_positive_midpoint_returns_no_orders(self):
        self.assertEqual(market_maker_service._compute_bands_orders(0.0, _config()), [])
        self.assertEqual(market_maker_service._compute_bands_orders(-0.5, _config()), [])

    def test_band_ceiling_is_checked_after_the_early_return(self):
        # An out-of-range band count is only rejected once the cheap guards
        # pass, so a zero midpoint short-circuits before the ValueError.
        config = _config(num_bands=MAX_BANDS + 1)
        self.assertEqual(market_maker_service._compute_bands_orders(0.0, config), [])

        with self.assertRaisesRegex(ValueError, "Max 20 bands allowed"):
            market_maker_service._compute_bands_orders(0.5, config)

    def test_exactly_max_bands_is_accepted(self):
        config = _config(
            num_bands=MAX_BANDS,
            min_spread=0.001,
            max_spread=0.02,
            band_order_size=1.0,
        )
        orders = market_maker_service._compute_bands_orders(0.5, config)
        self.assertEqual(len(orders), MAX_BANDS * 2)
        self.assertEqual(orders[0]["price"], 0.499)
        self.assertEqual(orders[-1]["price"], 0.52)

    def test_collateral_cap_stops_further_bands(self):
        # 10 USDC per leg with a 25 USDC cap admits only the first pair.
        config = _config(max_collateral=25.0)

        orders = market_maker_service._compute_bands_orders(0.5, config)

        self.assertEqual([o["side"] for o in orders], ["BUY", "SELL"])
        self.assertEqual([o["price"] for o in orders], [0.48, 0.52])

    def test_collateral_cap_boundary_is_inclusive(self):
        # Exactly enough collateral for two legs is accepted ...
        orders = market_maker_service._compute_bands_orders(0.5, _config(max_collateral=20.0))
        self.assertEqual([o["side"] for o in orders], ["BUY", "SELL"])

        # ... and one USDC less still affords the first leg but not the second.
        orders = market_maker_service._compute_bands_orders(0.5, _config(max_collateral=19.999))
        self.assertEqual([(o["side"], o["price"]) for o in orders], [("BUY", 0.48)])

    def test_min_price_prunes_bids_only(self):
        config = _config(min_price=0.45)

        orders = market_maker_service._compute_bands_orders(0.5, config)

        self.assertEqual([o["side"] for o in orders], ["BUY", "SELL", "SELL", "SELL"])
        self.assertEqual([o["price"] for o in orders], [0.48, 0.52, 0.56, 0.6])

    def test_max_price_prunes_asks_only(self):
        config = _config(max_price=0.55)

        orders = market_maker_service._compute_bands_orders(0.5, config)

        self.assertEqual([o["side"] for o in orders], ["BUY", "SELL", "BUY", "BUY"])
        self.assertEqual([o["price"] for o in orders], [0.48, 0.52, 0.44, 0.4])

    def test_zero_spread_places_nothing_on_either_side(self):
        # A band at exactly the midpoint fails the strict
        # ``bid < mid`` / ``ask > mid`` comparisons.
        config = _config(num_bands=2, min_spread=0.0, max_spread=0.0)
        self.assertEqual(market_maker_service._compute_bands_orders(0.5, config), [])

    def test_order_size_comes_from_band_order_size(self):
        config = _config(num_bands=1, band_order_size=3.5)
        orders = market_maker_service._compute_bands_orders(0.5, config)
        self.assertEqual([o["size"] for o in orders], [3.5, 3.5])


# ─────────────── AMM strategy ───────────────


class AmmOrderCalculationTests(unittest.TestCase):
    def test_symmetric_levels_around_midpoint(self):
        config = _config(num_bands=3, min_spread=0.02, max_spread=0.10)

        orders = market_maker_service._compute_amm_orders(0.5, config)

        self.assertEqual(
            orders,
            [
                {"side": "BUY", "price": 0.48, "size": 10.0, "token_id": "token-yes", "band": 0},
                {"side": "SELL", "price": 0.52, "size": 10.0, "token_id": "token-yes", "band": 0},
                {"side": "BUY", "price": 0.44, "size": 10.0, "token_id": "token-yes", "band": 1},
                {"side": "SELL", "price": 0.56, "size": 10.0, "token_id": "token-yes", "band": 1},
                {"side": "BUY", "price": 0.4, "size": 10.0, "token_id": "token-yes", "band": 2},
                {"side": "SELL", "price": 0.6, "size": 10.0, "token_id": "token-yes", "band": 2},
            ],
        )

    def test_curve_gate_suppresses_bids_but_not_asks(self):
        # share_delta for L=1000, size=10, mid=0.5 is ~19.61, so a 25-share
        # floor rejects every bid while asks are unaffected (asks are never
        # curve-gated). Two levels means a single 0.08 step: 0.02 then 0.10.
        config = _config(num_bands=2, min_order_size=25.0)

        orders = market_maker_service._compute_amm_orders(0.5, config)

        self.assertEqual([o["side"] for o in orders], ["SELL", "SELL"])
        self.assertEqual([o["price"] for o in orders], [0.52, 0.6])

    def test_zero_num_bands_falls_back_to_five_levels(self):
        config = _config(num_bands=0, min_spread=0.02, max_spread=0.10)

        orders = market_maker_service._compute_amm_orders(0.5, config)

        # Five levels means a step of 0.02: 0.02, 0.04, 0.06, 0.08, 0.10.
        self.assertEqual(len(orders), 10)
        self.assertEqual([o["band"] for o in orders], [0, 0, 1, 1, 2, 2, 3, 3, 4, 4])
        self.assertEqual(
            [o["price"] for o in orders],
            [0.48, 0.52, 0.46, 0.54, 0.44, 0.56, 0.42, 0.58, 0.4, 0.6],
        )

    def test_single_level_sits_at_min_spread(self):
        # The AMM step is not special-cased for one level (the divisor is
        # clamped to 1), but the only iteration is i == 0, so the offset
        # collapses back to min_spread and matches the bands strategy.
        config = _config(num_bands=1, min_spread=0.02, max_spread=0.10)

        orders = market_maker_service._compute_amm_orders(0.5, config)

        self.assertEqual([(o["side"], o["price"]) for o in orders], [("BUY", 0.48), ("SELL", 0.52)])

    def test_zero_num_bands_differs_between_strategies(self):
        # AMM substitutes a five-level default where bands treats the config
        # as "no orders at all".
        config = _config(num_bands=0)

        self.assertEqual(market_maker_service._compute_bands_orders(0.5, config), [])
        self.assertEqual(len(market_maker_service._compute_amm_orders(0.5, config)), 10)

    def test_non_positive_midpoint_returns_no_orders(self):
        self.assertEqual(market_maker_service._compute_amm_orders(0.0, _config()), [])
        self.assertEqual(market_maker_service._compute_amm_orders(-1.0, _config()), [])

    def test_non_positive_liquidity_returns_no_orders(self):
        self.assertEqual(
            market_maker_service._compute_amm_orders(0.5, _config(amm_liquidity=0)), []
        )

    def test_band_ceiling_is_checked_after_the_early_returns(self):
        # Zero liquidity short-circuits before validation.
        self.assertEqual(
            market_maker_service._compute_amm_orders(
                0.5, _config(num_bands=MAX_BANDS + 1, amm_liquidity=0)
            ),
            [],
        )

        with self.assertRaisesRegex(ValueError, "Max 20 bands allowed"):
            market_maker_service._compute_amm_orders(0.5, _config(num_bands=MAX_BANDS + 1))

    def test_non_positive_bid_price_is_dropped(self):
        # A 0.6 offset under a 0.5 midpoint puts the bid at -0.1; the ask is
        # still allowed because max_price was widened to admit it.
        config = _config(num_bands=1, min_spread=0.6, max_spread=0.6, max_price=1.5)

        orders = market_maker_service._compute_amm_orders(0.5, config)

        self.assertEqual([(o["side"], o["price"]) for o in orders], [("SELL", 1.1)])

    def test_max_price_prunes_asks(self):
        config = _config(num_bands=2, max_price=0.55)

        orders = market_maker_service._compute_amm_orders(0.5, config)

        self.assertEqual([o["side"] for o in orders], ["BUY", "SELL", "BUY"])

    def test_collateral_cap_stops_further_levels(self):
        orders = market_maker_service._compute_amm_orders(0.5, _config(max_collateral=25.0))
        self.assertEqual([(o["side"], o["price"]) for o in orders], [("BUY", 0.48), ("SELL", 0.52)])


# ─────────────── midpoint fetching ───────────────


class FetchMidpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_rounded_mid_of_best_bid_and_ask(self):
        client = _FakeAsyncClient(
            _FakeAsyncResponse(
                payload={"bids": [{"price": "0.4511"}], "asks": [{"price": "0.5521"}]}
            )
        )

        with _async_client_patch(client):
            midpoint = await market_maker_service._fetch_midpoint("token-yes")

        self.assertEqual(midpoint, 0.5016)
        self.assertEqual(client.get_calls, [(f"{CLOB_API}/book", {"token_id": "token-yes"})])
        self.assertEqual(client.init_kwargs, {"timeout": 10})

    async def test_only_first_book_level_is_used(self):
        client = _FakeAsyncClient(
            _FakeAsyncResponse(
                payload={
                    "bids": [{"price": 0.40}, {"price": 0.10}],
                    "asks": [{"price": 0.60}, {"price": 0.90}],
                }
            )
        )

        with _async_client_patch(client):
            midpoint = await market_maker_service._fetch_midpoint("token-yes")

        self.assertEqual(midpoint, 0.5)

    async def test_non_200_returns_none(self):
        client = _FakeAsyncClient(_FakeAsyncResponse(status_code=503))

        with _async_client_patch(client):
            self.assertIsNone(await market_maker_service._fetch_midpoint("token-yes"))

    async def test_missing_or_empty_sides_return_none(self):
        for payload in ({}, {"bids": []}, {"asks": []}, {"bids": [{"price": 0.4}]}):
            with self.subTest(payload=payload):
                client = _FakeAsyncClient(_FakeAsyncResponse(payload=payload))
                with _async_client_patch(client):
                    self.assertIsNone(await market_maker_service._fetch_midpoint("token-yes"))

    async def test_transport_error_returns_none(self):
        client = _FakeAsyncClient(error=RuntimeError("connection reset"))

        with _async_client_patch(client):
            self.assertIsNone(await market_maker_service._fetch_midpoint("token-yes"))


# ─────────────── open-order / cancel helpers ───────────────


class GetOpenOrdersTests(unittest.IsolatedAsyncioTestCase):
    async def test_derives_creds_and_queries_live_orders(self):
        clob = _fake_clob(orders=[{"id": "o1"}, {"id": "o2"}])

        with patch("py_clob_client.client.ClobClient", return_value=clob) as clob_cls:
            orders = await market_maker_service._get_open_orders("0xpk", "token-yes")

        self.assertEqual(orders, [{"id": "o1"}, {"id": "o2"}])
        clob_cls.assert_called_once_with(host=CLOB_API, chain_id=137, key="0xpk", signature_type=1)
        clob.create_or_derive_api_creds.assert_called_once_with()
        clob.set_api_creds.assert_called_once_with({"apiKey": "derived"})
        # get_orders needs an OpenOrderParams object, not a dict: passing a dict
        # raised AttributeError inside py-clob-client, which the caller swallowed,
        # so nothing was ever cancelled.
        params = clob.get_orders.call_args.kwargs["params"]
        self.assertIsInstance(params, OpenOrderParams)
        self.assertEqual(params.asset_id, "token-yes")

    async def test_non_live_orders_are_filtered_out(self):
        """OpenOrderParams has no `state` field, so live filtering happens here."""
        clob = _fake_clob(
            orders=[
                {"id": "live-1", "status": "live"},
                {"id": "matched", "status": "MATCHED"},
            ]
        )

        with patch("py_clob_client.client.ClobClient", return_value=clob):
            orders = await market_maker_service._get_open_orders("0xpk", "token-yes")

        self.assertEqual(orders, [{"id": "live-1", "status": "live"}])

    async def test_non_list_response_is_normalised_to_empty(self):
        with patch("py_clob_client.client.ClobClient", return_value=_fake_clob(orders={"o": 1})):
            self.assertEqual(await market_maker_service._get_open_orders("0xpk", "token-yes"), [])

    async def test_client_error_returns_empty(self):
        with patch(
            "py_clob_client.client.ClobClient", return_value=_fake_clob(error=ValueError("401"))
        ):
            self.assertEqual(await market_maker_service._get_open_orders("0xpk", "token-yes"), [])


class CancelOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_cancel_returns_true(self):
        clob = _fake_clob()

        with patch("py_clob_client.client.ClobClient", return_value=clob) as clob_cls:
            result = await market_maker_service._cancel_order("0xpk", "order-9")

        self.assertIs(result, True)
        clob_cls.assert_called_once_with(host=CLOB_API, chain_id=137, key="0xpk", signature_type=1)
        clob.cancel.assert_called_once_with("order-9")

    async def test_failure_returns_false(self):
        clob = _fake_clob()
        clob.cancel.side_effect = RuntimeError("order unknown")

        with patch("py_clob_client.client.ClobClient", return_value=clob):
            result = await market_maker_service._cancel_order("0xpk", "order-9")

        self.assertIs(result, False)


class CancelAllOrdersTests(unittest.IsolatedAsyncioTestCase):
    async def test_counts_only_confirmed_cancels(self):
        open_orders = [
            {"id": "a"},
            {"order_id": "b"},
            {"nothing": "useful"},
            {"id": "c"},
        ]
        cancel = AsyncMock(side_effect=[True, True, False])

        with (
            patch.object(
                market_maker_service,
                "_get_open_orders",
                new=AsyncMock(return_value=open_orders),
            ) as get_orders,
            patch.object(market_maker_service, "_cancel_order", new=cancel),
        ):
            cancelled = await market_maker_service._cancel_all_orders("0xpk", "token-yes")

        get_orders.assert_awaited_once_with("0xpk", "token-yes")
        self.assertEqual(cancelled, 2)
        self.assertEqual(
            [call.args for call in cancel.await_args_list],
            [("0xpk", "a"), ("0xpk", "b"), ("0xpk", "c")],
        )

    async def test_no_open_orders_cancels_nothing(self):
        cancel = AsyncMock()

        with (
            patch.object(market_maker_service, "_get_open_orders", new=AsyncMock(return_value=[])),
            patch.object(market_maker_service, "_cancel_order", new=cancel),
        ):
            self.assertEqual(await market_maker_service._cancel_all_orders("0xpk", "token-yes"), 0)

        cancel.assert_not_awaited()


# ─────────────── order placement ───────────────


class _PlacementPatches:
    """Context manager bundling every symbol ``_place_maker_order`` imports.

    The function does its imports lazily inside the body, so each name has to
    be patched on the *defining* module rather than on the service module.
    """

    def __init__(self, clob: MagicMock, proxy: str | None = "0xproxy") -> None:
        self.clob = clob
        self.order_args = MagicMock(name="OrderArgs")
        self.order_type = MagicMock(name="OrderType")
        self.signer = MagicMock(name="Signer")
        self.signer.return_value.address.return_value = "0xeoa"
        self.proxy_lookup = MagicMock(name="_get_poly_proxy_wallet_address")
        self.proxy_lookup.return_value = proxy
        self._managers = [
            patch("py_clob_client.client.ClobClient", return_value=clob),
            patch("py_clob_client.clob_types.OrderArgs", self.order_args),
            patch("py_clob_client.clob_types.OrderType", self.order_type),
            patch("py_clob_client.order_builder.constants.BUY", "BUY"),
            patch("py_clob_client.order_builder.constants.SELL", "SELL"),
            patch("py_clob_client.signer.Signer", self.signer),
            patch(
                "app.services.copy_trade_service._get_poly_proxy_wallet_address",
                self.proxy_lookup,
            ),
        ]

    def __enter__(self):
        for manager in self._managers:
            manager.start()
        return self

    def __exit__(self, *_exc) -> bool:
        for manager in reversed(self._managers):
            manager.stop()
        return False


class PlaceMakerOrderTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _clob(post_response=None, create_error: Exception | None = None) -> MagicMock:
        clob = _fake_clob()
        clob.create_order.return_value = "signed"
        if create_error is not None:
            clob.create_order.side_effect = create_error
        if post_response is not None:
            clob.post_order.return_value = post_response
        return clob

    async def test_buy_converts_notional_to_shares(self):
        clob = self._clob(post_response={"orderID": "0xorder"})

        with _PlacementPatches(clob) as patches:
            result = await market_maker_service._place_maker_order(
                "0xpk", "token-yes", "buy", 0.5, 10.0
            )

        self.assertEqual(result, "0xorder")
        # BUY size is a USDC notional, converted to shares at the order price.
        patches.order_args.assert_called_once_with(
            token_id="token-yes", price=0.5, size=20.0, side="BUY"
        )
        self.assertEqual(clob.post_order.call_args.args, ("signed", patches.order_type.GTC))
        clob.create_order.assert_called_once_with(patches.order_args.return_value)
        clob.set_api_creds.assert_called_once_with({"apiKey": "derived"})

    async def test_sell_passes_size_through_unchanged(self):
        clob = self._clob(post_response={"id": "abc"})

        with _PlacementPatches(clob) as patches:
            result = await market_maker_service._place_maker_order(
                "0xpk", "token-yes", "SELL", 0.42, 17.25
            )

        self.assertEqual(result, "abc")
        patches.order_args.assert_called_once_with(
            token_id="token-yes", price=0.42, size=17.25, side="SELL"
        )

    async def test_signer_eoa_is_resolved_through_the_proxy_lookup(self):
        clob = self._clob(post_response={"orderID": "x"})

        with _PlacementPatches(clob, proxy="0xproxy") as patches:
            await market_maker_service._place_maker_order("0xpk", "tok", "BUY", 0.5, 10.0)

        # The signer is built from the private key, and its address is what
        # gets resolved to a Polymarket proxy wallet.
        patches.signer.assert_called_once_with("0xpk", 137)
        patches.proxy_lookup.assert_called_once_with("0xeoa")

    async def test_resolved_proxy_is_used_as_funder(self):
        clob = self._clob(post_response={"orderID": "x"})

        with (
            _PlacementPatches(clob, proxy="0xproxy"),
            patch("py_clob_client.client.ClobClient", return_value=clob) as clob_cls,
        ):
            await market_maker_service._place_maker_order("0xpk", "tok", "BUY", 0.5, 10.0)

        self.assertEqual(
            clob_cls.call_args.kwargs,
            {
                "host": CLOB_API,
                "chain_id": 137,
                "key": "0xpk",
                "signature_type": 1,
                "funder": "0xproxy",
            },
        )

    async def test_missing_proxy_falls_back_to_no_funder(self):
        clob = self._clob(post_response={"orderID": "x"})

        with (
            _PlacementPatches(clob, proxy=None),
            patch("py_clob_client.client.ClobClient", return_value=clob) as clob_cls,
        ):
            await market_maker_service._place_maker_order("0xpk", "tok", "BUY", 0.5, 10.0)

        self.assertIsNone(clob_cls.call_args.kwargs["funder"])

    async def test_dust_buy_is_skipped_before_signing(self):
        clob = self._clob()

        with _PlacementPatches(clob) as patches:
            result = await market_maker_service._place_maker_order("0xpk", "tok", "BUY", 0.5, 0.001)

        self.assertIsNone(result)
        patches.order_args.assert_not_called()
        clob.create_order.assert_not_called()
        clob.post_order.assert_not_called()

    async def test_zero_price_buy_is_skipped(self):
        clob = self._clob()

        with _PlacementPatches(clob) as patches:
            result = await market_maker_service._place_maker_order("0xpk", "tok", "BUY", 0.0, 10.0)

        self.assertIsNone(result)
        patches.order_args.assert_not_called()

    async def test_non_dict_response_yields_no_order_id(self):
        clob = self._clob(post_response="OK")

        with _PlacementPatches(clob):
            result = await market_maker_service._place_maker_order("0xpk", "tok", "BUY", 0.5, 10.0)

        self.assertIsNone(result)

    async def test_placement_failure_returns_none(self):
        clob = self._clob(create_error=RuntimeError("not enough allowance"))

        with _PlacementPatches(clob):
            result = await market_maker_service._place_maker_order("0xpk", "tok", "BUY", 0.5, 10.0)

        self.assertIsNone(result)


# ─────────────── sync cycle ───────────────


class SyncMarketMakerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fetch_midpoint = AsyncMock(return_value=0.5)
        self.cancel_all = AsyncMock(return_value=0)
        self.place_order = AsyncMock(return_value="order-1")
        for name, mock in (
            ("_fetch_midpoint", self.fetch_midpoint),
            ("_cancel_all_orders", self.cancel_all),
            ("_place_maker_order", self.place_order),
            ("load_wallet_credentials", MagicMock(return_value={"private_key": "0xpk"})),
        ):
            patcher = patch.object(market_maker_service, name, new=mock)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _run(db):
        return patch.object(market_maker_service, "SessionLocal", return_value=db)

    async def test_missing_config_is_skipped(self):
        db = _session([None])

        with self._run(db):
            result = await market_maker_service._sync_market_maker(404)

        self.assertEqual(result, {"skipped": True, "reason": "disabled or not found"})
        db.close.assert_called_once_with()
        self.fetch_midpoint.assert_not_awaited()

    async def test_disabled_config_is_skipped(self):
        config = _config(enabled=False)
        db = _session([config])

        with self._run(db):
            result = await market_maker_service._sync_market_maker(1)

        self.assertEqual(result, {"skipped": True, "reason": "disabled or not found"})
        self.assertEqual(config.status, "idle")

    async def test_missing_user_is_skipped(self):
        config = _config()
        db = _session([config, None])

        with self._run(db):
            result = await market_maker_service._sync_market_maker(1)

        self.assertEqual(result, {"skipped": True, "reason": "user not found"})
        self.assertEqual(config.status, "idle")
        self.fetch_midpoint.assert_not_awaited()

    async def test_credential_failure_marks_config_error(self):
        config = _config()
        db = _session([config, _user()])

        with (
            self._run(db),
            patch.object(
                market_maker_service,
                "load_wallet_credentials",
                side_effect=RuntimeError("keystore down"),
            ),
        ):
            result = await market_maker_service._sync_market_maker(1)

        self.assertEqual(result, {"error": "keystore down"})
        self.assertEqual(config.status, "error")
        self.assertEqual(config.last_error, "Credential error: keystore down")
        db.commit.assert_called_once_with()
        self.fetch_midpoint.assert_not_awaited()

    async def test_missing_midpoint_is_reported(self):
        config = _config()
        db = _session([config, _user()])
        self.fetch_midpoint.return_value = None

        with self._run(db):
            result = await market_maker_service._sync_market_maker(1)

        self.assertEqual(result, {"error": "no midpoint"})
        self.assertEqual(config.last_error, "Could not fetch midpoint")
        self.cancel_all.assert_not_awaited()
        db.close.assert_called_once_with()

    async def test_bands_cycle_updates_counters_and_status(self):
        config = _config(num_bands=1, min_spread=0.02, max_spread=0.10)
        db = _session([config, _user()])
        self.cancel_all.return_value = 2

        with self._run(db):
            result = await market_maker_service._sync_market_maker(1)

        self.assertEqual(
            result,
            {"midpoint": 0.5, "expected_orders": 2, "cancelled": 2, "placed": 2},
        )
        self.cancel_all.assert_awaited_once_with("0xpk", "token-yes")
        self.assertEqual(
            [call.args for call in self.place_order.await_args_list],
            [
                ("0xpk", "token-yes", "BUY", 0.48, 10.0),
                ("0xpk", "token-yes", "SELL", 0.52, 10.0),
            ],
        )
        self.assertEqual(config.status, "running")
        self.assertIsNone(config.last_error)
        self.assertIsNotNone(config.last_sync_at)
        self.assertEqual(config.total_orders_cancelled, 2)
        self.assertEqual(config.total_orders_placed, 2)
        self.assertEqual(config.current_open_orders, 2)
        self.assertEqual(config.total_volume_usdc, 20.0)

    async def test_amm_strategy_uses_amm_calculator(self):
        config = _config(strategy="amm", num_bands=1)
        db = _session([config, _user()])
        amm_orders = [
            {"side": "SELL", "price": 0.6, "size": 10.0, "token_id": "token-yes", "band": 0}
        ]

        with (
            self._run(db),
            patch.object(
                market_maker_service, "_compute_amm_orders", return_value=amm_orders
            ) as amm,
            patch.object(market_maker_service, "_compute_bands_orders") as bands,
        ):
            result = await market_maker_service._sync_market_maker(1)

        amm.assert_called_once_with(0.5, config)
        bands.assert_not_called()
        self.assertEqual(result["expected_orders"], 1)
        self.assertEqual(config.total_volume_usdc, 10.0)

    async def test_unknown_strategy_falls_back_to_bands(self):
        config = _config(strategy="v2-experimental", num_bands=1)
        db = _session([config, _user()])

        with (
            self._run(db),
            patch.object(market_maker_service, "_compute_bands_orders", return_value=[]) as bands,
            patch.object(market_maker_service, "_compute_amm_orders") as amm,
        ):
            result = await market_maker_service._sync_market_maker(1)

        bands.assert_called_once_with(0.5, config)
        amm.assert_not_called()
        self.assertEqual(result["expected_orders"], 0)
        self.assertEqual(result["placed"], 0)
        self.assertEqual(config.status, "running")

    async def test_rejected_orders_are_not_counted_as_volume(self):
        config = _config(num_bands=1)
        db = _session([config, _user()])
        self.place_order.side_effect = ["order-1", None]

        with self._run(db):
            result = await market_maker_service._sync_market_maker(1)

        self.assertEqual(result["expected_orders"], 2)
        self.assertEqual(result["placed"], 1)
        self.assertEqual(config.total_orders_placed, 1)
        self.assertEqual(config.total_volume_usdc, 10.0)

    async def test_unexpected_error_marks_config_error(self):
        config = _config()
        db = _session([config, _user(), _no_paper_settings(), config])
        self.fetch_midpoint.side_effect = RuntimeError("midpoint boom")

        with self._run(db):
            result = await market_maker_service._sync_market_maker(1)

        self.assertEqual(result, {"error": "midpoint boom"})
        self.assertEqual(config.status, "error")
        self.assertEqual(config.last_error, "midpoint boom")
        db.close.assert_called_once_with()

    async def test_stored_error_is_truncated_to_500_chars(self):
        config = _config()
        db = _session([config, _user(), _no_paper_settings(), config])
        self.fetch_midpoint.side_effect = RuntimeError("e" * 600)

        with self._run(db):
            result = await market_maker_service._sync_market_maker(1)

        self.assertEqual(len(result["error"]), 600)
        self.assertEqual(len(config.last_error), 500)
        self.assertEqual(config.last_error, "e" * 500)

    async def test_recovery_without_a_config_row_skips_the_status_write(self):
        config = _config()
        db = _session([config, _user(), None])
        self.fetch_midpoint.side_effect = RuntimeError("midpoint boom")

        with self._run(db):
            result = await market_maker_service._sync_market_maker(1)

        # The config disappeared mid-cycle, so there is nothing to flag and
        # the session is healthy enough that no rollback is issued.
        self.assertEqual(result, {"error": "midpoint boom"})
        self.assertEqual(config.status, "idle")
        db.rollback.assert_not_called()
        db.close.assert_called_once_with()

    async def test_recovery_failure_rolls_back(self):
        config = _config()
        db = _session([config, _user(), _no_paper_settings(), RuntimeError("db gone")])
        self.fetch_midpoint.side_effect = RuntimeError("midpoint boom")

        with self._run(db):
            result = await market_maker_service._sync_market_maker(1)

        # The recovery query blew up, so the session is rolled back and the
        # *original* error is still what the caller is told about.
        self.assertEqual(result, {"error": "midpoint boom"})
        db.rollback.assert_called_once_with()
        # The status write never landed: the recovery query raised first.
        self.assertEqual(config.status, "idle")
        db.close.assert_called_once_with()


# ─────────────── maker loop ───────────────


class MakerLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_config_stops_loop_and_marks_idle(self):
        config = _config(enabled=False)
        db = _session([config, config, _user()])
        cancel_all = AsyncMock(return_value=4)
        sync = AsyncMock()

        with (
            patch.object(market_maker_service, "SessionLocal", return_value=db),
            patch.object(market_maker_service, "_sync_market_maker", new=sync),
            patch.object(market_maker_service, "_cancel_all_orders", new=cancel_all),
            patch.object(
                market_maker_service,
                "load_wallet_credentials",
                return_value={"private_key": "0xpk"},
            ),
        ):
            await market_maker_service._maker_loop(1)

        sync.assert_not_awaited()
        cancel_all.assert_awaited_once_with("0xpk", "token-yes")
        self.assertEqual(config.status, "idle")
        self.assertEqual(config.current_open_orders, 0)
        db.commit.assert_called_once_with()
        self.assertEqual(db.close.call_count, 2)

    async def test_cleanup_without_a_config_row_changes_nothing(self):
        # The config was deleted while the loop was running: cleanup finds no
        # row, so there is no status to move to "idle".
        db = _session([_config(enabled=False), None])

        with patch.object(market_maker_service, "SessionLocal", return_value=db):
            await market_maker_service._maker_loop(1)

        db.commit.assert_not_called()
        self.assertEqual(db.close.call_count, 2)

    async def test_cleanup_without_user_still_marks_idle(self):
        config = _config(enabled=False)
        db = _session([config, config, None])
        cancel_all = AsyncMock()

        with (
            patch.object(market_maker_service, "SessionLocal", return_value=db),
            patch.object(market_maker_service, "_cancel_all_orders", new=cancel_all),
        ):
            await market_maker_service._maker_loop(1)

        cancel_all.assert_not_awaited()
        self.assertEqual(config.status, "idle")
        db.commit.assert_called_once_with()

    async def test_cleanup_survives_credential_failure(self):
        config = _config(enabled=False)
        db = _session([config, config, _user()])
        cancel_all = AsyncMock()

        with (
            patch.object(market_maker_service, "SessionLocal", return_value=db),
            patch.object(
                market_maker_service,
                "load_wallet_credentials",
                side_effect=RuntimeError("keystore down"),
            ),
            patch.object(market_maker_service, "_cancel_all_orders", new=cancel_all),
        ):
            await market_maker_service._maker_loop(1)

        cancel_all.assert_not_awaited()
        self.assertEqual(config.status, "idle")
        db.commit.assert_called_once_with()

    async def test_missing_config_defaults_to_disabled(self):
        db = _session([None])
        sync = AsyncMock()

        with (
            patch.object(market_maker_service, "SessionLocal", return_value=db),
            patch.object(market_maker_service, "_sync_market_maker", new=sync),
        ):
            await market_maker_service._maker_loop(1)

        sync.assert_not_awaited()
        db.commit.assert_not_called()

    async def test_cancellation_during_sync_breaks_loop(self):
        config = _config(enabled=True, sync_interval_seconds=1)
        db = _session([config, config, None])
        cancel_all = AsyncMock()
        sync = AsyncMock(side_effect=asyncio.CancelledError)

        with (
            patch.object(market_maker_service, "SessionLocal", return_value=db),
            patch.object(market_maker_service, "_sync_market_maker", new=sync),
            patch.object(market_maker_service, "_cancel_all_orders", new=cancel_all),
        ):
            await market_maker_service._maker_loop(1)

        sync.assert_awaited_once_with(1)
        cancel_all.assert_not_awaited()
        self.assertEqual(config.status, "idle")

    async def test_sync_error_falls_back_to_thirty_second_interval(self):
        config = _config(enabled=True, sync_interval_seconds=1)
        db = _session([config])
        delays: list[float] = []
        sync = AsyncMock(side_effect=RuntimeError("sync exploded"))
        logger = MagicMock()

        async def _fake_sleep(seconds):
            delays.append(seconds)
            raise asyncio.CancelledError

        with (
            patch.object(market_maker_service, "SessionLocal", return_value=db),
            patch.object(market_maker_service, "_sync_market_maker", new=sync),
            patch.object(market_maker_service.asyncio, "sleep", new=_fake_sleep),
            patch.object(market_maker_service, "logger", logger),
            self.assertRaises(asyncio.CancelledError),
        ):
            await market_maker_service._maker_loop(1)

        self.assertEqual(delays, [30])
        sync.assert_awaited_once_with(1)
        self.assertEqual(
            logger.error.call_args.args[:2],
            ("Market maker loop error (config=%d): %s", 1),
        )
        self.assertIsInstance(logger.error.call_args.args[2], RuntimeError)
        self.assertEqual(str(logger.error.call_args.args[2]), "sync exploded")

    async def test_successful_sync_uses_configured_interval(self):
        config = _config(enabled=True, sync_interval_seconds=7)
        db = _session([config])
        delays: list[float] = []
        sync = AsyncMock(return_value={"placed": 0})
        logger = MagicMock()

        async def _fake_sleep(seconds):
            delays.append(seconds)
            raise asyncio.CancelledError

        with (
            patch.object(market_maker_service, "SessionLocal", return_value=db),
            patch.object(market_maker_service, "_sync_market_maker", new=sync),
            patch.object(market_maker_service.asyncio, "sleep", new=_fake_sleep),
            patch.object(market_maker_service, "logger", logger),
            self.assertRaises(asyncio.CancelledError),
        ):
            await market_maker_service._maker_loop(1)

        self.assertEqual(delays, [7])
        logger.info.assert_any_call("Market maker sync (config=%d): %s", 1, {"placed": 0})
        logger.error.assert_not_called()

    async def test_cleanup_failure_is_swallowed(self):
        db = _session([_config(enabled=False)])
        factory = MagicMock(side_effect=[db, RuntimeError("cleanup exploded")])
        logger = MagicMock()

        with (
            patch.object(market_maker_service, "SessionLocal", factory),
            patch.object(market_maker_service, "logger", logger),
        ):
            self.assertIsNone(await market_maker_service._maker_loop(1))

        self.assertEqual(
            logger.warning.call_args.args,
            ("Market maker loop cleanup failed for config_id=%d", 1),
        )
        self.assertIs(logger.warning.call_args.kwargs["exc_info"], True)


# ─────────────── public start/stop API ───────────────


async def _pending_task() -> asyncio.Task:
    task = asyncio.create_task(asyncio.Event().wait())
    await asyncio.sleep(0)
    self_done = task.done()
    assert not self_done, "pending task should not be finished"
    return task


async def _finished_task() -> asyncio.Task:
    async def _noop():
        return None

    task = asyncio.create_task(_noop())
    await asyncio.sleep(0)
    return task


class MarketMakerRegistryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._restore = dict(market_maker_service._running_makers)
        market_maker_service._running_makers.clear()
        self.addCleanup(market_maker_service._running_makers.update, self._restore)

    async def test_start_registers_a_maker_task(self):
        loop = AsyncMock()

        with patch.object(market_maker_service, "_maker_loop", new=loop):
            started = await market_maker_service.start_market_maker(42)
            self.assertIs(started, True)
            task = market_maker_service._running_makers[42]
            await task

        loop.assert_awaited_once_with(42)
        # The one-shot loop finished, so it is no longer reported as running.
        self.assertEqual(market_maker_service.get_running_maker_ids(), [])

    async def test_start_refuses_while_a_maker_is_running(self):
        task = await _pending_task()
        market_maker_service._running_makers[42] = task
        self.addCleanup(task.cancel)

        with patch.object(market_maker_service, "_maker_loop", new=AsyncMock()) as loop:
            started = await market_maker_service.start_market_maker(42)

        self.assertIs(started, False)
        self.assertIs(market_maker_service._running_makers[42], task)
        loop.assert_not_awaited()

    async def test_start_replaces_a_finished_maker(self):
        finished = await _finished_task()
        market_maker_service._running_makers[42] = finished
        loop = AsyncMock()

        with patch.object(market_maker_service, "_maker_loop", new=loop):
            started = await market_maker_service.start_market_maker(42)
            self.assertIs(started, True)
            task = market_maker_service._running_makers[42]
            await task

        self.assertIsNot(task, finished)
        loop.assert_awaited_once_with(42)

    async def test_stop_cancels_and_forgets_a_running_maker(self):
        task = await _pending_task()
        market_maker_service._running_makers[42] = task

        self.assertIs(await market_maker_service.stop_market_maker(42), True)
        self.assertTrue(task.cancelled())
        self.assertNotIn(42, market_maker_service._running_makers)

    async def test_stop_unknown_maker_returns_false(self):
        self.assertIs(await market_maker_service.stop_market_maker(999), False)
        self.assertEqual(market_maker_service._running_makers, {})

    async def test_stop_of_already_finished_maker_returns_false(self):
        finished = await _finished_task()
        market_maker_service._running_makers[42] = finished

        self.assertIs(await market_maker_service.stop_market_maker(42), False)
        # The stale entry is still evicted even though nothing was cancelled.
        self.assertNotIn(42, market_maker_service._running_makers)

    async def test_stop_all_clears_the_registry(self):
        tasks = [await _pending_task(), await _pending_task()]
        market_maker_service._running_makers[1] = tasks[0]
        market_maker_service._running_makers[2] = tasks[1]
        market_maker_service._running_makers[3] = await _finished_task()
        logger = MagicMock()

        with patch.object(market_maker_service, "logger", logger):
            await market_maker_service.stop_all_market_makers()

        self.assertEqual(market_maker_service._running_makers, {})
        self.assertTrue(all(task.cancelled() for task in tasks))
        logger.info.assert_called_once_with("All market makers stopped")

    async def test_running_ids_exclude_finished_tasks(self):
        pending = await _pending_task()
        market_maker_service._running_makers[1] = pending
        market_maker_service._running_makers[2] = await _finished_task()

        self.assertEqual(market_maker_service.get_running_maker_ids(), [1])

        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        self.assertEqual(market_maker_service.get_running_maker_ids(), [])

    async def test_start_all_enabled_makers_starts_each_config(self):
        db = _session(all_results=[SimpleNamespace(id=1), SimpleNamespace(id=2)])
        start = AsyncMock(side_effect=[True, False])

        with (
            patch.object(market_maker_service, "SessionLocal", return_value=db),
            patch.object(market_maker_service, "start_market_maker", new=start),
        ):
            await market_maker_service.start_all_enabled_market_makers()

        self.assertEqual([call.args for call in start.await_args_list], [(1,), (2,)])
        self.assertEqual(db.query.call_count, 1)
        db.close.assert_called_once_with()

    async def test_start_all_closes_session_when_no_configs(self):
        db = _session(all_results=[])
        start = AsyncMock()

        with (
            patch.object(market_maker_service, "SessionLocal", return_value=db),
            patch.object(market_maker_service, "start_market_maker", new=start),
        ):
            await market_maker_service.start_all_enabled_market_makers()

        start.assert_not_awaited()
        db.close.assert_called_once_with()

    async def test_trigger_single_sync_delegates_to_sync(self):
        sync = AsyncMock(return_value={"midpoint": 0.5, "placed": 1})

        with patch.object(market_maker_service, "_sync_market_maker", new=sync):
            result = await market_maker_service.trigger_single_sync(7)

        self.assertEqual(result, {"midpoint": 0.5, "placed": 1})
        sync.assert_awaited_once_with(7)


if __name__ == "__main__":
    unittest.main()
