"""Browser acceptance using real PostgreSQL/API and controlled native read boundary."""

import atexit
import os
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from test_live_account_reconciliation import T0, seed_fill, snapshot

from app.api.routes.analysis.live_reconciliation import router
from app.api.routes.auth.auth import get_current_user_from_token
from app.api.routes.portfolio.redemption_intents import router as redemption_router
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

app.include_router(redemption_router)
app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}


def database():
    with Session(engine) as db:
        yield db


app.dependency_overrides[get_db] = database


@app.get("/")
def health():
    return {"ready": True}


@app.post("/__test/chain")
def seed_chain_scenario(
    deposit_usdc: Decimal = Query(Decimal(0), ge=0, le=1_000_000, decimal_places=6),
    classify_deposit: bool = True,
):
    """Reset only the isolated browser schema and inject the outbound RPC boundary."""
    from chain_attribution_fixtures import OTHER, WALLET, VenueRPC, cash, seed_rpc_account

    from app.services.analytics import chain_attribution, chain_evidence

    rpc = VenueRPC()
    activity = []
    if deposit_usdc:
        transaction = "0x" + "e" * 64
        rpc.logs.append(cash(OTHER, WALLET, str(deposit_usdc), 15, transaction, 0))
        rpc.closing_cash += int(deposit_usdc * 1_000_000)
        if classify_deposit:
            activity.append(
                {"type": "DEPOSIT", "transaction_hash": transaction, "usdc_size": str(deposit_usdc)}
            )
    original_reader = chain_evidence.ChainReader

    class ControlledReader(original_reader):
        def __init__(self, **kwargs):
            kwargs["transport"] = rpc.transport()
            super().__init__(**kwargs)

    chain_attribution.ChainReader = ControlledReader
    with Session(engine) as db:
        seed_rpc_account(db, rpc, reset=True, activity=activity)
    return {
        "ready": True,
        "classification": "synthetic_rpc_boundary_only",
        "synthetic_deposit_usdc": str(deposit_usdc),
    }


@app.post("/__test/redemption")
def seed_redemption_scenario(submitted: bool = False):
    from unittest.mock import patch

    from redemption_fixtures import CONDITION, TEST_KEY, TEST_WALLET, FundedRedemptionRPC

    from app.models import AppEvent, RedemptionAttempt, TradingControlState
    from app.models.trading.redemption_intent import (
        RedemptionIntent,
        RedemptionObservation,
        RedemptionWalletLease,
    )
    from app.services.trading import redemption_workflow as workflow

    rpc = FundedRedemptionRPC()
    workflow.NativeRedemption = rpc.native
    app.state.redemption_rpc = rpc
    with Session(engine) as db:
        for model in (
            RedemptionWalletLease,
            RedemptionObservation,
            RedemptionAttempt,
            RedemptionIntent,
        ):
            db.query(model).delete(synchronize_session=False)
        db.query(AppEvent).filter_by(source="redemption_gateway").delete(synchronize_session=False)
        db.get(User, 1).wallet_address = TEST_WALLET
        db.get(UserSettings, 1).simulation_mode = False
        for scope in ("global", "user:1"):
            db.merge(TradingControlState(scope=scope, state="RUNNING", reason="synthetic fixture"))
        db.commit()
        if submitted:
            native = rpc.native()
            try:
                row = workflow.create_intent(
                    db,
                    db.get(Account, 11),
                    CONDITION,
                    "mock-submission",
                    mode="submit",
                    chain=native,
                )
                with (
                    patch.object(workflow, "block_legacy_live_execution", lambda _: None),
                    patch.object(
                        workflow, "load_wallet_credentials", lambda _: {"private_key": TEST_KEY}
                    ),
                ):
                    workflow.submit_intent(db, db.get(Account, 11), row.id, chain=native)
            finally:
                native.close()
    return {"condition_id": CONDITION, "classification": "synthetic_mock_funded_redemption"}


@app.post("/__test/redemption/finality")
def finalize_mock_redemption(reorg: bool = False):
    rpc = app.state.redemption_rpc
    rpc.final_number = 30
    if reorg:
        rpc.receipt["blockHash"] = "0x" + "f" * 64
    return {
        "classification": "synthetic_mock_finality",
        "broadcasts": len(rpc.broadcasts),
        "nonce_reads": rpc.nonce_reads,
    }
