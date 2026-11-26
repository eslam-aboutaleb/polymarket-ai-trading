"""Real PostgreSQL locks, concurrent intents and process-death recovery; no venue I/O."""

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.models import Base
from app.models.platform.app_event import AppEvent
from app.models.trading.live_order import LiveOrder
from app.models.trading.trading_control import TradingControlState
from app.models.trading.user_trade import UserTrade
from app.models.users.user import User
from app.models.users.user_settings import UserSettings
from app.services.trading.execution import clob_transport, live
from app.services.trading.live_trading_policy import LiveTradingBlocked
from app.services.trading.trading_halt_service import request_halt
from app.utils.time import utc_now


@pytest.fixture
def pg_gateway(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL required")
    schema = "live_gateway_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id=1, wallet_address="0x" + "1" * 40))
        db.flush()
        db.add(
            UserSettings(
                user_id=1,
                simulation_mode=False,
                max_position_size=100,
                daily_loss_limit=50,
                initial_capital=1000,
            )
        )
        for scope in ("global", "user:1"):
            db.add(TradingControlState(scope=scope, state="RUNNING", reason="test", version=1))
        db.commit()
    monkeypatch.setattr(live, "get_settings", lambda: SimpleNamespace(live_trading_enabled=True))
    monkeypatch.setattr(live, "load_wallet_credentials", lambda _: {"private_key": "test-only"})
    client = MagicMock()
    monkeypatch.setattr(clob_transport, "build_client", lambda *_: client)
    monkeypatch.setattr(clob_transport, "balance", lambda *_: Decimal(1000))

    def prepare(_client, token, side, price, shares, **options):
        identity = "0x" + uuid4().hex * 2
        return {"id": identity}, identity, shares, shares * price

    monkeypatch.setattr(clob_transport, "prepare", prepare)

    def submit(_client, signed, _type, _post_only):
        with Session(engine) as observer:
            row = observer.scalar(select(LiveOrder).where(LiveOrder.order_hash == signed["id"]))
            assert row.state == "SUBMITTING" and row.reserved > 0
            assert observer.scalar(
                select(AppEvent.id).where(
                    AppEvent.aggregate_id == row.order_hash,
                    AppEvent.event_type == "order_intent_created",
                )
            )
        return {"success": True, "orderID": signed["id"]}

    submit_mock = MagicMock(side_effect=submit)
    monkeypatch.setattr(clob_transport, "submit", submit_mock)
    try:
        yield engine, schema, client, submit_mock
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def place(engine, key="intent", shares=10):
    with Session(engine, expire_on_commit=False) as db:
        return live.place_live_order(
            db, user_id=1, token_id="123", side="BUY", price=0.5, size=shares, idempotency_key=key
        )


def test_concurrent_same_intent_submits_once(pg_gateway):
    engine, _, _, submit = pg_gateway
    barrier = Barrier(2)

    def worker(_):
        barrier.wait(timeout=10)
        return place(engine)

    with ThreadPoolExecutor(2) as workers:
        results = list(workers.map(worker, range(2)))
    assert results[0]["order_hash"] == results[1]["order_hash"]
    assert results[0]["trade_id"] == results[1]["trade_id"]
    submit.assert_called_once()
    with Session(engine) as db:
        assert len(db.scalars(select(LiveOrder)).all()) == 1
        assert len(db.scalars(select(UserTrade)).all()) == 1
        assert len(db.scalars(select(AppEvent)).all()) == 2


def test_competing_orders_cannot_double_spend_budget(pg_gateway):
    engine, _, _, submit = pg_gateway
    barrier = Barrier(2)

    def worker(index):
        barrier.wait(timeout=10)
        try:
            return place(engine, key=f"intent-{index}", shares=80)
        except LiveTradingBlocked:
            return None

    with ThreadPoolExecutor(2) as workers:
        results = list(workers.map(worker, range(2)))
    assert sum(result is not None for result in results) == 1
    submit.assert_called_once()
    with Session(engine) as db:
        assert sum(row.reserved for row in db.scalars(select(LiveOrder))) == 40


CRASH_WORKER = """
import os
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.services.trading.execution import clob_transport, live
engine=create_engine(os.environ["TEST_DATABASE_URL"],
    connect_args={"options":"-csearch_path="+os.environ["GATEWAY_TEST_SCHEMA"]})
live.get_settings=lambda: SimpleNamespace(live_trading_enabled=True)
live.load_wallet_credentials=lambda _: {"private_key":"test-only"}
clob_transport.build_client=lambda *_: object()
clob_transport.balance=lambda *_: Decimal(1000)
clob_transport.prepare=lambda c,t,s,p,q,**kw: (object(),"0x"+"a"*64,q,p*q)
clob_transport.submit=lambda *_: os._exit(73)
if os.environ["GATEWAY_CRASH_PHASE"] == "before_commit":
    original=live.audit
    def audit(*args,**kwargs):
        original(*args,**kwargs)
        os._exit(72)
    live.audit=audit
with Session(engine) as db:
    live.place_live_order(db,user_id=1,token_id="123",side="BUY",price=.5,
        size=10,idempotency_key="crash-intent")
"""


@pytest.mark.parametrize("phase,exit_code", [("before_commit", 72), ("after_send", 73)])
def test_hard_process_exit_preserves_exactly_one_recoverable_intent(pg_gateway, phase, exit_code):
    engine, schema, client, submit = pg_gateway
    result = subprocess.run(  # noqa: S603 - fixed test interpreter and source, isolated schema
        [sys.executable, "-c", CRASH_WORKER],
        cwd=Path(__file__).parents[2],
        env={
            **os.environ,
            "PYTHONPATH": ".:../services/shared",
            "GATEWAY_TEST_SCHEMA": schema,
            "GATEWAY_CRASH_PHASE": phase,
        },
        timeout=30,
        capture_output=True,
        check=False,
    )
    assert result.returncode == exit_code, result.stderr.decode()
    with Session(engine) as db:
        row = db.scalar(select(LiveOrder))
        if phase == "before_commit":
            assert row is None
            assert db.scalar(select(UserTrade.id)) is None
            assert db.scalar(select(AppEvent.id)) is None
            submit.assert_not_called()
            return
        assert row.state == "SUBMITTING" and row.reserved == 5
    replay = place(engine, key="crash-intent")
    assert replay["status"] == "submitting"
    submit.assert_not_called()  # A restarted process must never resend blindly.
    client.get_order.return_value = {
        "id": replay["order_hash"],
        "status": "CANCELED",
        "original_size": "10",
        "size_matched": "0",
    }
    with Session(engine) as db:
        live.reconcile_pending_live_orders(db)
        row = db.scalar(select(LiveOrder))
        assert row.state == "CANCELLED" and row.reserved == 0


def test_halt_acknowledgement_waits_for_inflight_submission(pg_gateway):
    engine, _, _, submit = pg_gateway
    entered, release, halt_started = Event(), Event(), Event()
    original = submit.side_effect

    def delayed(*args):
        entered.set()
        assert release.wait(timeout=10)
        return original(*args)

    submit.side_effect = delayed

    def halt():
        halt_started.set()
        with Session(engine) as db:
            request_halt(db, user_id=1)

    with ThreadPoolExecutor(2) as workers:
        order = workers.submit(place, engine)
        assert entered.wait(timeout=10)
        halted = workers.submit(halt)
        assert halt_started.wait(timeout=10)
        try:
            # The order holds the wallet lock until its result is durable.
            with pytest.raises(TimeoutError):
                halted.result(timeout=0.2)
        finally:
            release.set()
        assert order.result(timeout=10)["success"]
        halted.result(timeout=10)
    with pytest.raises(LiveTradingBlocked, match="HALTED"):
        place(engine, key="after-halt")
    submit.assert_called_once()


def test_copy_permission_is_locked_until_submission_result_commits(pg_gateway, monkeypatch):
    from sqlalchemy.exc import OperationalError

    from app.models.trading.followed_trader import FollowedTrader

    engine, _, _, submit = pg_gateway
    wallet = "0x" + "2" * 40
    with Session(engine) as db:
        settings = db.scalar(select(UserSettings))
        settings.copy_trading_enabled = True
        settings.require_ai_approval = False
        db.add(FollowedTrader(user_id=1, trader_wallet=wallet, is_active=True))
        db.commit()
    monkeypatch.setattr(
        live,
        "get_settings",
        lambda: SimpleNamespace(live_trading_enabled=True, live_copy_trading_enabled=True),
    )
    original = submit.side_effect
    observation = {}

    def probe_grant(*args):
        with Session(engine) as concurrent:
            try:
                concurrent.scalar(select(FollowedTrader).with_for_update(nowait=True))
                observation["locked"] = False
            except OperationalError as exc:
                observation["locked"] = getattr(exc.orig, "pgcode", None) == "55P03"
        return original(*args)

    submit.side_effect = probe_grant
    with Session(engine) as db:
        result = live.place_live_order(
            db,
            user_id=1,
            token_id="123",
            side="BUY",
            price=0.5,
            size=10,
            idempotency_key="copy-grant",
            strategy="copy",
            copied_from_wallet=wallet,
            market_id="market",
            order_type="FOK",
            execution_deadline=utc_now() + timedelta(seconds=5),
            execution_evidence={
                "condition": "market",
                "token": "123",
                "tick_size": ".01",
                "neg_risk": False,
            },
        )
    assert result["success"] and observation["locked"]
    submit.assert_called_once()
