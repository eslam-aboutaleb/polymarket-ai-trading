"""Account-scoped event replay, shadow runs and strategy-lab comparison (#2).

Research only: nothing here can sign, submit or cancel an order. Every result
is stored on the existing BacktestRun record (compatibility boundary) as a
simulated, not exchange-confirmed, report.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import Field
from sqlalchemy.orm import Session

from app.api.routes.markets.microstructure import owned_account
from app.config import get_settings
from app.models.analytics.backtest_run import BacktestRun
from app.security.rate_limit import limiter
from app.services.backtesting.contracts import ENGINE_VERSION, EngineConfig, Frozen, ReplayEvent
from app.services.backtesting.event_replay_engine import replay
from app.services.backtesting.shadow_runner import run_shadow
from app.services.backtesting.strategies import StrategySpec, build_decider
from app.services.backtesting.strategy_lab import benchmark_report, compare, run_baselines
from app.utils.database import get_db
from app.utils.time import utc_now

EVENT_RUN = "event_replay"
SHADOW_RUN = "shadow_run"
MODE_WATERMARK = {
    "event_replay": "SIMULATED REPLAY. Not exchange-confirmed. Not a forecast of live results.",
    "shadow_run": "SHADOW. Decisions simulated on observed books. No order was sent or signed.",
}


def enabled():
    if not get_settings().event_replay_lab_enabled:
        raise HTTPException(503, "Event replay laboratory disabled")


router = APIRouter(
    prefix="/api/backtesting/lab",
    tags=["strategy laboratory"],
    dependencies=[Depends(enabled)],
)


class LabRequest(Frozen):
    name: str = Field("Unnamed experiment", min_length=1, max_length=200)
    events: Annotated[list[ReplayEvent], Field(min_length=1, max_length=2000)]
    config: EngineConfig
    strategy: StrategySpec
    include_baselines: bool = True
    # Manifest of tokens the passive-holding baseline may buy. Frozen up front.
    baseline_tokens: list[str] = Field(default_factory=list, max_length=100)
    baseline_notional_usdc: Annotated[float, Field(gt=0, le=1_000_000)] = 50.0


def _persist(db, account, run_type, body: LabRequest, report: dict, baselines: dict | None):
    pnl = report["pnl"]
    run = BacktestRun(
        user_id=account.owner_user_id,
        account_id=account.id,
        strategy_type=run_type,
        strategy_name=body.name,
        start_date=report["run"]["data_window"]["first_available_at"] or "",
        end_date=report["run"]["data_window"]["last_available_at"] or "",
        parameters={
            "engine_version": ENGINE_VERSION,
            "strategy": body.strategy.model_dump(mode="json"),
            "config": body.config.model_dump(mode="json"),
            "events": [e.model_dump(mode="json") for e in body.events],
            "baseline_tokens": body.baseline_tokens,
            "baseline_notional_usdc": body.baseline_notional_usdc,
            "include_baselines": body.include_baselines,
        },
        total_trades=len(report["fills"]),
        # Legacy numeric field only. net_pnl in the report is authoritative and
        # may be unknown; clients must read indicator_values.report.pnl.
        total_pnl=float(pnl["net_pnl"]) if pnl["net_pnl"] is not None else 0.0,
        status="completed",
        trade_log=report["fills"],
        indicator_values={
            "report": report,
            "baselines": baselines,
            "mode_watermark": MODE_WATERMARK[run_type],
            "net_pnl_quality": "unknown" if pnl["net_pnl"] is None else "modeled",
        },
        completed_at=utc_now(),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _present(run: BacktestRun) -> dict:
    stored = run.indicator_values or {}
    return {
        "id": run.id,
        "run_type": run.strategy_type,
        "name": run.strategy_name,
        "mode_watermark": stored.get("mode_watermark"),
        "report": stored.get("report"),
        "baselines": stored.get("baselines"),
        "benchmark": stored.get("benchmark"),
        "net_pnl_quality": stored.get("net_pnl_quality"),
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


def _owned(db: Session, account, run_id: int, run_type: str | None = None) -> BacktestRun:
    query = db.query(BacktestRun).filter(
        BacktestRun.id == run_id,
        BacktestRun.account_id == account.id,
        BacktestRun.user_id == account.owner_user_id,
        BacktestRun.strategy_type.in_([EVENT_RUN, SHADOW_RUN]),
    )
    if run_type:
        query = query.filter(BacktestRun.strategy_type == run_type)
    run = query.first()
    if run is None:
        raise HTTPException(404, "Run not found")
    return run


def _execute(body: LabRequest, run_type: str):
    decider = build_decider(body.strategy)
    version = body.strategy.version
    parameters = body.strategy.model_dump(mode="json")
    if run_type == SHADOW_RUN:
        report = run_shadow(body.events, decider, body.config, version, parameters)
    else:
        report = replay(
            body.events, decider, body.config, strategy_version=version, parameters=parameters
        )
    baselines = None
    if body.include_baselines and run_type == EVENT_RUN:
        from decimal import Decimal

        baselines = run_baselines(
            body.events,
            body.config,
            body.baseline_tokens,
            Decimal(str(body.baseline_notional_usdc)),
        )
    return report, baselines


def _create(request, body, account, db, run_type):
    try:
        report, baselines = _execute(body, run_type)
        run = _persist(db, account, run_type, body, report, baselines)
        if baselines:
            run.indicator_values = {
                **run.indicator_values,
                "benchmark": benchmark_report(report, baselines),
            }
            db.commit()
            db.refresh(run)
    except (ValueError, ArithmeticError) as exc:
        db.rollback()
        raise HTTPException(409, "Replay inputs rejected by the engine") from exc
    return _present(run)


@router.post("/event-replays")
@limiter.limit("6/minute")
def create_event_replay(
    request: Request,
    body: LabRequest,
    account=Depends(owned_account),
    db: Session = Depends(get_db),
):
    return _create(request, body, account, db, EVENT_RUN)


@router.post("/shadow-runs")
@limiter.limit("6/minute")
def create_shadow_run(
    request: Request,
    body: LabRequest,
    account=Depends(owned_account),
    db: Session = Depends(get_db),
):
    return _create(request, body, account, db, SHADOW_RUN)


@router.get("/runs")
def list_runs(
    limit: int = Query(20, ge=1, le=100),
    account=Depends(owned_account),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(BacktestRun)
        .filter(
            BacktestRun.account_id == account.id,
            BacktestRun.user_id == account.owner_user_id,
            BacktestRun.strategy_type.in_([EVENT_RUN, SHADOW_RUN]),
        )
        .order_by(BacktestRun.id.desc())
        .limit(limit)
        .all()
    )
    return {"runs": [_present(r) for r in rows]}


@router.get("/event-replays/{run_id}")
def get_event_replay(run_id: int, account=Depends(owned_account), db: Session = Depends(get_db)):
    return _present(_owned(db, account, run_id))


@router.post("/event-replays/{run_id}/verify")
@limiter.limit("6/minute")
def verify_event_replay(
    request: Request,
    run_id: int,
    account=Depends(owned_account),
    db: Session = Depends(get_db),
):
    """Re-run the frozen inputs and prove the stored result is reproducible."""
    run = _owned(db, account, run_id)
    stored = run.parameters or {}
    try:
        body = LabRequest(
            name=run.strategy_name,
            events=stored["events"],
            config=stored["config"],
            strategy=stored["strategy"],
            include_baselines=False,
        )
        report, _ = _execute(body, run.strategy_type)
    except (KeyError, ValueError, ArithmeticError) as exc:
        raise HTTPException(409, "Stored run could not be re-evaluated") from exc
    original = (run.indicator_values or {}).get("report") or {}
    return {
        "identical": report["result_hash"] == original.get("result_hash"),
        "stored_result_hash": original.get("result_hash"),
        "recomputed_result_hash": report["result_hash"],
    }


@router.get("/strategy-lab/compare")
def compare_runs(
    run_ids: Annotated[list[int], Query(min_length=2, max_length=10)],
    account=Depends(owned_account),
    db: Session = Depends(get_db),
):
    reports = {}
    for run_id in run_ids:
        run = _owned(db, account, run_id)
        reports[f"{run.strategy_type}:{run.id}:{run.strategy_name}"] = (run.indicator_values or {})[
            "report"
        ]
    return compare(reports)
