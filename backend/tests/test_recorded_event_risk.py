"""Confirmed fills, uncertain reservations and account boundaries on the actual reader."""

from datetime import datetime, timedelta
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_valuation_recorded_evidence import valuation_db as _valuation_db

from app.api.routes.auth.auth import get_current_user_from_token
from app.api.routes.portfolio.event_risk import router
from app.config import get_settings
from app.models import Account
from app.models.trading.live_order import LiveOrder
from app.models.trading.user_trade import UserTrade
from app.services.analytics.portfolio_risk_service import recorded_event_risk
from app.utils.database import get_db

valuation_db = _valuation_db


def seed(db, inp):
    now = inp.as_of
    db.add(
        UserTrade(
            id=1,
            user_id=1,
            account_id=1,
            market_id=inp.condition_id,
            token_id="11",
            action="buy",
            status="executed",
            amount=55,
            price=0.55,
            filled_size=100,
            filled_price=0.55,
            fee_paid=0,
            created_at=now - timedelta(seconds=10),
            executed_at=now - timedelta(seconds=5),
            updated_at=now - timedelta(seconds=1),
        )
    )
    db.add(
        UserTrade(
            id=2,
            user_id=1,
            account_id=1,
            market_id=inp.condition_id,
            token_id="22",
            action="buy",
            status="pending",
            amount=40,
            price=0.4,
            created_at=now - timedelta(seconds=1),
            updated_at=now,
        )
    )
    db.flush()
    db.add(
        LiveOrder(
            id=1,
            user_id=1,
            account_id=1,
            trade_id=2,
            idempotency_key="fixture-pending",
            fingerprint="fixture-source",
            strategy="manual",
            token_id="22",
            side="BUY",
            price=Decimal(".4"),
            shares=Decimal("100"),
            notional=Decimal("40"),
            reserved=Decimal("40"),
            state="SUBMITTED",
            created_at=now - timedelta(seconds=1),
            updated_at=now,
            response={},
        )
    )
    db.commit()


def test_repeated_reads_cancellation_late_fill_and_future_mutation(valuation_db):
    db, _, inp = valuation_db
    seed(db, inp)
    account = db.get(Account, 1)
    first = recorded_event_risk(db, account, as_of=inp.as_of)
    assert first.status == "assessed", first.abstain_reasons
    assert first.worst_case_portfolio_loss >= 55
    assert recorded_event_risk(db, account, as_of=inp.as_of).model_dump() == first.model_dump()
    assert db.query(UserTrade).count() == 2
    trade, order = db.get(UserTrade, 2), db.get(LiveOrder, 1)
    cancelled_at = inp.as_of + timedelta(seconds=1)
    trade.updated_at = cancelled_at
    order.updated_at = cancelled_at
    trade.status = "cancelled"
    order.state = "CANCELLED"
    order.response = {"size_matched": "0"}
    db.commit()
    cancelled = recorded_event_risk(db, account, as_of=cancelled_at)
    assert cancelled.worst_case_portfolio_loss == 55
    # A subsequently confirmed late partial fill changes exposure; it is not
    # erased merely because cancellation previously completed.
    trade.status = "executed"
    trade.filled_size = 40
    trade.filled_price = 0.4
    trade.fee_paid = 0
    trade.executed_at = inp.as_of
    trade.updated_at = inp.as_of + timedelta(seconds=2)
    order.response = {"size_matched": "40"}
    order.updated_at = trade.updated_at
    db.commit()
    assert recorded_event_risk(db, account, as_of=inp.as_of).status == "abstain"
    late = recorded_event_risk(db, account, as_of=inp.as_of + timedelta(seconds=2))
    assert late.worst_case_portfolio_loss == 31


def test_missing_fill_unknown_lifecycle_and_dispute_do_not_become_zero(valuation_db):
    db, _, inp = valuation_db
    seed(db, inp)
    trade = db.get(UserTrade, 1)
    trade.filled_size = None
    db.commit()
    result = recorded_event_risk(db, db.get(Account, 1), as_of=inp.as_of)
    assert result.status == "abstain" and result.worst_case_portfolio_loss is None
    trade.filled_size = 100
    trade.action = "redeem"
    db.commit()
    result = recorded_event_risk(db, db.get(Account, 1), as_of=inp.as_of)
    assert result.status == "abstain" and result.worst_case_portfolio_loss is None


def test_recorded_api_is_scoped_and_never_falls_back_from_foreign_account(
    valuation_db, monkeypatch
):
    db, _, inp = valuation_db
    seed(db, inp)
    monkeypatch.setattr(get_settings(), "event_correlation_risk_enabled", True)

    class Clock(datetime):
        frozen_time = inp.as_of

        @classmethod
        def now(cls, tz=None):
            return cls.frozen_time.astimezone(tz) if tz else cls.frozen_time

    monkeypatch.setattr("app.services.analytics.event_exposure_read_side.datetime", Clock)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
    with TestClient(app) as client:
        assert client.get("/api/risk/event-correlation/recorded").json()["status"] == "assessed"
        assert (
            client.get(
                "/api/risk/event-correlation/recorded", headers={"X-Account-Id": "3"}
            ).status_code
            == 403
        )
        assert (
            client.get(
                "/api/risk/event-correlation/recorded", headers={"X-Account-Id": "999"}
            ).status_code
            == 403
        )
        own = client.get("/api/risk/event-correlation/recorded", headers={"X-Account-Id": "2"})
        assert own.status_code == 200 and Decimal(own.json()["worst_case_portfolio_loss"]) == 0


def test_stale_rule_capture_is_unknown_risk(valuation_db):
    db, _, inp = valuation_db
    seed(db, inp)
    result = recorded_event_risk(db, db.get(Account, 1), as_of=inp.as_of + timedelta(seconds=301))
    assert result.status == "abstain"
    assert "ABSTAIN_STALE_POSITION_RULE" in result.abstain_reasons
    assert result.worst_case_portfolio_loss is None


def test_aggregated_sells_retain_a_conservative_remaining_cost_basis(valuation_db):
    db, _, inp = valuation_db
    for identity, side, price, created, executed in [
        (1, "buy", 0.8, 20, 5),
        (2, "buy", 0.2, 10, 9),
        (3, "sell", 0.5, 4, 3),
    ]:
        db.add(
            UserTrade(
                id=identity,
                user_id=1,
                account_id=1,
                market_id=inp.condition_id,
                token_id="11",
                action=side,
                status="executed",
                amount=100 * price,
                price=price,
                filled_size=100,
                filled_price=price,
                fee_paid=0,
                created_at=inp.as_of - timedelta(seconds=created),
                executed_at=inp.as_of - timedelta(seconds=executed),
                updated_at=inp.as_of,
            )
        )
    db.commit()
    result = recorded_event_risk(db, db.get(Account, 1), as_of=inp.as_of)
    assert result.status == "assessed", result.abstain_reasons
    assert result.worst_case_portfolio_loss == 80
