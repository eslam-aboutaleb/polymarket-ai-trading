"""Copy-service retries run through durable storage rather than an ACK cache."""

import asyncio
from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from test_live_gateway import gateway as gateway

from app.models.trading.live_order import LiveOrder
from app.models.trading.user_trade import UserTrade
from app.services.trading import copy_trade_service
from app.services.trading.copy_quote import copy_limit
from app.services.trading.pre_trade_gate import _get_idempotency_cache
from app.utils.time import utc_now


@pytest.fixture
def copy(gateway, monkeypatch):
    db, _, _, submit = gateway
    monkeypatch.setattr(
        copy_trade_service, "load_wallet_credentials", lambda _: {"private_key": "test"}
    )
    monkeypatch.setattr(
        copy_trade_service,
        "_plan_copy_trade_size",
        AsyncMock(
            return_value={
                "trade_size_pre_safety": 5,
                "rejection": None,
            }
        ),
    )

    async def fresh_quote(condition, token, side, price, slippage, size):
        # Copy orders now require fresh executable CLOB evidence (FOK, tick, neg_risk).
        evidence = {"condition": condition, "token": token, "tick_size": ".01", "neg_risk": False}
        limit = copy_limit(side, price, slippage, Decimal(".01"))
        return limit, Decimal(10), utc_now() + timedelta(seconds=5), evidence

    monkeypatch.setattr(copy_trade_service, "fresh_copy_quote", fresh_quote)

    def execute(key):
        return asyncio.run(
            copy_trade_service.execute_copy_trade(
                db,
                1,
                "0x" + "2" * 40,
                "market",
                "123",
                "BUY",
                0.5,
                10,
                idempotency_key=key,
                source_timestamp=utc_now(),
            )
        )

    return db, submit, execute


def test_same_copy_source_never_submits_twice(copy):
    db, submit, execute = copy
    first = execute("source:one")
    assert first["executed"]
    assert execute("source:one")["trade_id"] == first["trade_id"]
    assert len(db.scalars(select(UserTrade)).all()) == 1
    submit.assert_called_once()


def test_copy_identity_survives_cache_loss(copy):
    _, submit, execute = copy
    first = execute("source:one")
    _get_idempotency_cache().clear()
    assert execute("source:one")["order_hash"] == first["order_hash"]
    submit.assert_called_once()


def test_different_copy_sources_have_different_economic_orders(copy):
    db, submit, execute = copy
    first, second = execute("source:one"), execute("source:two")
    assert first["executed"] and second["executed"]
    assert first["order_hash"] != second["order_hash"]
    assert submit.call_count == 2
    assert len(db.scalars(select(LiveOrder)).all()) == 2


def test_ambiguous_copy_ack_is_replayed_without_new_submission(copy):
    db, submit, execute = copy
    submit.side_effect = TimeoutError()
    first = execute("source:one")
    assert not first["executed"] and first["status"] == "unknown"
    assert execute("source:one")["order_hash"] == first["order_hash"]
    assert db.scalar(select(LiveOrder)).reserved == Decimal("5.2")  # 10 shares at the .52 limit
    submit.assert_called_once()
