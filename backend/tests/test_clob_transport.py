"""Real SDK signing and transport contracts, with remote requests replaced."""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from py_clob_client_v2 import ClobClient

from app.services.trading.execution import clob_transport


@pytest.mark.parametrize("side,expected", [("BUY", "0.43"), ("SELL", "0.44")])
def test_real_v2_signing_respects_tick_and_authorized_price_limit(monkeypatch, side, expected):
    # Published test fixture key. No funded wallet or network is used.
    client = ClobClient(host="https://exchange.invalid", chain_id=137, key="1" * 64)
    monkeypatch.setattr(client, "get_tick_size", lambda _: "0.01")
    monkeypatch.setattr(client, "get_neg_risk", lambda _: False)
    signed, order_hash, shares, notional = clob_transport.prepare(
        client,
        "123",
        side,
        Decimal("0.4321"),
        Decimal("10"),
    )
    assert len(order_hash) == 66 and signed.signature
    assert shares == 10
    assert notional / shares == Decimal(expected)
    assert signed.signatureType == 0


def test_transport_performs_actual_sdk_submission_without_fabricated_response():
    client = MagicMock()
    signed = object()
    response = {"success": True, "orderID": "exchange-order"}
    client.post_order.return_value = response
    assert clob_transport.submit(client, signed, "GTC", True) is response
    client.post_order.assert_called_once_with(signed, "GTC", post_only=True)


def test_cancellation_requires_explicit_exchange_acknowledgement():
    client = MagicMock()
    for response in ({}, {"not_canceled": {"order": "already filled"}}, {"success": True}):
        client.cancel_order.return_value = response
        assert not clob_transport.cancel(client, "order")
    client.cancel_order.return_value = {"canceled": ["order"]}
    assert clob_transport.cancel(client, "order")


def test_settlement_uses_only_owned_confirmed_fills_and_deduplicates_ids():
    client = MagicMock()
    client.get_trades.return_value = [
        {
            "id": "trade",
            "status": "CONFIRMED",
            "transaction_hash": "tx",
            "taker_order_id": "owned",
            "size": "10",
            "price": "0.48",
        }
    ]
    result = clob_transport.settled_fills(client, "owned", ["trade", "trade"])
    assert result == {
        "shares": Decimal(10),
        "notional": Decimal("4.80"),
        "price": Decimal("0.48"),
        "filled_at": None,
    }
    client.get_trades.assert_called_once()
    assert clob_transport.settled_fills(client, "foreign-order", ["trade"]) is None
    client.get_trades.return_value[0]["status"] = "MATCHED"
    assert clob_transport.settled_fills(client, "owned", ["trade"]) is None


def test_latency_plan_signs_only_the_validated_depth_amount(monkeypatch):
    from latency_book_fixtures import candidate

    q = candidate(asks=[{"price": ".501", "size": "200"}, {"price": ".500", "size": "40"}])
    client = ClobClient(host="https://exchange.invalid", chain_id=137, key="1" * 64)
    monkeypatch.setattr(client, "get_tick_size", lambda _: "0.001")
    monkeypatch.setattr(client, "get_neg_risk", lambda _: False)
    signed, _, shares, notional = clob_transport.prepare(
        client,
        q.token_id,
        "BUY",
        q.limit_price,
        q.shares,
        expected_tick=q.tick_size,
        expected_neg_risk=q.neg_risk,
    )
    assert signed.signature and shares <= q.shares
    assert notional <= q.worst_case_cash <= q.authorized_cash
    monkeypatch.setattr(client, "create_order", MagicMock(side_effect=AssertionError("no signing")))
    monkeypatch.setattr(client, "get_tick_size", lambda _: "0.01")
    with pytest.raises(ValueError, match="VENUE_TICK_CHANGED"):
        clob_transport.prepare(
            client, q.token_id, "BUY", q.limit_price, q.shares, expected_tick=q.tick_size
        )
    monkeypatch.setattr(client, "get_tick_size", lambda _: "0.001")
    monkeypatch.setattr(client, "get_neg_risk", lambda _: True)
    with pytest.raises(ValueError, match="VENUE_RISK_RULE_CHANGED"):
        clob_transport.prepare(
            client,
            q.token_id,
            "BUY",
            q.limit_price,
            q.shares,
            expected_tick=q.tick_size,
            expected_neg_risk=False,
        )
    client.create_order.assert_not_called()


def test_expired_latency_quote_cannot_sign_after_sdk_metadata_fetch():
    from datetime import UTC, datetime, timedelta

    client = MagicMock()
    client.get_tick_size.return_value = "0.001"
    client.get_neg_risk.return_value = False
    with pytest.raises(ValueError, match="EXECUTABLE_QUOTE_EXPIRED_OR_MISSING"):
        clob_transport.prepare(
            client,
            "11",
            "BUY",
            Decimal(".5"),
            Decimal(10),
            execution_deadline=datetime.now(UTC) - timedelta(seconds=1),
        )
    client.create_order.assert_not_called()
