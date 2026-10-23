"""Disposable paper browser app: real routes/journal/SQL, controlled native receipts.

Only this test process exposes clock/reset controls. Never imported in production.
"""

import atexit
from datetime import UTC, datetime, timedelta
from tempfile import TemporaryDirectory

from fastapi import FastAPI, Header
from paper_account_fixtures import evidence
from pydantic import BaseModel
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.api.routes.auth import accounts
from app.api.routes.auth.auth import get_current_user_from_token
from app.api.routes.portfolio import portfolio
from app.api.routes.trading import paper as routes
from app.config import get_settings
from app.models import Account, AccountSettings, Base, User, UserSettings
from app.security.rate_limit import limiter
from app.services.analytics import equity_snapshot_service as equity
from app.services.analytics import paper_accounting as paper
from app.utils.database import get_db

directory = TemporaryDirectory(prefix="paper-e2e-")
engine = create_engine(
    f"sqlite:///{directory.name}/paper.db", connect_args={"check_same_thread": False, "timeout": 30}
)
atexit.register(directory.cleanup)
atexit.register(engine.dispose)


@event.listens_for(engine, "connect")
def connect(connection, _):
    connection.isolation_level = None
    connection.execute("PRAGMA journal_mode=WAL")


@event.listens_for(engine, "begin")
def begin(connection):
    connection.exec_driver_sql("BEGIN")


sessions = sessionmaker(engine, expire_on_commit=False)
clock = [datetime.now(UTC) - timedelta(hours=3)]
paper.utc_now = lambda: clock[0]
equity.utc_now = lambda: clock[0]
get_settings().paper_journal_v2_enabled = True
get_settings().accounts_enabled = True
app = FastAPI()
app.state.limiter = limiter
limiter.enabled = False
app.include_router(accounts.router)
app.include_router(routes.router)
app.include_router(portfolio.router)
app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}


def database():
    with sessions() as db:
        yield db


app.dependency_overrides[get_db] = database


def forbidden(*_, **__):
    raise AssertionError("Paper E2E accessed private order execution")


from py_clob_client.client import ClobClient  # noqa: E402

ClobClient.post_order = forbidden
ClobClient.create_order = forbidden


@app.post("/__test/reset")
def reset():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    clock[0] = (datetime.now(UTC) - timedelta(hours=3)).replace(minute=0, second=0, microsecond=0)
    with sessions() as db:
        db.add_all(
            [
                User(id=1, wallet_address="0x" + "11" * 20),
                User(id=2, wallet_address="0x" + "22" * 20),
            ]
        )
        db.flush()
        db.add_all(
            [
                Account(id=1, owner_user_id=1, name="Personal", kind="personal", is_default=True),
                Account(id=2, owner_user_id=1, name="Second", kind="sub", is_default=False),
                Account(id=3, owner_user_id=2, name="Foreign", kind="personal", is_default=True),
            ]
        )
        db.add(UserSettings(user_id=1, paper_balance=1000, simulation_mode=True))
        db.add(AccountSettings(account_id=2, paper_balance=1000, simulation_mode=True))
        evidence(db, clock)
        db.commit()
    return {"ok": True}


class Advance(BaseModel):
    seconds: int = 3600
    bid: str | None = None
    ask: str = "0.61"


@app.post("/__test/advance")
def advance(body: Advance):
    clock[0] += timedelta(seconds=body.seconds)
    if body.bid is not None:
        with sessions() as db:
            evidence(db, clock, bid=body.bid, ask=body.ask)
            db.commit()
    return {"as_of": clock[0].isoformat()}


@app.get("/api/settings/paper-summary")
def summary(x_account_id: int = Header(1)):
    from app.services.analytics.simulation import get_paper_summary

    with sessions() as db:
        return get_paper_summary(db, 1, x_account_id)


@app.get("/")
def healthy():
    return {"ok": True}


reset()
