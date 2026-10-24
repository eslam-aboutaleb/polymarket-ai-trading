"""Manual API behavior against the real durable gateway and a real test DB."""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_live_gateway import gateway as gateway

from app.api.routes.auth.auth import get_current_user_from_token
from app.api.routes.trading import trades
from app.models.trading.live_order import LiveOrder
from app.models.trading.trading_control import TradingControlState
from app.models.trading.user_trade import UserTrade
from app.utils.database import get_db


def client_for(db):
    app = FastAPI()
    app.include_router(trades.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user_from_token] = lambda: {
        "user_id": 1,
        "wallet_address": "wallet",
    }
    return TestClient(app)


def payload(**changes):
    return {
        "execution_mode": "live",
        "token_id": "123",
        "side": "BUY",
        "price": 0.5,
        "size": 5,
        **changes,
    }


def test_manual_order_uses_notional_buys_and_records_pending_before_exchange(gateway):
    db, _, _, submit = gateway
    response = client_for(db).post(
        "/api/trades/execute", json=payload(), headers={"Idempotency-Key": "manual"}
    )
    assert response.status_code == 200 and response.json()["success"]
    row = db.scalar(select(LiveOrder))
    assert row.shares == 10 and row.notional == 5
    assert db.get(UserTrade, row.trade_id).status == "pending"
    assert db.get(UserTrade, row.trade_id).executed_at is None
    submit.assert_called_once()


def test_manual_duplicate_request_replays_real_order(gateway):
    db, _, _, submit = gateway
    client = client_for(db)
    headers = {"Idempotency-Key": "manual"}
    first = client.post("/api/trades/execute", json=payload(), headers=headers)
    again = client.post("/api/trades/execute", json=payload(), headers=headers)
    assert again.json() == first.json()
    submit.assert_called_once()


def test_manual_pending_orders_count_against_shared_budget(gateway):
    db, _, _, submit = gateway
    client = client_for(db)
    assert client.post(
        "/api/trades/execute", json=payload(size=40), headers={"Idempotency-Key": "one"}
    ).json()["success"]
    response = client.post(
        "/api/trades/execute", json=payload(size=20), headers={"Idempotency-Key": "two"}
    )
    assert response.status_code == 503 and "DAILY_BUDGET_EXCEEDED" in response.text
    submit.assert_called_once()


def test_manual_order_requires_economic_identity(gateway):
    db, _, _, submit = gateway
    response = client_for(db).post("/api/trades/execute", json=payload())
    assert response.status_code == 422
    submit.assert_not_called()


def test_manual_order_respects_durable_halt(gateway):
    db, _, _, submit = gateway
    db.get(TradingControlState, "user:1").state = "HALTED"
    db.commit()
    response = client_for(db).post(
        "/api/trades/execute", json=payload(), headers={"Idempotency-Key": "halted"}
    )
    assert response.status_code == 503
    submit.assert_not_called()


def test_cash_out_preserves_share_units_and_forces_sell(gateway):
    db, _, _, submit = gateway
    response = client_for(db).post(
        "/api/trades/cash-out", json=payload(size=8), headers={"Idempotency-Key": "sell"}
    )
    assert response.status_code == 200 and response.json()["side"] == "SELL"
    assert db.scalar(select(LiveOrder)).shares == 8
    assert db.scalar(select(LiveOrder)).side == "SELL"
    assert submit.call_args.args[2] == "FOK"


def test_lost_exchange_ack_is_exposed_as_unknown_without_fabricated_fill(gateway):
    db, _, _, submit = gateway
    submit.side_effect = TimeoutError()
    response = client_for(db).post(
        "/api/trades/execute", json=payload(), headers={"Idempotency-Key": "lost"}
    )
    assert response.json()["status"] == "unknown" and not response.json()["success"]
    assert db.scalar(select(LiveOrder)).reserved == 5
