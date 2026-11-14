"""Account-scoped API exercises real SQL and bounded controlled chain transport."""

import json
from datetime import datetime
from unittest.mock import AsyncMock

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from test_recorded_event_risk import seed
from test_settlement_acceptance import CID, TX, WALLET, receipt_fixture
from test_valuation_recorded_evidence import valuation_db as _valuation_db

from app.api.routes.auth.auth import get_current_user_from_token
from app.api.routes.portfolio.settlement import router
from app.config import get_settings
from app.models.trading.redemption_attempt import RedemptionAttempt
from app.utils.database import get_db

valuation_db = _valuation_db


def exercise_recorded(db, inp, monkeypatch):
    seed(db, inp)
    db.add(
        RedemptionAttempt(
            id=1,
            user_id=1,
            account_id=1,
            market_id=CID,
            collection_id="0x" + "00" * 32,
            status="confirmed",
            amount=40,
            tx_hash=TX,
            created_at=inp.as_of,
        )
    )
    db.add(
        RedemptionAttempt(
            id=2,
            user_id=2,
            account_id=3,
            market_id=CID,
            collection_id="0x" + "00" * 32,
            status="preview",
            amount=100,
            created_at=inp.as_of,
        )
    )
    from app.models.users.user import User

    db.get(User, 1).wallet_address = WALLET
    db.commit()

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return inp.as_of.astimezone(tz) if tz else inp.as_of

    monkeypatch.setattr("app.services.analytics.settlement_read_side.datetime", Clock)
    monkeypatch.setattr(get_settings(), "settlement_redemption_enabled", True)
    receipt, block, head = receipt_fixture()
    calls = []

    def handler(request):
        data = json.loads(request.content)
        calls.append(data["method"])
        result = {
            "eth_chainId": "0x89",
            "eth_getTransactionReceipt": receipt,
            "eth_getBlockByNumber": block,
            "eth_blockNumber": hex(head),
        }[data["method"]]
        return httpx.Response(200, json={"id": 1, "jsonrpc": "2.0", "result": result})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "app.services.analytics.settlement_read_side.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    app = FastAPI()
    app.include_router(router)

    def get():
        yield db

    app.dependency_overrides[get_db] = get
    app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
    writes = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(statement)

    event.listen(db.get_bind(), "before_cursor_execute", capture)
    try:
        with TestClient(app) as client:
            first = client.get("/api/settlement/recorded")
            assert first.status_code == 200, first.text
            data = first.json()
            assert len(data["attempts"]) == 1
            assert data["attempts"][0]["status"] == "confirmed", data
            assert data["observed_redemption_cashflows_usdc"] == "40"
            assert data["available_cash_usdc"] is None
            assert data["realized_pnl_usdc"] is None
            assert data["accounting_status"] == "abstain"
            assert data["execution_eligible"] is False
            again = client.get("/api/settlement/recorded").json()
            assert again["observed_redemption_cashflows_usdc"] == "40"
            assert (
                client.get("/api/settlement/recorded", headers={"X-Account-Id": "3"}).status_code
                == 403
            )
            assert (
                client.get("/api/settlement/recorded", headers={"X-Account-Id": "999"}).status_code
                == 403
            )
            assert writes == []
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", capture)
    assert calls.count("eth_getTransactionReceipt") == 2
    return app


def test_recorded_receipt_is_read_only_repeatable_and_account_scoped(valuation_db, monkeypatch):
    db, _, inp = valuation_db
    exercise_recorded(db, inp, monkeypatch)


def test_rpc_outage_returns_unknown_without_zero_or_ledger_mutation(valuation_db, monkeypatch):
    db, _, inp = valuation_db
    app = exercise_recorded(db, inp, monkeypatch)
    monkeypatch.setattr(
        "app.services.analytics.settlement_read_side.read_redemption_cashflow",
        AsyncMock(side_effect=httpx.ConnectError("unavailable")),
    )
    with TestClient(app) as client:
        result = client.get("/api/settlement/recorded").json()
        assert result["status"] == "abstain"
        assert result["observed_redemption_cashflows_usdc"] is None
        assert result["attempts"][0]["status"] == "unknown"
        assert (
            db.scalar(select(RedemptionAttempt.status).where(RedemptionAttempt.id == 1))
            == "confirmed"
        )


def test_duplicate_attempts_preview_and_changed_canonical_receipt(valuation_db, monkeypatch):
    db, _, inp = valuation_db
    app = exercise_recorded(db, inp, monkeypatch)
    db.add(
        RedemptionAttempt(
            id=3,
            user_id=1,
            account_id=1,
            market_id=CID,
            collection_id="0x" + "00" * 32,
            status="confirmed",
            amount=40,
            tx_hash=TX,
            created_at=inp.as_of,
        )
    )
    db.add(
        RedemptionAttempt(
            id=4,
            user_id=1,
            account_id=1,
            market_id=CID,
            collection_id="0x" + "00" * 32,
            status="preview",
            amount=100,
            created_at=inp.as_of,
        )
    )
    db.commit()
    with TestClient(app) as client:
        result = client.get("/api/settlement/recorded").json()
        assert result["observed_redemption_cashflows_usdc"] is None
        assert result["attempts"][1]["status"] == "duplicate_not_counted"
        assert result["attempts"][2]["status"] == "preview_not_broadcast"
        monkeypatch.setattr(
            "app.services.analytics.settlement_read_side.read_redemption_cashflow",
            AsyncMock(side_effect=ValueError("NONCANONICAL_RECEIPT")),
        )
        changed = client.get("/api/settlement/recorded").json()
        assert changed["attempts"][0]["status"] == "unknown"
        assert changed["available_cash_usdc"] is None
        assert changed["realized_pnl_usdc"] is None
