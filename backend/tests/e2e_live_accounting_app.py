"""Browser acceptance using real PostgreSQL/API and controlled native read boundary."""

import atexit
import os
from datetime import timedelta
from uuid import uuid4

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from test_live_account_reconciliation import T0, seed_fill, snapshot

from app.api.routes.analysis.live_reconciliation import router
from app.api.routes.auth.auth import get_current_user_from_token
from app.config import get_settings
from app.models import Base
from app.models.users.account import Account
from app.models.users.user import User
from app.models.users.user_settings import UserSettings
from app.security.rate_limit import limiter
from app.utils.database import get_db

url = os.environ["TEST_DATABASE_URL"]
if not url.startswith("postgresql"):
    raise RuntimeError("Disposable PostgreSQL required")
schema = "live_accounting_browser_" + uuid4().hex
admin = create_engine(url)
with admin.begin() as connection:
    connection.execute(text(f'CREATE SCHEMA "{schema}"'))
engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
Base.metadata.create_all(engine)
with Session(engine) as db:
    db.add(User(id=1, wallet_address="0x" + "1" * 40))
    db.flush()
    db.add(UserSettings(user_id=1))
    db.add(Account(id=11, owner_user_id=1, name="Personal", kind="personal", is_default=True))
    db.commit()
    snapshot(db, T0, "100", {})
    watermark = seed_fill(db)
    snapshot(db, T0 + timedelta(seconds=10), "96", {"123": "10"}, watermark)
get_settings().live_account_reconciliation_enabled = True
limiter.enabled = False


def cleanup():
    engine.dispose()
    with admin.begin() as connection:
        connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()


atexit.register(cleanup)
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:15139"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)
app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}


def database():
    with Session(engine) as db:
        yield db


app.dependency_overrides[get_db] = database


@app.get("/")
def health():
    return {"ready": True}


@app.post("/__test/chain")
def seed_chain_scenario():
    """Reset only the isolated browser schema and inject the outbound RPC boundary."""
    from chain_attribution_fixtures import VenueRPC, seed_rpc_account

    from app.services.analytics import chain_attribution, chain_evidence

    rpc = VenueRPC()
    original_reader = chain_evidence.ChainReader

    class ControlledReader(original_reader):
        def __init__(self, **kwargs):
            kwargs["transport"] = rpc.transport()
            super().__init__(**kwargs)

    chain_attribution.ChainReader = ControlledReader
    with Session(engine) as db:
        seed_rpc_account(db, rpc, reset=True)
    return {"ready": True, "classification": "synthetic_rpc_boundary_only"}
