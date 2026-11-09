"""Real paper runner/API/journal browser harness with recorded public book fixtures."""

import json
from datetime import timedelta

from e2e_paper_app import app, clock, forbidden, reset, sessions
from latency_paper_fixtures import native
from paper_account_fixtures import CID
from test_latency_arb_service_coverage import _opportunity

from app.api.routes.trading.latency_arb import router
from app.models import UserSettings, UserTrade
from app.models.market_data.replay_capture import ReplayCapture
from app.models.trading.latency_arb_config import LatencyArbConfig
from app.models.trading.paper_account import PaperOrderRecord
from app.models.trading.paper_execution_intent import PaperExecutionIntent
from app.services.analytics import paper_accounting as paper
from app.services.trading import latency_arb_service as runner
from app.services.trading import latency_paper as runtime
from app.services.trading.execution import live

app.include_router(router)
runtime.utc_now = lambda: clock[0]
live.place_live_order = forbidden
runner.SessionLocal = sessions
scenario = ["full"]


async def capture(db, condition, **kwargs):
    clock[0] += timedelta(milliseconds=1)
    case = scenario[0]
    if case == "missing":
        return False
    options = {}
    if case == "partial":
        options["depth"] = "5"
    if case == "adverse":
        options.update(ask=".90", bid=".89")
    if case == "crossed":
        options["bid"] = ".50"
    if case == "stale":
        options["source_at"] = clock[0] - timedelta(seconds=100)
    if case == "resolved":
        options["final"] = "11"
    if case == "future":
        native(db, [clock[0] + timedelta(seconds=1)])
    else:
        native(db, clock, **options)
    return True


runtime.refresh_evidence = capture


def state(db, result):
    ledger = paper.restore(db, paper.account_state(db, 1, 1))
    return {
        "result": result,
        "cash": str(ledger.cash),
        "realized_pnl": str(ledger.realized_pnl),
        "trades": db.query(UserTrade).count(),
        "intents": db.query(PaperExecutionIntent).count(),
        "orders": db.query(PaperOrderRecord).count(),
    }


@app.post("/__test/latency-paper/{case}")
async def attempt(case: str):
    reset()
    scenario[0] = case
    with sessions() as db:
        db.query(ReplayCapture).delete()
        settings = db.query(UserSettings).filter_by(user_id=1).one()
        settings.simulation_mode = True
        paper.configure_account(db, 1, 1, fee_bps=1000 if case == "fees" else 0)
        if case == "fees":
            scenario[0] = "adverse"
        db.commit()
        db.add(
            LatencyArbConfig(
                user_id=1,
                account_id=1,
                enabled=True,
                max_notional=10,
                edge_threshold=0.03,
                alert_on_opportunity=False,
            )
        )
        db.commit()
    op = _opportunity(
        detected_at=clock[0] - timedelta(milliseconds=300), feed_lag_ms=0, p_model=0.9
    )
    op.market.condition_id = CID
    op.market.token_ids = {"up": "11", "down": "22"}
    op.market.prices = {"up": 0.01, "down": 0.99}
    await runner._execute_for_users([op], clock[0])
    with sessions() as db:
        row = db.query(PaperOrderRecord).one()
        return state(db, runtime.response(json.loads(row.result_json)))


@app.post("/__test/latency-paper-retry")
async def retry():
    clock[0] += timedelta(seconds=2)
    with sessions() as db:
        settings = db.query(UserSettings).filter_by(user_id=1).one()
        op = _opportunity(detected_at=clock[0], feed_lag_ms=0, p_model=0.9)
        op.market.condition_id = CID
        result = await runtime.execute(db, settings, op, "11", 10, 300, minimum_edge=0.03)
        return state(db, result)
