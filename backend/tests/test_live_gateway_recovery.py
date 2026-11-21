"""Regression tests for signed-order identity, recovery and audit durability."""

import json
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_live_gateway import gateway as gateway
from test_live_gateway import place

from app.models.platform.app_event import AppEvent
from app.models.trading.live_order import LiveOrder
from app.models.trading.user_trade import UserTrade
from app.services.trading.execution import clob_transport, live
from app.services.trading.live_trading_policy import LiveTradingBlocked


def observe(client, result, matched="0", original="10", status="LIVE", **extra):
    client.get_order.return_value = {
        "id": result["order_hash"],
        "status": status,
        "original_size": original,
        "size_matched": matched,
        **extra,
    }


@pytest.mark.parametrize("original", ["5", "20", "NaN", "Infinity"])
def test_signed_quantity_mismatch_never_releases_cash(gateway, original):
    db, client, _, _ = gateway
    result = place(db)
    observe(client, result, original=original, matched="4", status="CANCELED")
    with pytest.raises(LiveTradingBlocked):
        live.reconcile_order(db, user_id=1, order_hash=result["order_hash"])
    row = db.scalar(select(LiveOrder))
    assert row.state == "SUBMITTED" and row.reserved == 5
    assert row.response["reconciliation_required"] is True
    with pytest.raises(LiveTradingBlocked, match="UNRESOLVED"):
        place(db, idempotency_key="new-risk")


def test_fill_watermark_survives_restart(gateway):
    db, client, _, _ = gateway
    result = place(db)
    observe(client, result, matched="4")
    live.reconcile_order(db, user_id=1, order_hash=result["order_hash"])
    observe(client, result, matched="2", status="CANCELED")
    with Session(db.bind) as restarted, pytest.raises(LiveTradingBlocked):
        live.reconcile_order(restarted, user_id=1, order_hash=result["order_hash"])
    db.expire_all()
    row = db.scalar(select(LiveOrder))
    assert row.state == "PARTIALLY_FILLED" and row.reserved == 5
    assert row.response["size_matched"] == "4"


@pytest.mark.parametrize("terminal,matched", [("MATCHED", "10"), ("CANCELED", "4")])
def test_terminal_order_cannot_reopen(gateway, terminal, matched):
    db, client, _, _ = gateway
    result = place(db)
    observe(client, result, matched=matched, status=terminal)
    first = live.reconcile_order(db, user_id=1, order_hash=result["order_hash"])
    observe(client, result, matched=matched, status="LIVE")
    with pytest.raises(LiveTradingBlocked):
        live.reconcile_order(db, user_id=1, order_hash=result["order_hash"])
    assert db.scalar(select(LiveOrder)).state.lower() == first["status"]


def test_cancelled_order_accepts_late_fills_conservatively(gateway):
    db, client, _, _ = gateway
    result = place(db)
    for matched, reserved in [("4", 2), ("6", 3)]:
        observe(client, result, matched=matched, status="CANCELED")
        live.reconcile_order(db, user_id=1, order_hash=result["order_hash"])
        row = db.scalar(select(LiveOrder))
        assert row.state == "CANCELLED" and row.reserved == reserved


@pytest.mark.parametrize("extra", [{"asset_id": "foreign"}, {"side": "SELL"}])
def test_wrong_token_or_side_cannot_release_reservation(gateway, extra):
    db, client, _, _ = gateway
    result = place(db)
    observe(client, result, status="CANCELED", **extra)
    with pytest.raises(LiveTradingBlocked):
        live.reconcile_order(db, user_id=1, order_hash=result["order_hash"])
    assert db.scalar(select(LiveOrder)).reserved == 5


def test_contradictory_ack_retains_reservation(gateway):
    db, _, _, submit = gateway

    def contradictory(*_):
        return {
            "success": False,
            "orderID": db.scalar(select(LiveOrder)).order_hash,
            "errorMsg": "ambiguous",
        }

    submit.side_effect = contradictory
    assert place(db)["status"] == "unknown"
    assert db.scalar(select(LiveOrder)).reserved == 5


def test_signer_cannot_lower_sell_price(gateway, monkeypatch):
    db, _, _, submit = gateway
    monkeypatch.setattr(
        clob_transport, "prepare", lambda *a, **k: (object(), "hash", Decimal(10), Decimal(4))
    )
    with pytest.raises(ValueError, match="authorized"):
        place(db, side="SELL")
    submit.assert_not_called()
    assert db.scalar(select(LiveOrder.id)) is None


def test_open_buys_reduce_authenticated_buying_power(gateway, monkeypatch):
    db, _, _, submit = gateway
    monkeypatch.setattr(clob_transport, "balance", lambda *_: Decimal(7))
    place(db)
    with pytest.raises(LiveTradingBlocked, match="BALANCE"):
        place(db, idempotency_key="second")
    submit.assert_called_once()


def test_cancel_intent_commits_before_exchange_side_effect(gateway, monkeypatch):
    db, client, _, _ = gateway
    result = place(db)
    observe(client, result, status="CANCELED")

    def cancel(*_):
        with Session(db.bind) as observer:
            assert observer.scalar(
                select(AppEvent.id).where(AppEvent.event_type == "order_cancel_requested")
            )
        return True

    monkeypatch.setattr(clob_transport, "cancel", cancel)
    live.reconcile_order(db, user_id=1, order_hash=result["order_hash"], cancel=True)


def test_cancel_timeout_can_be_reconciled(gateway, monkeypatch):
    db, client, _, _ = gateway
    result = place(db)
    observe(client, result, status="CANCELED")
    monkeypatch.setattr(clob_transport, "cancel", MagicMock(side_effect=TimeoutError()))
    assert (
        live.reconcile_order(db, user_id=1, order_hash=result["order_hash"], cancel=True)["status"]
        == "cancelled"
    )


def test_transition_audit_and_duplicate_reads(gateway):
    db, client, _, submit = gateway
    result = place(db)
    observe(client, result, matched="4", status="CANCELED")
    first = live.reconcile_order(db, user_id=1, order_hash=result["order_hash"])
    events = db.scalars(select(AppEvent).where(AppEvent.event_type == "order_state_changed")).all()
    assert len(events) == 2
    payload = json.loads(events[-1].payload_json)
    assert payload["before"]["state"] == "SUBMITTED"
    assert Decimal(payload["after"]["reserved"]) == 2
    count = len(db.scalars(select(AppEvent)).all())
    with Session(db.bind) as restarted:
        assert live.reconcile_order(restarted, user_id=1, order_hash=result["order_hash"]) == first
        assert len(restarted.scalars(select(AppEvent)).all()) == count
    submit.assert_called_once()


def test_settled_execution_cannot_be_erased(gateway, monkeypatch):
    from app.utils.time import utc_now

    db, client, _, _ = gateway
    result = place(db)
    observe(client, result, matched="10", status="MATCHED", associate_trades=["fill"])
    fills = {
        "shares": Decimal(10),
        "price": Decimal(".48"),
        "notional": Decimal("4.8"),
        "filled_at": utc_now(),
        "fills": [
            {"trade_id": "fill", "transaction_hash": "tx", "shares": "10", "notional": "4.8"}
        ],
    }
    monkeypatch.setattr(clob_transport, "settled_fills", lambda *_: fills)
    live.reconcile_order(db, user_id=1, order_hash=result["order_hash"])
    assert db.get(UserTrade, result["trade_id"]).status == "executed"
    monkeypatch.setattr(clob_transport, "settled_fills", lambda *_: None)
    with pytest.raises(LiveTradingBlocked):
        live.reconcile_order(db, user_id=1, order_hash=result["order_hash"])
    trade = db.get(UserTrade, result["trade_id"])
    assert trade.status == "executed" and trade.filled_size == 10
    assert db.scalar(select(LiveOrder)).response["settled"] is True


def accounts(db):
    from app.models.users.account import Account
    from app.models.users.account_settings import AccountSettings

    rows = [Account(owner_user_id=1, name=f"account-{i}", kind="sub") for i in range(2)]
    db.add_all(rows)
    db.flush()
    for row in rows:
        db.add(
            AccountSettings(
                account_id=row.id,
                simulation_mode=False,
                max_position_size=100,
                daily_loss_limit=50,
                initial_capital=1000,
            )
        )
    db.commit()
    return rows


def test_account_context_is_checked_before_remote_cancellation(gateway, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes.auth.auth import get_current_user_from_token
    from app.api.routes.trading import risk
    from app.utils.database import get_db

    db, client, _, _ = gateway
    first, other = accounts(db)
    result = place(db, account_id=first.id)
    credentials = MagicMock(side_effect=AssertionError("no credential access"))
    monkeypatch.setattr(live, "load_wallet_credentials", credentials)
    app = FastAPI()
    app.include_router(risk.router)
    app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as http:
        for action in ("cancel", "reconcile"):
            response = http.post(
                f"/api/v1/risk/orders/{result['order_hash']}/{action}",
                headers={"X-Account-Id": str(other.id)},
            )
            assert response.status_code == 404
        assert http.get("/api/v1/risk/orders", headers={"X-Account-Id": str(other.id)}).json() == []
        assert (
            len(http.get("/api/v1/risk/orders", headers={"X-Account-Id": str(first.id)}).json())
            == 1
        )
    credentials.assert_not_called()
    client.get_order.assert_not_called()


@pytest.mark.parametrize("path", ["execute", "cash-out"])
def test_manual_http_honors_account_budget_and_rejects_invalid_headers(gateway, path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes.auth.auth import get_current_user_from_token
    from app.api.routes.trading import trades
    from app.models.users.account_settings import AccountSettings
    from app.utils.database import get_db

    db, _, _, submit = gateway
    first, _ = accounts(db)
    db.scalar(
        select(AccountSettings).where(AccountSettings.account_id == first.id)
    ).simulation_mode = True
    db.commit()
    app = FastAPI()
    app.include_router(trades.router)
    app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
    app.dependency_overrides[get_db] = lambda: db
    payload = {"execution_mode": "live", "token_id": "123", "side": "BUY", "price": 0.5, "size": 5}
    with TestClient(app) as http:
        assert (
            http.post(
                f"/api/trades/{path}",
                json=payload,
                headers={"X-Account-Id": "invalid", "Idempotency-Key": "bad"},
            ).status_code
            == 400
        )
        response = http.post(
            f"/api/trades/{path}",
            json=payload,
            headers={"X-Account-Id": str(first.id), "Idempotency-Key": "scoped"},
        )
        assert response.status_code == 503
        assert response.json()["detail"] == "ACCOUNT_NOT_IN_LIVE_MODE"
    submit.assert_not_called()
    assert db.scalar(select(LiveOrder.id)) is None


def test_conflict_quarantine_recovers_without_a_new_submission(gateway):
    db, client, _, submit = gateway
    result = place(db)
    observe(client, result, matched="4", original="20", status="CANCELED")
    with pytest.raises(LiveTradingBlocked):
        live.reconcile_order(db, user_id=1, order_hash=result["order_hash"])
    observe(client, result, matched="4", status="CANCELED")
    live.reconcile_pending_live_orders(db)
    row = db.scalar(select(LiveOrder))
    assert not row.response["reconciliation_required"] and row.reserved == 2
    submit.assert_called_once()


def test_audit_failure_before_cancel_cannot_reach_exchange(gateway, monkeypatch):
    db, _, _, _ = gateway
    result = place(db)
    monkeypatch.setattr("app.services.platform.event_log.append", lambda *a, **kw: None)
    cancel = MagicMock(side_effect=AssertionError("must not cancel"))
    monkeypatch.setattr(clob_transport, "cancel", cancel)
    with pytest.raises(LiveTradingBlocked, match="AUDIT"):
        live.reconcile_order(db, user_id=1, order_hash=result["order_hash"], cancel=True)
    cancel.assert_not_called()


def test_database_failure_after_send_never_retries_the_submission(gateway, monkeypatch):
    db, _, _, submit = gateway
    original = db.commit
    commits = [0]

    def fail_second_commit():
        commits[0] += 1
        if commits[0] == 2:
            raise RuntimeError("database connection lost after exchange ACK")
        return original()

    monkeypatch.setattr(db, "commit", fail_second_commit)
    with pytest.raises(RuntimeError, match="database connection"):
        place(db)
    with Session(db.bind) as restarted:
        result = place(restarted)
        assert result["status"] == "submitting"
        assert restarted.scalar(select(LiveOrder)).reserved == 5
    submit.assert_called_once()


def test_financial_context_is_independent_of_callers_precision(gateway):
    from decimal import localcontext

    db, client, _, _ = gateway
    with localcontext() as context:
        context.prec = 2
        result = place(db, price=0.4321, size=10.12)
        observe(client, result, original="10.12", matched="3.33", status="CANCELED")
        live.reconcile_order(db, user_id=1, order_hash=result["order_hash"])
    row = db.scalar(select(LiveOrder))
    assert row.reserved == Decimal("1.438893")


def test_confirmed_fill_identity_cannot_be_replaced_and_duplicates_are_audited_once(
    gateway, monkeypatch
):
    from app.utils.time import utc_now

    db, client, _, _ = gateway
    result = place(db)
    observe(client, result, matched="10", status="MATCHED", associate_trades=["fill"])
    proof = {
        "shares": Decimal(10),
        "price": Decimal(".48"),
        "notional": Decimal("4.8"),
        "filled_at": utc_now(),
        "fills": [
            {"trade_id": "fill", "transaction_hash": "tx", "shares": "10", "notional": "4.8"}
        ],
    }
    monkeypatch.setattr(clob_transport, "settled_fills", lambda *_: proof)
    for _ in range(2):
        live.reconcile_order(db, user_id=1, order_hash=result["order_hash"])
    assert (
        len(db.scalars(select(AppEvent).where(AppEvent.event_type == "order_fill_confirmed")).all())
        == 1
    )
    proof["fills"] = [
        {"trade_id": "fill", "transaction_hash": "replacement", "shares": "10", "notional": "4.8"}
    ]
    with pytest.raises(LiveTradingBlocked, match="IDENTITIES_CONFLICT"):
        live.reconcile_order(db, user_id=1, order_hash=result["order_hash"])
    assert db.scalar(select(LiveOrder)).response["confirmed_fills"][0]["transaction_hash"] == "tx"


def test_quarantined_wallet_is_not_reported_ready_for_live_execution(gateway):
    from app.api.routes.trading.risk import risk_summary

    db, client, _, _ = gateway
    result = place(db)
    observe(client, result, original="20", matched="4", status="CANCELED")
    with pytest.raises(LiveTradingBlocked):
        live.reconcile_order(db, user_id=1, order_hash=result["order_hash"])
    summary = risk_summary(current_user={"user_id": 1}, db=db)
    assert not summary["live_execution_available"]
    assert summary["release_blocker"] == "RECONCILIATION_REQUIRED"


def test_personal_account_keeps_access_to_legacy_wallet_intents(gateway, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes.auth.auth import get_current_user_from_token
    from app.api.routes.trading import risk
    from app.models.users.account import Account
    from app.utils.database import get_db

    db, client, _, _ = gateway
    result = place(db)  # Existing wallet-level records have no account_id.
    personal = Account(owner_user_id=1, name="Personal", kind="personal", is_default=True)
    db.add(personal)
    db.commit()
    app = FastAPI()
    app.include_router(risk.router)
    app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
    app.dependency_overrides[get_db] = lambda: db
    observe(client, result, status="CANCELED")
    monkeypatch.setattr(clob_transport, "cancel", lambda *_: True)
    with TestClient(app) as http:
        headers = {"X-Account-Id": str(personal.id)}
        assert len(http.get("/api/v1/risk/orders", headers=headers).json()) == 1
        response = http.post(f"/api/v1/risk/orders/{result['order_hash']}/cancel", headers=headers)
        assert response.status_code == 200 and response.json()["status"] == "cancelled"
