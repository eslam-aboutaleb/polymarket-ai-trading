from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from paper_account_fixtures import db, evidence, order  # noqa: F401

from app.models import PaperAccount, PaperJournalEvent, PaperNavPoint, PaperOrderRecord, UserTrade
from app.services.analytics import paper_accounting as paper
from app.services.users.account_service import AccountAccessDenied


def test_mark_change_without_trade_and_partial_exit_reconcile(db):
    session, clock = db
    result = order(session)
    assert Decimal(result["shares"]) == 25 and Decimal(result["notional_usdc"]) == 10
    clock[0] += timedelta(seconds=1)
    evidence(session, clock, bid="0.60", ask="0.70")
    state = session.get(PaperAccount, 1)
    value = paper.value_account(session, state)
    assert Decimal(value["cash"]) == 990
    assert Decimal(value["position_value"]) == 15
    assert Decimal(value["nav"]) == 1005
    clock[0] += timedelta(seconds=1)
    evidence(session, clock, bid="0.70", ask="0.71")
    sold = order(session, amount="10", side="SELL", key="exit")
    value = paper.value_account(session, state)
    assert Decimal(sold["realized_pnl"]) == 3
    assert Decimal(value["cash"]) == 997
    assert Decimal(value["nav"]) == Decimal("1007.5")
    assert session.query(UserTrade).count() == 2


def test_retry_after_restart_returns_original_before_temporal_checks(db):
    session, clock = db
    result = order(session, as_of=clock[0])
    session.commit()
    clock[0] += timedelta(seconds=1)
    order(session, amount="1", key="another")
    session.commit()
    session.expire_all()
    assert order(session, as_of=clock[0] - timedelta(seconds=1)) == result
    assert session.query(UserTrade).count() == 2
    with pytest.raises(ValueError, match="different economic"):
        order(session, amount="26", as_of=clock[0] - timedelta(seconds=1))


def test_shared_depth_partial_inventory_and_fee_budget(db):
    session, _ = db
    paper.configure_account(session, 1, 1, fee_bps=100)
    first = order(session, amount="90")
    second = order(session, amount="90", key="second")
    assert first["status"] == "filled" and second["status"] == "partial"
    assert Decimal(first["shares"]) + Decimal(second["shares"]) == 100
    value = paper.value_account(session, session.get(PaperAccount, 1))
    assert Decimal(value["cash"]) == Decimal("959.6")
    assert Decimal(value["fees"]) == Decimal("0.4")
    assert order(session, amount="1", key="third")["status"] == "unfilled"
    assert order(session, amount="1", side="SELL", key="sell", account=2)["status"] == "unfilled"


@pytest.mark.parametrize(
    "change,reason",
    [
        ("stale", "STALE_BOOK"),
        ("missing_fee", "FEES_UNKNOWN"),
        ("future", "MISSING_BOOK"),
        ("rule", "MARKET_IDENTITY_OR_RULES_CHANGED"),
    ],
)
def test_missing_evidence_never_creates_cash_or_trades(db, change, reason):
    session, clock = db
    if change == "stale":
        clock[0] += timedelta(seconds=61)
    if change == "missing_fee":
        session.get(PaperAccount, 1).fee_bps = None
    if change == "future":
        from app.models import ReplayCapture

        for row in session.query(ReplayCapture).filter_by(kind="book"):
            row.observed_at = clock[0] + timedelta(seconds=1)
    if change == "rule":
        from app.models import ReplayCapture

        for row in session.query(ReplayCapture).filter_by(kind="gamma"):
            row.rule_hash = "b" * 64
    result = order(session)
    assert result["reason"] == reason and result["status"] == "unfilled"
    assert session.query(PaperJournalEvent).count() == session.query(UserTrade).count() == 0


def test_missing_mark_is_unknown_not_flat_or_zero(db):
    session, clock = db
    order(session)
    clock[0] += timedelta(seconds=61)
    value = paper.value_account(session, session.get(PaperAccount, 1))
    assert value["nav"] is value["net_pnl"] is None
    assert value["quality"] == "incomplete" and value["missing_marks"] == ["11"]
    assert Decimal(value["cash"]) == 990


def test_accounts_and_rollback_are_isolated(db):
    session, _ = db
    with patch("py_clob_client.client.ClobClient.post_order", side_effect=AssertionError("live")):
        order(session)
    assert Decimal(paper.value_account(session, session.get(PaperAccount, 2))["nav"]) == 1000
    with pytest.raises(AccountAccessDenied):
        paper.account_state(session, 1, 3)
    session.rollback()
    assert session.query(UserTrade).count() == session.query(PaperOrderRecord).count() == 0


def test_funding_is_not_profit_and_is_idempotent(db):
    session, _ = db
    value = paper.cash_flow(session, 1, 1, "deposit", "100")
    assert Decimal(value["nav"]) == 1100 and Decimal(value["net_pnl"]) == 0
    paper.cash_flow(session, 1, 1, "deposit", "100")
    assert Decimal(paper.restore(session, session.get(PaperAccount, 1)).cash) == 1100
    with pytest.raises(ValueError, match="identity conflict"):
        paper.cash_flow(session, 1, 1, "deposit", "101")
    with pytest.raises(ValueError, match="withdrawal"):
        paper.cash_flow(session, 1, 1, "withdrawal", "-1101")


def test_final_settlement_is_paired_idempotent_and_visible_in_same_clock(db):
    session, clock = db
    order(session)
    clock[0] += timedelta(seconds=1)
    evidence(session, clock, final="11")
    assert paper.settle_captured(session, 1, 1) == 1
    assert paper.settle_captured(session, 1, 1) == 0
    value = paper.value_account(session, session.get(PaperAccount, 1), clock[0])
    assert Decimal(value["nav"]) == 1015 and not value["positions"]


def test_snapshot_hour_idempotency_and_new_hour_mark_change(db):
    session, clock = db
    order(session)
    state = session.get(PaperAccount, 1)
    first = paper.capture_nav(session, state)
    assert paper.capture_nav(session, state).id == first.id
    clock[0] += timedelta(hours=1)
    evidence(session, clock, bid="0.60", ask="0.61")
    paper.capture_nav(session, state)
    assert session.query(PaperNavPoint).count() == 2


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"order_type": "GTC"}, "RESTING_ORDER_MODEL_UNAVAILABLE"),
        ({"limit_price": Decimal("0.405")}, "OFF_TICK_LIMIT"),
    ],
)
def test_order_semantics_are_explicit(db, kwargs, reason):
    session, _ = db
    assert order(session, **kwargs)["reason"] == reason
    assert session.query(UserTrade).count() == 0
