"""Disposable latency safety browser harness; never imported in production.

Real SQL, runner, native adapter, API and gateway. Only public venue HTTP and
private exchange I/O are controlled. No wallet credentials or funded keys.
"""

import atexit
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from tempfile import TemporaryDirectory

import httpx
from fastapi import FastAPI
from latency_book_fixtures import snapshot
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from test_latency_arb_service_coverage import _opportunity

from app.api.routes.auth.auth import get_current_user_from_token
from app.api.routes.trading.latency_arb import router
from app.config import get_settings
from app.models import Base, User, UserSettings
from app.models.platform.app_event import AppEvent
from app.models.trading.latency_arb_config import LatencyArbConfig
from app.models.trading.live_order import LiveOrder
from app.models.trading.trading_control import TradingControlState
from app.models.trading.user_trade import UserTrade
from app.services.trading import latency_arb_service as runner
from app.services.trading import latency_book
from app.services.trading.execution import clob_transport, live
from app.utils.database import get_db

folder = TemporaryDirectory(prefix="latency-safety-e2e-")
engine = create_engine(
    f"sqlite:///{folder.name}/test.db", connect_args={"check_same_thread": False}
)
sessions = sessionmaker(engine, expire_on_commit=False)
atexit.register(folder.cleanup)
atexit.register(engine.dispose)
app = FastAPI()
app.include_router(router)
app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
runner.SessionLocal = sessions
scenario = ["outage"]
calls = {"prepare": 0, "post": 0}
last_opportunity = [None]


def database():
    with sessions() as db:
        yield db


app.dependency_overrides[get_db] = database


def transport(request):
    raw, metadata, now = snapshot(asks=[{"price": ".500", "size": "60"}])
    if request.url.path.endswith("/book"):
        if scenario[0] == "outage":
            return httpx.Response(503)
        if scenario[0] == "stale":
            raw["timestamp"] = str(int((now - timedelta(seconds=3)).timestamp() * 1000))
        return httpx.Response(200, json=raw)
    if request.url.path.endswith("/fee-rate"):
        return httpx.Response(200, json={} if scenario[0] == "fees" else {"base_fee": 0})
    if "gamma" in request.url.host:
        return httpx.Response(200, json=[metadata["raw"]["gamma"]])
    return httpx.Response(200, json=metadata["raw"]["clob"])


class FixtureVenue(latency_book.LiveVenue):
    async def get(self, url, params=None):
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            self.client = client
            return await super().get(url, params)


latency_book.LiveVenue = FixtureVenue
live.load_wallet_credentials = lambda _: {"test_boundary": True}
clob_transport.build_client = lambda *_: object()
clob_transport.balance = lambda *_: Decimal(1000)


def prepare(client, token, side, price, shares, **options):
    calls["prepare"] += 1
    assert token == "11" and side == "BUY" and price == Decimal(".500") and shares == 60
    assert price * shares <= 50
    return object(), "0x" + "ab" * 32, shares, price * shares


def submit(client, signed, order_type, post_only):
    assert order_type == "FOK" and not post_only
    calls["post"] += 1
    return {"success": True, "orderID": "0x" + "ab" * 32, "status": "live"}


clob_transport.prepare, clob_transport.submit = prepare, submit


def forbidden(*args, **kwargs):
    raise AssertionError("SDK signing/submission is forbidden in latency E2E")


from py_clob_client.client import ClobClient as Legacy  # noqa: E402
from py_clob_client_v2.client import ClobClient  # noqa: E402

for sdk in (Legacy, ClobClient):
    sdk.create_order = forbidden
    sdk.post_order = forbidden


@app.post("/__test/attempt/{case}")
async def attempt(case: str):
    scenario[0] = case
    runner.binance_ws_service.is_connected = lambda: case != "disconnect"
    calls.update(prepare=0, post=0)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    runner._execution_outcome_cache.delete("outcome:1")
    flags = get_settings()
    flags.latency_arb_live = case != "disabled"
    flags.live_latency_arb_enabled = True
    flags.live_trading_enabled = True
    with sessions.begin() as db:
        db.add(User(id=1, wallet_address="0x" + "11" * 20, is_admin=True))
        db.flush()
        db.add(
            UserSettings(
                user_id=1,
                simulation_mode=False,
                max_position_size=100,
                daily_loss_limit=100,
                initial_capital=1000,
            )
        )
        db.add(
            LatencyArbConfig(
                user_id=1,
                enabled=True,
                max_notional=50,
                edge_threshold=0.03,
                alert_on_opportunity=False,
            )
        )
        for scope in ("global", "user:1"):
            db.add(
                TradingControlState(
                    scope=scope,
                    state="HALTED" if case == "halt" else "RUNNING",
                    reason="test",
                    version=1,
                )
            )
    from book_research_fixtures import CID

    opportunity = _opportunity()
    opportunity.market.condition_id = CID
    opportunity.market.token_ids = {"up": "11", "down": "22"}
    opportunity.market.prices = {"up": 0.99, "down": 0.01}  # Deliberately unusable Gamma quote.
    last_opportunity[0] = opportunity
    runner._store_opportunities([opportunity])
    await runner._execute_for_users([opportunity], datetime.now(UTC))
    return counts()


def counts():
    with sessions() as db:
        return {
            **calls,
            "orders": db.scalar(select(func.count()).select_from(LiveOrder)),
            "trades": db.scalar(select(func.count()).select_from(UserTrade)),
            "audit_intents": db.scalar(
                select(func.count())
                .select_from(AppEvent)
                .where(AppEvent.event_type == "order_intent_created")
            ),
            "outcome": runner.get_execution_outcome(1),
        }


@app.post("/__test/retry")
async def retry():
    now = datetime.now(UTC)
    opportunity = replace(last_opportunity[0], detected_at=now)
    await runner._execute_for_users([opportunity], now)
    return counts()


@app.post("/__test/disable")
async def disable():
    get_settings().live_latency_arb_enabled = False
    get_settings().latency_arb_live = False
    return await retry()


@app.get("/")
def health():
    return {"ok": True}
