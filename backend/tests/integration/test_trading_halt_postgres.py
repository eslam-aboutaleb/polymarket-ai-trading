"""Real PostgreSQL locking, crash rollback, persistence and ownership checks.

Set TEST_DATABASE_URL to a disposable PostgreSQL database. Every test uses a
separate schema and removes it afterward; no application database is used.
"""

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.api.routes.auth.auth import get_current_user_from_token
from app.api.routes.trading import risk, trades
from app.models import Base
from app.models.trading.inverse_bot_position import InverseBotPosition
from app.models.trading.trading_control import EmergencyHaltOperation, TradingControlState
from app.models.users.user import User
from app.models.users.user_settings import UserSettings
from app.services.trading.halt_cancellation_worker import process_next_halt
from app.services.trading.trading_halt_service import operation_status, read_control, request_halt
from app.utils.database import get_db


@pytest.fixture
def sessions():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for real PostgreSQL tests")
    schema = "halt_test_" + uuid.uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        Base.metadata.create_all(engine)
        with factory.begin() as db:
            db.add_all([User(id=i, wallet_address="0x" + f"{i:040x}") for i in (1, 2)])
            db.add(TradingControlState(scope="global", state="RECOVERY_REQUIRED", reason="release"))
        yield factory
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


class Exchange:
    def __init__(self):
        self.open = True
        self.cancels = 0
        self.cancelled_ids = []
        self.final_status = "CANCELED"

    def get_orders(self):
        return [{"id": "one"}] if self.open else []

    def cancel(self, _order_id):
        self.cancels += 1
        self.cancelled_ids.append(_order_id)
        self.open = False

    def get_order(self, order_id):
        return {"id": order_id, "status": self.final_status}


def test_halt_commits_before_network_and_survives_new_session(sessions):
    with sessions.begin() as db:
        db.add(UserSettings(user_id=1, inverse_bot_enabled=True, copy_trading_enabled=True))
        db.add(InverseBotPosition(user_id=1, token_id="token", condition_id="market", enabled=True))
    with sessions() as db:
        operation = request_halt(db, user_id=1, close_positions=True)[0]
    with sessions() as restarted:
        assert read_control(restarted, "user:1")["state"] == "HALTING"
        result = operation_status(restarted, operation_id=operation.id, user_id=1)
        assert result["operation_status"] == "PENDING"
        assert result["positions_confirmed_closed"] == 0
        settings = restarted.scalar(select(UserSettings).where(UserSettings.user_id == 1))
        assert settings.trading_halted
        assert settings.inverse_bot_enabled is False
        assert settings.copy_trading_enabled is False
        assert restarted.scalar(select(InverseBotPosition)).enabled is False


def test_failed_halt_transaction_does_not_enqueue_work(sessions, monkeypatch):
    with sessions() as db:

        def fail():
            raise RuntimeError("database commit unavailable")

        monkeypatch.setattr(db, "commit", fail)
        with pytest.raises(RuntimeError):
            request_halt(db, user_id=1)
    with sessions() as db:
        assert db.scalars(select(EmergencyHaltOperation)).all() == []
        assert read_control(db, "user:1")["state"] == "RECOVERY_REQUIRED"


def test_concurrent_halt_requests_serialize_wallet_state(sessions):
    def halt():
        with sessions() as db:
            return request_halt(db, user_id=1)[0].id

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: halt(), range(2)))
    assert len(set(ids)) == 2
    with sessions() as db:
        assert read_control(db, "user:1")["version"] == 2


def test_two_workers_cannot_process_same_operation(sessions):
    with sessions() as db:
        request_halt(db, user_id=1)
    exchange = Exchange()
    claimed, release = threading.Event(), threading.Event()

    def adapter(_db, _user_id):
        claimed.set()
        if not release.wait(timeout=10):
            raise TimeoutError("test synchronization failed")
        return exchange

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(process_next_halt, sessions, adapter)
        try:
            assert claimed.wait(timeout=10)
            second = pool.submit(process_next_halt, sessions, lambda *_: exchange)
            assert second.result(timeout=10) is False
        finally:
            release.set()
        assert first.result(timeout=10) is True
    assert exchange.cancels == 1


def test_crash_after_exchange_cancellation_recovers_without_submission(sessions):
    class WorkerCrash(BaseException):
        pass

    exchange = Exchange()
    with sessions() as db:
        operation = request_halt(db, user_id=1)[0]

    class CrashingExchange(Exchange):
        def cancel(self, order_id):
            exchange.cancel(order_id)
            raise WorkerCrash

    with pytest.raises(WorkerCrash):
        process_next_halt(sessions, lambda *_: CrashingExchange())
    with sessions() as db:
        assert db.get(EmergencyHaltOperation, operation.id).state == "PENDING"
        assert db.get(EmergencyHaltOperation, operation.id).report["known_order_ids"] == ["one"]
    assert process_next_halt(sessions, lambda *_: exchange)
    with sessions() as db:
        status = operation_status(db, operation_id=operation.id, user_id=1)
        assert status["operation_status"] == "RECOVERY_REQUIRED"
        assert status["positions_confirmed_closed"] == 0
        assert status["orders_confirmed_cancelled"] == 1
        assert read_control(db, "user:1")["state"] == "RECOVERY_REQUIRED"
    assert exchange.cancelled_ids == ["one", "one"]  # Idempotent cancellation, never a new order.


def test_unavailable_credentials_preserve_unknown_exposure(sessions):
    with sessions() as db:
        operation = request_halt(db, user_id=1)[0]

    def unavailable(*_):
        raise RuntimeError("credential store offline")

    assert process_next_halt(sessions, unavailable)
    with sessions() as db:
        result = operation_status(db, operation_id=operation.id, user_id=1)
        assert result["orders_still_open"] is None
        assert result["reconciliation_status"] == "RECOVERY_REQUIRED"


def test_database_connection_loss_after_cancel_keeps_durable_work_pending(sessions):
    from sqlalchemy.exc import OperationalError

    exchange = Exchange()
    with sessions() as db:
        operation = request_halt(db, user_id=1)[0]

    def adapter(db, _user_id):
        worker_pid = db.scalar(text("SELECT pg_backend_pid()"))

        class DisconnectingExchange(Exchange):
            def cancel(self, order_id):
                exchange.cancel(order_id)
                # Terminate only this test worker's database connection, after
                # the external side effect and before recording its outcome.
                with sessions.begin() as other:
                    other.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": worker_pid})

        return DisconnectingExchange()

    with pytest.raises(OperationalError):
        process_next_halt(sessions, adapter)
    with sessions() as db:
        assert db.get(EmergencyHaltOperation, operation.id).state == "PENDING"
    assert process_next_halt(sessions, lambda *_: exchange)
    assert exchange.cancelled_ids == ["one", "one"]
    with sessions() as db:
        result = operation_status(db, operation_id=operation.id, user_id=1)
        assert result["positions_confirmed_closed"] == 0
        assert result["reconciliation_status"] == "RECOVERY_REQUIRED"


def test_crash_recovery_verifies_original_order_when_open_listing_is_empty(sessions):
    class WorkerCrash(BaseException):
        pass

    exchange = Exchange()
    with sessions() as db:
        operation = request_halt(db, user_id=1)[0]

    class FilledDuringCrash(Exchange):
        def cancel(self, order_id):
            exchange.cancel(order_id)
            exchange.final_status = "MATCHED"
            raise WorkerCrash

    with pytest.raises(WorkerCrash):
        process_next_halt(sessions, lambda *_: FilledDuringCrash())
    assert process_next_halt(sessions, lambda *_: exchange)
    with sessions() as db:
        result = operation_status(db, operation_id=operation.id, user_id=1)
        assert result["known_order_ids"] == ["one"]
        assert result["orders_confirmed_cancelled"] == 0
        assert result["orders_unresolved"] == ["one"]
        assert result["positions_confirmed_closed"] == 0


def test_order_identity_is_committed_before_cancellation(sessions):
    with sessions() as db:
        operation = request_halt(db, user_id=1)[0]
    observed = []

    class VerifiedSnapshotExchange(Exchange):
        def cancel(self, order_id):
            with sessions() as observer:
                saved = observer.get(EmergencyHaltOperation, operation.id)
                observed.append(saved.report.get("known_order_ids"))
            super().cancel(order_id)

    assert process_next_halt(sessions, lambda *_: VerifiedSnapshotExchange())
    assert observed == [["one"]]


def test_stale_cancellation_work_cannot_overwrite_newer_control_version(sessions):
    with sessions() as db:
        request_halt(db, user_id=1)
    client = _client(sessions, 1)
    assert (
        client.post("/api/v1/risk/recovery-request", json={"reason": "owner review"}).status_code
        == 202
    )
    assert process_next_halt(sessions, lambda *_: Exchange())
    with sessions() as db:
        state = read_control(db, "user:1")
        assert state["version"] == 2
        assert state["reason"] == "Recovery requested: owner review"


def _client(sessions, user_id):
    app = FastAPI()
    app.include_router(risk.router)
    app.include_router(trades.router)
    app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": user_id}

    def database():
        with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = database
    return TestClient(app)


def test_halt_operation_is_wallet_owner_only(sessions):
    with sessions() as db:
        operation = request_halt(db, user_id=1)[0]
    assert _client(sessions, 2).get(f"/api/trades/emergency-stop/{operation.id}").status_code == 404
    assert _client(sessions, 2).get("/api/trades/emergency-stop").json() is None
    assert _client(sessions, 1).get(f"/api/trades/emergency-stop/{operation.id}").status_code == 200


def test_only_fresh_admin_authority_can_request_global_halt(sessions):
    client = _client(sessions, 1)
    assert client.post("/api/v1/risk/halt", json={"scope": "global"}).status_code == 403
    with sessions.begin() as db:
        db.get(User, 1).is_admin = True
    assert client.post("/api/v1/risk/halt", json={"scope": "global"}).status_code == 202
    with sessions() as db:
        assert read_control(db, "global")["state"] == "HALTING"
        operations = db.scalars(select(EmergencyHaltOperation)).all()
        assert len(operations) == 2
        assert all(op.requested_by_user_id == 1 for op in operations)


def test_legacy_resume_and_recovery_cannot_reenable_trading(sessions):
    client = _client(sessions, 1)
    response = client.post("/api/trades/emergency-stop?close_positions=true")
    assert response.status_code == 202
    assert response.json()["close_requests_submitted"] == 0
    assert client.post("/api/trades/resume-trading").status_code == 409
    response = client.post("/api/v1/risk/recovery-request", json={"reason": "review"})
    assert response.status_code == 202
    assert response.json()["resumed"] is False
    with sessions() as db:
        settings = db.scalar(select(UserSettings).where(UserSettings.user_id == 1))
        assert settings.copy_trading_enabled is False
        assert settings.trading_halted is True


@pytest.fixture
def live_gateway(sessions, monkeypatch):
    from decimal import Decimal
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from app.services.trading.execution import clob_transport, live

    with sessions.begin() as db:
        db.get(TradingControlState, "global").state = "RUNNING"
        db.add(TradingControlState(scope="user:1", user_id=1, state="RUNNING", reason="test"))
        db.add(
            UserSettings(
                user_id=1,
                max_position_size=10,
                daily_loss_limit=10,
                initial_capital=1000,
                simulation_mode=False,
                require_ai_approval=False,
            )
        )
    monkeypatch.setattr(live, "get_settings", lambda: SimpleNamespace(live_trading_enabled=True))
    monkeypatch.setattr(live, "load_wallet_credentials", lambda _: {"private_key": "test"})
    monkeypatch.setattr(clob_transport, "build_client", lambda *_: object())
    monkeypatch.setattr(clob_transport, "balance", lambda *_: Decimal(100))

    def prepare(client, token, side, price, shares):
        import hashlib

        order_hash = "0x" + hashlib.sha256(token.encode()).hexdigest()
        return order_hash, order_hash, shares, shares * price

    submit = MagicMock(side_effect=lambda client, signed, *_: {"success": True, "orderID": signed})
    monkeypatch.setattr(clob_transport, "prepare", prepare)
    monkeypatch.setattr(clob_transport, "submit", submit)

    def place(key="one", token="123"):
        with sessions() as db:
            return live.place_live_order(
                db, user_id=1, token_id=token, side="BUY", price=0.5, size=16, idempotency_key=key
            )

    return place, submit


def test_live_concurrent_duplicate_source_submits_once(live_gateway):
    place, submit = live_gateway
    start = threading.Barrier(2)

    def concurrent():
        start.wait(timeout=5)
        return place()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: concurrent(), range(2)))
    assert results[0]["order_hash"] == results[1]["order_hash"]
    submit.assert_called_once()
    assert place()["status"] == "submitted"


def test_live_concurrent_strategies_cannot_overspend_budget(live_gateway):
    from app.services.trading.live_trading_policy import LiveTradingBlocked

    place, submit = live_gateway
    start = threading.Barrier(2)

    def concurrent(index):
        start.wait(timeout=5)
        try:
            return place(str(index), str(index))
        except LiveTradingBlocked as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(concurrent, range(2)))
    assert sum(isinstance(result, dict) and result["success"] for result in results) == 1
    assert any(
        "BUDGET_EXCEEDED" in result or "UNRESOLVED_LIVE_ORDER" in result
        for result in results
        if isinstance(result, str)
    )
    with pytest.raises(LiveTradingBlocked, match="BUDGET_EXCEEDED"):
        place("third", "third")
    submit.assert_called_once()


def test_live_crash_after_durable_intent_never_resubmits(live_gateway, sessions):
    from app.models.trading.live_order import LiveOrder

    place, submit = live_gateway
    submit.side_effect = SystemExit("worker died after request could have reached exchange")
    with pytest.raises(SystemExit):
        place()
    with sessions() as db:
        assert db.scalar(select(LiveOrder)).state == "SUBMITTING"
    submit.side_effect = None
    result = place()
    assert result["status"] == "submitting"
    submit.assert_called_once()


def test_live_halt_waits_for_inflight_submission_then_blocks_new_orders(live_gateway, sessions):
    from app.services.trading.live_trading_policy import LiveTradingBlocked

    place, submit = live_gateway
    sending = threading.Event()
    finish = threading.Event()
    halt_started = threading.Event()

    def exchange(client, signed, *_):
        sending.set()
        assert finish.wait(timeout=5)
        return {"success": True, "orderID": signed}

    def halt():
        halt_started.set()
        with sessions() as db:
            return request_halt(db, user_id=1)

    submit.side_effect = exchange
    with ThreadPoolExecutor(max_workers=2) as pool:
        order = pool.submit(place)
        assert sending.wait(timeout=5)
        halted = pool.submit(halt)
        assert halt_started.wait(timeout=5)
        assert not halted.done()
        finish.set()
        assert order.result(timeout=5)["success"]
        halted.result(timeout=5)
    with pytest.raises(LiveTradingBlocked, match="HALTED_OR_RECOVERY"):
        place("later", "later")
    submit.assert_called_once()


def test_halt_cancels_durable_unknown_order_even_if_open_order_snapshot_is_empty(
    live_gateway, sessions
):
    place, submit = live_gateway
    submit.side_effect = TimeoutError()
    unknown = place()
    cancelled = []

    class Adapter:
        def get_orders(self):
            return []

        def cancel(self, order_id):
            cancelled.append(order_id)

        def get_order(self, order_id):
            return {"id": order_id, "status": "CANCELED"}

    with sessions() as db:
        operation = request_halt(db, user_id=1)[0]
        operation_id = operation.id
    process_next_halt(sessions, adapter_factory=lambda *_: Adapter())
    assert cancelled == [unknown["order_hash"]]
    with sessions() as db:
        report = operation_status(db, operation_id=operation_id, user_id=1)
        assert report["orders_confirmed_cancelled"] == 1
        assert report["positions_confirmed_closed"] == 0
