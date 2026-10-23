import asyncio
from datetime import timedelta
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient
from paper_account_fixtures import db as _paper_db
from paper_account_fixtures import evidence, order

from app.api.routes.auth.auth import get_current_user_from_token
from app.api.routes.portfolio import portfolio
from app.api.routes.trading import paper as routes
from app.models import PaperAccount, PaperNavPoint
from app.services.analytics import equity_snapshot_service as equity
from app.services.analytics import paper_accounting as paper
from app.utils.database import get_db

db = _paper_db


def client_for(session):
    app = FastAPI()
    app.include_router(routes.router)
    app.include_router(portfolio.router)
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
    return TestClient(app)


def test_api_journal_nav_and_account_isolation(db):
    session, clock = db
    with client_for(session) as client:
        assert client.get("/api/paper/account").json()["account_id"] == 1
        body = {
            "token_id": "11",
            "side": "BUY",
            "amount": "25",
            "units": "SHARES",
            "intent_key": "api",
        }
        first = client.post("/api/paper/orders", json=body)
        assert first.status_code == 200, first.text
        assert client.post("/api/paper/orders", json=body).json() == first.json()
        clock[0] += timedelta(seconds=1)
        evidence(session, clock, bid="0.60", ask="0.61")
        value = client.post("/api/paper/snapshot").json()
        assert Decimal(value["nav"]) == 1005
        rows = client.get("/api/portfolio/equity/snapshots?is_paper=true&from=2026-01-01").json()
        assert rows[-1]["bankroll"] == 1005 and rows[-1]["total_current_value"] == 15
        assert (
            client.get("/api/paper/account", headers={"X-Account-Id": "2"}).json()["nav"]
            == "1000.00000000"
        )
        assert client.get("/api/paper/journal", headers={"X-Account-Id": "2"}).json() == []
        assert client.get("/api/paper/account", headers={"X-Account-Id": "3"}).status_code == 403
        assert client.get("/api/paper/account", headers={"X-Account-Id": "999"}).status_code == 403
        assert client.get("/api/paper/account", headers={"X-Account-Id": "bad"}).status_code == 400


def test_null_gaps_cashflows_and_mode_never_mix(db):
    session, clock = db
    order(session)
    state = session.get(PaperAccount, 1)
    paper.capture_nav(session, state)
    clock[0] += timedelta(hours=1)
    evidence(session, clock, bid="0.60", ask="0.61")
    paper.cash_flow(session, 1, 1, "topup", "100")
    paper.capture_nav(session, state)
    result = equity.summarize_snapshots(session, 1, "all", is_paper=True, now=clock[0])
    assert result["net_cash_flows"] == 100 and result["delta"] == 5.25
    assert result["delta_pct"] is result["max_drawdown_pct"] is None
    clock[0] += timedelta(hours=1)
    paper.capture_nav(session, state)
    rows = equity.get_snapshots(session, 1, is_paper=True)
    assert rows[-1]["bankroll"] is None and rows[-1]["quality"] == "incomplete"
    assert equity.get_snapshots(session, 1, is_paper=False) == []
    assert (
        equity.summarize_snapshots(session, 1, "all", is_paper=True, now=clock[0])["delta"] is None
    )


def test_scheduled_writer_samples_all_paper_accounts_once(db):
    session, _ = db
    from app.models import User

    assert asyncio.run(equity.capture_user_snapshot(session, session.get(User, 1))) is not None
    assert session.query(PaperNavPoint).count() == 2
    asyncio.run(equity.capture_user_snapshot(session, session.get(User, 1)))
    assert session.query(PaperNavPoint).count() == 2


def test_equal_net_cashflow_still_disables_percent_returns(db):
    session, clock = db
    state = session.get(PaperAccount, 1)
    paper.capture_nav(session, state)
    clock[0] += timedelta(hours=1)
    paper.cash_flow(session, 1, 1, "in", "100")
    paper.cash_flow(session, 1, 1, "out", "-100")
    paper.capture_nav(session, state)
    summary = equity.summarize_snapshots(session, 1, "all", is_paper=True, now=clock[0])
    assert summary["delta"] == summary["net_cash_flows"] == 0
    assert summary["delta_pct"] is summary["max_drawdown_pct"] is None


def test_snapshot_api_returns_the_persisted_hour_and_never_resets_funding(db):
    from app.models import UserSettings

    session, _ = db
    with client_for(session) as client:
        first = client.post("/api/paper/snapshot").json()
        order(session)
        session.get(UserSettings, 1).paper_balance = 100_000
        session.commit()
        repeated = client.post("/api/paper/snapshot").json()
        assert repeated == first
        current = client.get("/api/paper/account").json()
        assert Decimal(current["nav"]) == Decimal("999.75")
        assert Decimal(current["funding"]) == 1000


def test_paper_digest_uses_journal_nav_and_never_fetches_wallet_positions(db):
    from unittest.mock import AsyncMock, patch

    from app.models import User
    from app.services.users.daily_report_service import _assemble_digest_data

    session, clock = db
    paper.capture_nav(session, session.get(PaperAccount, 1))
    session.commit()
    with (
        patch(
            "app.services.users.daily_report_service._fetch_positions",
            side_effect=AssertionError("wallet"),
        ),
        patch(
            "app.services.users.daily_report_service._fetch_catalysts",
            new=AsyncMock(return_value=[]),
        ),
    ):
        report = asyncio.run(
            _assemble_digest_data(
                session,
                session.get(User, 1),
                paper=True,
                now=clock[0],
                day_start=clock[0].replace(hour=0),
            )
        )
    assert report["current_value"] == report["start_value"] == 1000
    assert report["realized_pnl_today"] == 0 and report["positions"] == []


def test_asof_window_excludes_later_receipts_and_normalizes_timezone(db):
    from datetime import timezone

    session, clock = db
    state = session.get(PaperAccount, 1)
    paper.capture_nav(session, state)
    clock[0] += timedelta(hours=1, minutes=10)
    paper.capture_nav(session, state)
    cutoff = (clock[0] - timedelta(minutes=5)).astimezone(timezone(timedelta(hours=5)))
    rows = equity.get_snapshots(session, 1, is_paper=True, end=cutoff)
    assert len(rows) == 1
    assert rows[0]["captured_at"].endswith("+00:00")
