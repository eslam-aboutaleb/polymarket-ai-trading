"""Exact financial invariants and real SQL/API/native-read boundary tests (#3)."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.routes.analysis.live_reconciliation import router
from app.api.routes.auth.auth import get_current_user_from_token
from app.config import get_settings
from app.models import Base
from app.models.analytics.live_balance_snapshot import LiveBalanceSnapshot
from app.models.platform.app_event import AppEvent
from app.models.trading.live_order import LiveOrder
from app.models.trading.user_trade import UserTrade
from app.models.users.account import Account
from app.models.users.user import User
from app.security.rate_limit import limiter
from app.services.analytics import live_account_reconciliation as readside
from app.services.analytics.pnl_reconciliation import reconcile_confirmed_cashflows
from app.services.trading.execution import clob_transport
from app.utils.database import get_db

T0 = datetime(2026, 11, 20, tzinfo=UTC)


def observation(cash="100", positions=None):
    return {"cash_usdc": cash, "positions": positions or {}, "coverage": "complete_wallet"}


def fill(identity, side, shares, notional, strategy="manual", fee="0", rebate="0"):
    return {
        "source_id": identity,
        "order_hash": f"order-{identity}",
        "fill_id": str(identity),
        "token_id": "123",
        "side": side,
        "shares": shares,
        "notional": notional,
        "strategy": strategy,
        "fee_usdc": fee,
        "confirmed_rebate_usdc": rebate,
        "matched_at": (T0 + timedelta(seconds=identity)).isoformat(),
    }


def test_gross_to_net_strategy_additivity_and_residuals_are_exact():
    rows = [
        fill(1, "BUY", "10", "4", strategy="copy", fee=".1"),
        fill(2, "SELL", "10", "6", strategy="stop_loss", fee=".2"),
    ]
    result = reconcile_confirmed_cashflows(rows, observation(), observation("101.7"))
    assert result["status"] == "reconciled"
    assert result["gross_realized_pnl"] == 2
    assert result["net_realized_pnl"] == Decimal("1.7")
    assert result["cash_residual_usdc"] == 0
    assert sum(row["net_realized"] for row in result["strategies"].values()) == Decimal("1.7")
    assert result["strategies"]["copy"]["net_realized"] == Decimal("1.7")
    assert result["strategies"]["stop_loss"]["net_realized"] == 0


def test_duplicate_receipts_do_not_double_count_and_replacements_fail():
    row = fill(1, "BUY", "10", "4")
    first = reconcile_confirmed_cashflows([row], observation(), observation("96", {"123": "10"}))
    second = reconcile_confirmed_cashflows(
        [row, {**row, "source_id": 2}], observation(), observation("96", {"123": "10"})
    )
    assert first == second
    with pytest.raises(ValueError, match="CONFLICTING_FILL"):
        reconcile_confirmed_cashflows([row, {**row, "notional": "5"}], observation(), observation())


def test_missing_fees_and_estimated_rebates_never_become_profit():
    rows = [fill(1, "BUY", "10", "4", fee=None), fill(2, "SELL", "10", "6")]
    rows[0]["estimated_rebate_usdc"] = "500"
    report = reconcile_confirmed_cashflows(rows, observation(), observation("102"))
    assert report["net_realized_pnl"] is None
    assert report["total_fees_usdc"] is None
    assert report["cash_residual_usdc"] is None
    assert report["estimated_rebates_usdc"] == 500
    assert report["gross_realized_pnl"] == 2


def test_external_cash_and_tokens_are_residuals_not_attributed_profit():
    report = reconcile_confirmed_cashflows([], observation(), observation("110", {"123": "3"}))
    assert report["gross_realized_pnl"] == 0
    assert report["cash_residual_usdc"] == 10
    assert report["position_residuals"]["123"] == 3
    assert report["status"] == "abstain"
    assert "UNEXPLAINED_CASH_CHANGE" in report["reasons"]


def test_opening_inventory_has_unknown_cost_and_marks_stay_unknown():
    result = reconcile_confirmed_cashflows(
        [fill(1, "SELL", "5", "3")], observation(positions={"123": "5"}), observation("103")
    )
    assert result["gross_realized_pnl"] is None and result["net_realized_pnl"] is None
    assert result["unrealized_pnl"] is None and result["available_cash_usdc"] is None


def test_decimal_context_does_not_change_money_or_tolerances():
    rows = [fill(1, "BUY", "10", "4.123456"), fill(2, "SELL", "10", "6.123456")]
    baseline = reconcile_confirmed_cashflows(rows, observation(), observation("102"))
    with localcontext() as context:
        context.prec = 2
        assert reconcile_confirmed_cashflows(rows, observation(), observation("102")) == baseline


@pytest.fixture
def account_db(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = Session(engine, expire_on_commit=False)
    db.add_all(
        [User(id=1, wallet_address="0x" + "1" * 40), User(id=2, wallet_address="0x" + "2" * 40)]
    )
    db.flush()
    db.add_all(
        [
            Account(id=11, owner_user_id=1, name="Personal", kind="personal", is_default=True),
            Account(id=21, owner_user_id=2, name="Foreign", kind="personal", is_default=True),
        ]
    )
    db.commit()
    monkeypatch.setattr(get_settings(), "live_account_reconciliation_enabled", True)
    monkeypatch.setattr(limiter, "enabled", False)
    monkeypatch.setattr(
        readside,
        "wallet_evidence",
        lambda *args, **kwargs: {
            "positions": {},
            "activity": [],
            "activity_from": None,
            "activity_to": None,
            "receipts": [],
            "chain_finality": "unverified",
        },
    )
    yield db
    db.close()
    engine.dispose()


def snapshot(db, instant, cash, positions, watermark=0, account_id=11):
    evidence = {
        "user_id": 1 if account_id == 11 else 2,
        "account_id": account_id,
        "wallet_identity": "wallet",
        "provider": "authenticated_clob_v2",
        "cash_usdc": cash,
        "positions": positions,
        "coverage": "known_application_tokens",
        "observed_at": instant.isoformat(),
        "event_watermark": watermark,
        "active_or_unsettled_orders": False,
    }
    identity = readside.digest(evidence)
    db.add(
        LiveBalanceSnapshot(
            id=identity,
            user_id=evidence["user_id"],
            account_id=account_id,
            observed_at=instant,
            captured_at=instant,
            evidence_json=readside.canonical(evidence),
        )
    )
    db.commit()
    return identity


def seed_fill(db, *, timestamp=True):
    trade = UserTrade(
        user_id=1,
        account_id=11,
        market_id="condition",
        token_id="123",
        action="buy",
        amount=4,
        price=0.4,
        status="executed",
        strategy_source="manual",
        executed_at=T0 + timedelta(seconds=1),
    )
    db.add(trade)
    db.flush()
    order = LiveOrder(
        user_id=1,
        account_id=11,
        trade_id=trade.id,
        idempotency_key="fill",
        fingerprint="f" * 64,
        strategy="manual",
        token_id="123",
        side="BUY",
        price=0.4,
        shares=10,
        notional=4,
        reserved=4,
        state="FILLED",
        order_hash="order-1",
        response={"settled": True},
    )
    db.add(order)
    db.add(
        AppEvent(
            user_id=1,
            account_id=11,
            aggregate_type="order",
            aggregate_id="order-1",
            source="live_gateway",
            event_type="order_intent_created",
            occurred_at=T0,
            payload_json=json.dumps(
                {
                    "token_id": "123",
                    "side": "BUY",
                    "strategy": "manual",
                    "condition_id": "condition",
                }
            ),
        )
    )
    db.flush()
    payload = {"trade_id": "venue-fill", "transaction_hash": "tx", "shares": "10", "notional": "4"}
    if timestamp:
        payload["matched_at"] = (T0 + timedelta(seconds=1)).isoformat()
    row = AppEvent(
        user_id=1,
        account_id=11,
        aggregate_type="order",
        aggregate_id="order-1",
        source="live_gateway",
        event_type="order_fill_confirmed",
        occurred_at=T0 + timedelta(seconds=2),
        payload_json=json.dumps(payload),
    )
    db.add(row)
    db.commit()
    return row.id


def client(db):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
    return TestClient(app)


def test_report_uses_owned_immutable_sources_and_performs_no_writes(account_db):
    db = account_db
    opening = snapshot(db, T0, "100", {})
    watermark = seed_fill(db)
    closing = snapshot(db, T0 + timedelta(seconds=10), "96", {"123": "10"}, watermark)
    statements = []

    def record(_conn, _cursor, statement, *args):
        statements.append(statement)

    event.listen(db.bind, "before_cursor_execute", record)
    try:
        result = client(db).get(
            "/api/trades/analytics/reconciliation",
            params={"opening_id": opening, "closing_id": closing},
            headers={"X-Account-Id": "11"},
        )
        assert result.status_code == 200
        report = result.json()
        assert report["gross_trading_cashflow"] == "-4"
        assert report["net_realized_pnl"] is None
        assert report["source_ids"] == [watermark]
        assert not report["full_wallet_reconciled"]
        assert not any(
            statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
            for statement in statements
        )
    finally:
        event.remove(db.bind, "before_cursor_execute", record)
    foreign = snapshot(db, T0, "100", {}, account_id=21)
    assert (
        client(db)
        .get(
            "/api/trades/analytics/reconciliation",
            params={"opening_id": foreign, "closing_id": closing},
            headers={"X-Account-Id": "11"},
        )
        .status_code
        == 404
    )
    assert (
        client(db)
        .get("/api/trades/analytics/balance-snapshots", headers={"X-Account-Id": "21"})
        .status_code
        == 403
    )


def test_snapshot_tamper_and_legacy_fill_time_fail_closed(account_db):
    db = account_db
    opening = snapshot(db, T0, "100", {})
    watermark = seed_fill(db, timestamp=False)
    closing = snapshot(db, T0 + timedelta(seconds=10), "96", {"123": "10"}, watermark)
    result = readside.build_reconciliation_report(db, db.get(Account, 11), opening, closing)
    assert "FILL_EXECUTION_TIME_UNVERIFIED" in result["reasons"]
    assert result["cash_residual_usdc"] is None
    db.get(LiveBalanceSnapshot, closing).evidence_json = "{}"
    db.commit()
    with pytest.raises((ValueError, KeyError)):
        readside.build_reconciliation_report(db, db.get(Account, 11), opening, closing)


def test_native_capture_reads_balances_only_and_never_derives_or_posts(account_db, monkeypatch):
    db = account_db
    native = MagicMock()
    native.get_balance_allowance.return_value = {"balance": "100000000"}
    monkeypatch.setattr(
        readside,
        "load_wallet_credentials",
        lambda _: {"private_key": "test-only", "clob_creds": {"api_key": "read"}},
    )
    monkeypatch.setattr(clob_transport, "build_client", lambda *_: native)
    forbidden = MagicMock(side_effect=AssertionError("money mutation forbidden"))
    for name in ("prepare", "submit", "cancel"):
        monkeypatch.setattr(clob_transport, name, forbidden)
    result = client(db).post(
        "/api/trades/analytics/balance-snapshots", headers={"X-Account-Id": "11"}
    )
    assert result.status_code == 200
    assert Decimal(result.json()["cash_usdc"]) == 100
    assert result.json()["coverage"] == "venue_indexed_wallet_tokens"
    assert db.get(LiveBalanceSnapshot, result.json()["id"])
    forbidden.assert_not_called()
    native.create_or_derive_api_key.assert_not_called()
    native.create_order.assert_not_called()


def test_native_capture_rejects_changing_cash_and_creates_no_evidence(account_db, monkeypatch):
    native = MagicMock()
    native.get_balance_allowance.side_effect = [{"balance": "100000000"}, {"balance": "99000000"}]
    monkeypatch.setattr(
        readside, "load_wallet_credentials", lambda _: {"clob_creds": {"api_key": "read"}}
    )
    monkeypatch.setattr(clob_transport, "build_client", lambda *_: native)
    response = client(account_db).post(
        "/api/trades/analytics/balance-snapshots", headers={"X-Account-Id": "11"}
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "BALANCE_CHANGED_DURING_OBSERVATION"
    assert account_db.query(LiveBalanceSnapshot).count() == 0


def test_disabled_feature_and_subaccount_observations_cannot_claim_wallet_cash(
    account_db, monkeypatch
):
    monkeypatch.setattr(get_settings(), "live_account_reconciliation_enabled", False)
    assert (
        client(account_db)
        .get("/api/trades/analytics/balance-snapshots", headers={"X-Account-Id": "11"})
        .status_code
        == 503
    )
    monkeypatch.setattr(get_settings(), "live_account_reconciliation_enabled", True)
    account_db.get(Account, 11).kind = "sub"
    account_db.commit()
    response = client(account_db).post(
        "/api/trades/analytics/balance-snapshots", headers={"X-Account-Id": "11"}
    )
    assert response.status_code == 422
    assert "SUBACCOUNT_WALLET_ALLOCATION_UNVERIFIED" in response.text


def test_fill_ordering_uses_instants_instead_of_timezone_text():
    buy = fill(1, "BUY", "10", "4")
    sell = fill(2, "SELL", "10", "6")
    buy["matched_at"] = "2026-11-20T01:00:01+01:00"
    result = reconcile_confirmed_cashflows([sell, buy], observation(), observation("102"))
    assert result["net_realized_pnl"] == 2
    buy["matched_at"] = "2026-11-20T00:00:01"
    with pytest.raises(ValueError, match="TIME_UNVERIFIED"):
        reconcile_confirmed_cashflows([buy], observation(), observation())


def test_recorded_analytics_are_scoped_and_foreign_accounts_rejected(account_db):
    from app.services.analytics.execution_analytics import get_execution_summary, get_trade_details

    account_db.add(Account(id=12, owner_user_id=1, name="Sub", kind="sub"))
    for account_id, amount in [(None, 1), (11, 2), (12, 3)]:
        account_db.add(
            UserTrade(
                user_id=1,
                account_id=account_id,
                market_id="m",
                action="buy",
                amount=amount,
                price=0.5,
                status="executed",
                strategy_source="manual",
                fee_paid=0,
            )
        )
    account_db.commit()
    assert get_execution_summary(account_db, 1, account_id=11)["totals"]["submissions"] == 2
    assert get_execution_summary(account_db, 1, account_id=12)["totals"]["submissions"] == 1
    assert len(get_trade_details(account_db, 1, account_id=12)) == 1
    with pytest.raises(ValueError, match="Account unavailable"):
        get_execution_summary(account_db, 1, account_id=21)


@pytest.mark.parametrize("indexed_quantity,expected_status", [("2", 200), ("3", 503)])
def test_discovered_external_tokens_are_checked_against_native_balances(
    account_db, monkeypatch, indexed_quantity, expected_status
):
    native = MagicMock()
    native.get_balance_allowance.side_effect = [
        {"balance": "100000000"},
        {"balance": "2000000"},
        {"balance": "100000000"},
    ]
    monkeypatch.setattr(
        readside, "load_wallet_credentials", lambda _: {"clob_creds": {"api_key": "read"}}
    )
    monkeypatch.setattr(clob_transport, "build_client", lambda *_: native)
    monkeypatch.setattr(
        readside,
        "wallet_evidence",
        lambda *args, **kwargs: {
            "positions": {"999": indexed_quantity},
            "activity": [],
            "activity_from": None,
            "activity_to": None,
            "receipts": [],
            "chain_finality": "unverified",
        },
    )
    result = client(account_db).post(
        "/api/trades/analytics/balance-snapshots", headers={"X-Account-Id": "11"}
    )
    assert result.status_code == expected_status
    if expected_status == 200:
        assert Decimal(result.json()["positions"]["999"]) == 2
        assert native.get_balance_allowance.call_args_list[1].args[0].token_id == "999"
    else:
        assert account_db.query(LiveBalanceSnapshot).count() == 0
