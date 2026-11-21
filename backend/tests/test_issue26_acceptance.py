"""Acceptance regressions for #26 beyond the initial recovery increment."""

import json
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_live_gateway import gateway as gateway
from test_live_gateway import place
from test_live_gateway_recovery import observe

from app.models.platform.app_event import AppEvent
from app.models.trading.live_order import LiveOrder
from app.models.trading.user_trade import UserTrade
from app.services.trading.execution import clob_transport, live
from app.services.trading.live_trading_policy import LiveTradingBlocked
from app.utils.time import utc_now


def test_risk_approval_is_committed_with_signed_intent_before_send(gateway):
    db, _, _, submit = gateway
    original = submit.side_effect
    captured = {}

    def inspect_approval(*args):
        with Session(db.bind) as observer:
            event = observer.scalar(
                select(AppEvent).where(AppEvent.event_type == "order_intent_created")
            )
            captured["payload"] = json.loads(event.payload_json)
            captured["order_hash"] = observer.scalar(select(LiveOrder)).order_hash
            captured["aggregate_id"] = event.aggregate_id
        return original(*args)

    submit.side_effect = inspect_approval
    assert place(db)["success"]
    submit.assert_called_once()
    payload = captured["payload"]
    approval = payload["risk_approval"]
    assert approval["policy_version"] == "conservative-principal-v1"
    assert Decimal(approval["wallet"]["requested_principal"]) == 5
    assert Decimal(approval["authenticated_available"]) == 1000
    assert approval["balance_received_at"] and approval["wallet"]["approved_at"]
    assert {item["name"] for item in approval["wallet"]["budgets"]} >= {"position", "daily"}
    assert payload["expiration_policy"] == "until_cancelled"
    assert captured["aggregate_id"] == captured["order_hash"]


@pytest.mark.parametrize("worker_state,quarantined", [("CANCELLED", False), ("SUBMITTING", True)])
def test_sender_reloads_intent_after_commit_before_any_remote_send(
    gateway, monkeypatch, worker_state, quarantined
):
    db, _, _, submit = gateway
    original = db.commit
    commits = [0]

    def worker_between_transactions():
        original()
        commits[0] += 1
        if commits[0] == 1:
            with Session(db.bind) as worker:
                row = worker.scalar(select(LiveOrder))
                row.state = worker_state
                row.response = {"reconciliation_required": quarantined}
                worker.commit()

    monkeypatch.setattr(db, "commit", worker_between_transactions)
    result = place(db)
    assert result["status"] == worker_state.lower()
    assert result["reconciliation_required"] is quarantined
    submit.assert_not_called()


def test_periodic_worker_detects_late_fill_after_confirmed_partial_cancel(gateway, monkeypatch):
    db, client, _, _ = gateway
    result = place(db)
    current = [Decimal(4)]

    def fills(*_):
        quantity = current[0]
        return {
            "shares": quantity,
            "price": Decimal(".5"),
            "notional": quantity / 2,
            "filled_at": utc_now(),
            "fills": [
                {"trade_id": "first", "transaction_hash": "tx1", "shares": "4", "notional": "2"},
                *(
                    [
                        {
                            "trade_id": "late",
                            "transaction_hash": "tx2",
                            "shares": "2",
                            "notional": "1",
                        }
                    ]
                    if quantity == 6
                    else []
                ),
            ],
        }

    monkeypatch.setattr(clob_transport, "settled_fills", fills)
    observe(client, result, matched="4", status="CANCELED")
    live.reconcile_pending_live_orders(db)
    assert db.get(UserTrade, result["trade_id"]).status == "executed"
    current[0] = Decimal(6)
    observe(client, result, matched="6", status="CANCELED")
    live.reconcile_pending_live_orders(db)
    row = db.scalar(select(LiveOrder))
    assert row.reserved == 3 and row.response["size_matched"] == "6"
    assert db.get(UserTrade, result["trade_id"]).filled_size == 6


@pytest.mark.parametrize("status", [429, 500])
def test_http_failure_after_send_never_retries_or_releases(gateway, status):
    db, _, _, submit = gateway
    submit.side_effect = httpx.HTTPStatusError(
        "unknown venue outcome",
        request=httpx.Request("POST", "https://exchange.invalid"),
        response=httpx.Response(status),
    )
    result = place(db)
    with Session(db.bind) as restarted:
        assert place(restarted)["order_hash"] == result["order_hash"]
        assert restarted.scalar(select(LiveOrder)).reserved == 5
    submit.assert_called_once()


def test_expired_credentials_cannot_release_or_clear_recovery(gateway, monkeypatch):
    db, _, _, submit = gateway
    submit.side_effect = TimeoutError()
    result = place(db)
    monkeypatch.setattr(live, "load_wallet_credentials", lambda _: None)
    with pytest.raises(LiveTradingBlocked, match="CREDENTIALS_REQUIRED"):
        live.reconcile_order(db, user_id=1, order_hash=result["order_hash"])
    assert db.scalar(select(LiveOrder)).reserved == 5
    assert db.scalar(select(LiveOrder)).state == "UNKNOWN"


@pytest.mark.parametrize("revocation", ["follow", "ai_approval"])
def test_copy_authorization_is_rechecked_after_intent_commit(gateway, monkeypatch, revocation):
    from app.models.trading.followed_trader import FollowedTrader
    from app.models.users.user_settings import UserSettings

    db, _, _, submit = gateway
    original = db.commit
    count = [0]

    def revoke():
        original()
        count[0] += 1
        if count[0] == 1:
            with Session(db.bind) as other:
                if revocation == "follow":
                    other.scalar(select(FollowedTrader)).is_active = False
                else:
                    other.scalar(select(UserSettings)).require_ai_approval = True
                other.commit()

    monkeypatch.setattr(db, "commit", revoke)
    result = place(db, strategy="copy", copied_from_wallet="0x" + "2" * 40)
    assert result["status"] == "rejected"
    assert db.scalar(select(LiveOrder)).reserved == 0
    submit.assert_not_called()


def test_authenticated_balance_is_refreshed_after_signing(gateway, monkeypatch):
    from unittest.mock import MagicMock

    db, _, _, submit = gateway
    monkeypatch.setattr(
        clob_transport, "balance", MagicMock(side_effect=[Decimal(100), Decimal(0)])
    )
    result = place(db)
    assert result["status"] == "rejected"
    assert db.scalar(select(LiveOrder)).reserved == 0
    submit.assert_not_called()


def test_oversized_approval_evidence_never_becomes_a_truncated_money_intent(gateway):
    db, _, _, submit = gateway
    with pytest.raises(LiveTradingBlocked, match="AUDIT_PAYLOAD_TOO_LARGE"):
        place(db, market_id="x" * 5000)
    submit.assert_not_called()
    assert db.scalar(select(LiveOrder.id)) is None
    assert db.scalar(select(AppEvent.id)) is None


def test_redis_outage_cannot_bypass_the_durable_circuit_breaker(gateway, monkeypatch):
    from unittest.mock import MagicMock

    from app.models.trading.trading_control import TradingControlState

    db, _, _, submit = gateway
    redis = MagicMock(side_effect=ConnectionError("cache unavailable"))
    monkeypatch.setattr("redis.Redis.from_url", redis)
    db.get(TradingControlState, "user:1").state = "HALTED"
    db.commit()
    with pytest.raises(LiveTradingBlocked, match="HALTED"):
        place(db)
    submit.assert_not_called()
    redis.assert_not_called()  # SQL authority has no fail-open Redis fallback.


@pytest.mark.parametrize("fault", ["deadline", "release_flag"])
def test_final_balance_refresh_cannot_outlive_quote_authority(gateway, monkeypatch, fault):
    from datetime import timedelta

    db, _, _, submit = gateway
    flags = gateway[2]
    clock = [utc_now()]
    calls = [0]
    monkeypatch.setattr(live, "utc_now", lambda: clock[0])

    def balance(*_):
        calls[0] += 1
        if calls[0] == 2:
            if fault == "deadline":
                clock[0] += timedelta(seconds=2)
            else:
                flags.live_trading_enabled = False
        return Decimal(1000)

    monkeypatch.setattr(clob_transport, "balance", balance)
    result = place(db, strategy="latency_arb", execution_deadline=clock[0] + timedelta(seconds=1))
    assert result["status"] == "rejected"
    expected = (
        "EXECUTABLE_QUOTE_EXPIRED_OR_MISSING" if fault == "deadline" else "LIVE_TRADING_DISABLED"
    )
    assert result["error"] == expected
    assert db.scalar(select(LiveOrder)).reserved == 0
    submit.assert_not_called()
