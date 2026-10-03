"""
SELL price-floor tests for ``copy_trade_service._place_order_on_polymarket``.

SELL orders are FOK marketable limit orders: py-clob-client only
walks the order book when ``MarketOrderArgs.price <= 0``, so a
positive price is signed directly as the limit price.  Without a
floor a copy/stop-loss sell could fill arbitrarily far below the
reference price; with the floor it is rejected (fail-closed) when
no bid exists at or above it.

The exchange client is not importable in every environment, so
every ``py_clob_client`` symbol the function imports lazily is
replaced with a double installed on the *source* module.
"""

import contextlib
import unittest
from unittest.mock import MagicMock, patch

import app.services.copy_trade_service as copy_trade_service


@contextlib.contextmanager
def _patched_exchange(client: MagicMock, proxy: str | None = "0xproxy"):
    """Install doubles for every lazy ``py_clob_client`` import.

    Yields the argument-capturing doubles for the two order-builder
    types and the order-type enum so tests can assert on how the
    order was constructed and posted.
    """
    limit_args = MagicMock(name="OrderArgs")
    market_args = MagicMock(name="MarketOrderArgs")
    order_type = MagicMock(name="OrderType")
    signer = MagicMock(name="Signer")
    signer.return_value.address.return_value = "0xeoa"
    proxy_lookup = MagicMock(name="proxy_wallet_lookup")
    proxy_lookup.return_value = proxy
    with (
        patch("py_clob_client.client.ClobClient", return_value=client),
        patch(
            "py_clob_client.clob_types.ApiCreds",
            MagicMock(name="ApiCreds"),
        ),
        patch(
            "py_clob_client.clob_types.AssetType",
            MagicMock(name="AssetType"),
        ),
        patch(
            "py_clob_client.clob_types.BalanceAllowanceParams",
            MagicMock(name="BalanceAllowanceParams"),
        ),
        patch("py_clob_client.clob_types.MarketOrderArgs", market_args),
        patch("py_clob_client.clob_types.OrderArgs", limit_args),
        patch("py_clob_client.clob_types.OrderType", order_type),
        patch("py_clob_client.order_builder.constants.BUY", "BUY"),
        patch("py_clob_client.order_builder.constants.SELL", "SELL"),
        patch("py_clob_client.signer.Signer", signer),
        patch(
            "app.services.copy_trade_service._get_poly_proxy_wallet_address",
            proxy_lookup,
        ),
    ):
        yield market_args, limit_args, order_type, signer, proxy_lookup


def _fake_exchange(post_response=None) -> MagicMock:
    """A ClobClient double whose creds/tick-size calls succeed."""
    client = MagicMock(name="ClobClient")
    client.create_or_derive_api_creds.return_value = {"apiKey": "derived"}
    client.create_market_order.return_value = "signed"
    client.create_order.return_value = "signed"
    client.get_tick_size.return_value = "0.01"
    if post_response is not None:
        client.post_order.return_value = post_response
    return client


class SellFloorTests(unittest.TestCase):
    """The SELL branch signs a tick-aligned floor, never a raw price."""

    def test_floor_is_reference_price_less_the_guard(self):
        client = _fake_exchange(post_response={"orderID": "0xorder"})

        with _patched_exchange(client) as (market_args, _limit, order_type, *_rest):
            result = copy_trade_service._place_order_on_polymarket(
                "0xpk", None, "token-yes", "SELL", 0.50, 100.0
            )

        self.assertTrue(result["success"])
        # 0.50 × 0.95 = 0.475, rounded down to the 0.01 tick → 0.47.
        market_args.assert_called_once_with(
            token_id="token-yes", amount=100.0, side="SELL", price=0.47
        )
        self.assertEqual(client.post_order.call_args.args, ("signed", order_type.FOK))
        client.get_tick_size.assert_called_once_with("token-yes")

    def test_floor_rounds_down_to_a_coarse_tick(self):
        client = _fake_exchange(post_response={"orderID": "0xorder"})
        client.get_tick_size.return_value = "0.1"

        with _patched_exchange(client) as (market_args, *_rest):
            copy_trade_service._place_order_on_polymarket(
                "0xpk", None, "token-yes", "SELL", 0.30, 50.0
            )

        # 0.30 × 0.95 = 0.285 → 0.2 on a 0.1 tick.
        market_args.assert_called_once_with(
            token_id="token-yes", amount=50.0, side="SELL", price=0.2
        )

    def test_zero_price_sell_is_unrestricted_minimum_tick(self):
        client = _fake_exchange(post_response={"orderID": "0xorder"})

        with _patched_exchange(client) as (market_args, *_rest):
            copy_trade_service._place_order_on_polymarket(
                "0xpk", None, "token-yes", "SELL", 0.0, 50.0
            )

        # Emergency stop (trades.py passes 0.0): no slippage guard,
        # only the exchange's minimum tick.
        market_args.assert_called_once_with(
            token_id="token-yes", amount=50.0, side="SELL", price=0.01
        )

    def test_floor_never_drops_below_the_tick(self):
        client = _fake_exchange(post_response={"orderID": "0xorder"})
        client.get_tick_size.return_value = "0.1"

        with _patched_exchange(client) as (market_args, *_rest):
            copy_trade_service._place_order_on_polymarket(
                "0xpk", None, "token-yes", "SELL", 0.10, 10.0
            )

        # 0.10 × 0.95 = 0.095 → 0 ticks → clamped up to the tick.
        market_args.assert_called_once_with(
            token_id="token-yes", amount=10.0, side="SELL", price=0.1
        )

    def test_floor_never_exceeds_one_minus_the_tick(self):
        client = _fake_exchange(post_response={"orderID": "0xorder"})
        client.get_tick_size.return_value = "0.3"

        with _patched_exchange(client) as (market_args, *_rest):
            copy_trade_service._place_order_on_polymarket(
                "0xpk", None, "token-yes", "SELL", 0.99, 10.0
            )

        # 0.99 × 0.95 = 0.9405 → 0.9 on a 0.3 tick, clamped to 0.7.
        market_args.assert_called_once_with(
            token_id="token-yes", amount=10.0, side="SELL", price=0.7
        )

    def test_buy_path_is_unchanged(self):
        client = _fake_exchange(post_response={"orderID": "0xorder"})

        with _patched_exchange(client) as (
            _market,
            limit_args,
            order_type,
            *_rest,
        ):
            result = copy_trade_service._place_order_on_polymarket(
                "0xpk", None, "token-yes", "BUY", 0.5, 10.0
            )

        self.assertTrue(result["success"])
        # BUY size is a USDC notional, converted to shares at the
        # order price and posted as a GTC limit order.
        limit_args.assert_called_once_with(token_id="token-yes", price=0.5, size=20.0, side="BUY")
        self.assertEqual(client.post_order.call_args.args, ("signed", order_type.GTC))
        client.create_order.assert_called_once_with(limit_args.return_value)


if __name__ == "__main__":
    unittest.main()
