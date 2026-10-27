"""Disposable real-router/SQL/maker fixture. Not imported by production."""

from datetime import timedelta
from decimal import Decimal

from book_research_fixtures import CID, NOW
from e2e_book_app import app, clock, forbidden, reset, sessions

from app.api.routes.trading.market_maker import router
from app.config import get_settings
from app.models import MarketMakerConfig, PaperAccount
from app.services.analytics import paper_accounting as paper
from app.services.trading import paper_maker_runtime as runtime
from app.services.trading.paper_maker_lifecycle import FlowEvent

app.include_router(router)
get_settings().accounts_enabled = True
get_settings().paper_maker_fill_model_v2 = True
get_settings().paper_journal_v2_enabled = True
get_settings().paper_maker_queue_ahead = Decimal("3")
QUOTES = [{"token_id": "11", "side": "BUY", "price": "0.40", "size": "4"}]

from py_clob_client.client import ClobClient as Legacy  # noqa: E402
from py_clob_client_v2.client import ClobClient  # noqa: E402

for client in (Legacy, ClobClient):
    for method in (
        "create_order",
        "post_order",
        "cancel",
        "cancel_all",
        "cancel_order",
        "cancel_orders",
        "create_market_order",
        "update_balance_allowance",
    ):
        setattr(client, method, forbidden)


@app.post("/__test/maker/reset")
async def maker_reset():
    await reset()
    with sessions() as db:
        config = MarketMakerConfig(
            id=1,
            user_id=1,
            account_id=1,
            condition_id=CID,
            token_id_yes="11",
            token_id_no="22",
            market_title="Lifecycle fixture",
        )
        db.add(config)
        paper.configure_account(db, 1, 1, funding=1000, fee_bps=100, acknowledge_legacy=True)
        db.get(PaperAccount, 1).started_at = NOW
        db.commit()
        for _ in range(100):
            runtime.sync(db, config, QUOTES, NOW)
        db.commit()
    return {"cycles": 100}


@app.post("/__test/maker/fill")
def maker_fill():
    clock[0] = NOW + timedelta(seconds=3)
    with sessions() as db, paper.transaction(db):
        state = paper.account_state(db, 1, 1)
        ledger = paper.restore(db, state)
        event = FlowEvent(
            id="controlled-flow",
            token_id="11",
            condition_id=CID,
            occurred_at=NOW + timedelta(seconds=1),
            available_at=NOW + timedelta(seconds=2),
            price=Decimal("0.40"),
            shares=Decimal("8"),
            aggressor="SELL",
            continuous=True,
            evidence_snapshot_id="synthetic-attributed-flow",
        )
        runtime.consume(db, state, ledger, [event], clock[0])
        db.commit()
        return {"cash": str(ledger.cash), "shares": str(ledger.positions["11"].shares)}


@app.post("/__test/maker/cancel")
def maker_cancel():
    with sessions() as db:
        runtime.cancel_config(db, db.get(MarketMakerConfig, 1), NOW + timedelta(seconds=4))
        db.commit()
    return {"cancelled": True}
