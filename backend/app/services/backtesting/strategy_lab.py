"""Baselines, benchmark reports and side-by-side comparison for the lab."""

from decimal import Decimal

from app.services.backtesting.contracts import EngineConfig, OrderIntent, ReplayEvent
from app.services.backtesting.event_replay_engine import Decider, DecisionContext, digest, replay

BASELINE_VERSION = "lab-baselines-v1"


def no_trade(_context: DecisionContext) -> list:
    return []


def naive_copy(context: DecisionContext) -> list:
    """Copy every visible signal at the follower's own size."""
    event = context.event
    if event.kind != "SIGNAL":
        return []
    return [
        OrderIntent(
            intent_id=f"copy-{event.event_id}",
            token_id=event.token_id,
            side=event.side,
            amount=event.amount,
            units=event.units,
            limit_price=event.limit_price,
        )
    ]


def passive_holding(tokens: list[str], notional: Decimal) -> Decider:
    """Buy each manifest token once on its first visible book and hold it."""
    bought: set[str] = set()

    def decide(context: DecisionContext) -> list:
        event = context.event
        if event.kind != "BOOK" or event.token_id not in tokens or event.token_id in bought:
            return []
        if not event.asks:
            return []
        bought.add(event.token_id)
        return [
            OrderIntent(
                intent_id=f"hold-{event.token_id}",
                token_id=event.token_id,
                side="BUY",
                amount=notional,
                units="USDC",
            )
        ]

    return decide


def benchmark_id(tokens: list[str], notional: Decimal, config: EngineConfig) -> str:
    return (
        "bench-"
        + digest(
            {
                "version": BASELINE_VERSION,
                "tokens": sorted(tokens),
                "notional": str(notional),
                "config": config.model_dump(mode="json"),
            }
        )[:24]
    )


def run_baselines(
    events: list[ReplayEvent],
    config: EngineConfig,
    tokens: list[str],
    notional: Decimal = Decimal(50),
) -> dict[str, dict]:
    """The same engine, costs and data as the candidate, with three reference policies."""
    bid = benchmark_id(tokens, notional, config)
    naive_config = config.model_copy(
        update={"decision_latency_ms": 0, "submission_latency_ms": 0, "latency_jitter_ms": 0}
    )
    return {
        "no_trade": replay(
            events,
            no_trade,
            config,
            strategy_version="baseline-no-trade",
            benchmark_id=bid,
            mode="baseline",
        ),
        "passive_holding": replay(
            events,
            passive_holding(list(tokens), notional),
            config,
            strategy_version="baseline-passive-holding",
            benchmark_id=bid,
            mode="baseline",
        ),
        "naive_copy": replay(
            events,
            naive_copy,
            naive_config,
            strategy_version="baseline-naive-copy-zero-latency",
            benchmark_id=bid,
            mode="baseline",
        ),
    }


def _row(name: str, report: dict) -> dict:
    pnl = report["pnl"]
    return {
        "name": name,
        "mode": report["mode"],
        "gross_pnl": pnl["gross_pnl"],
        "net_pnl": pnl["net_pnl"],
        "fees": pnl["fees"],
        "fee_status": pnl["fee_status"],
        "max_drawdown_usdc": report["downside"]["max_drawdown_usdc"],
        "fills": len(report["fills"]),
        "quality_status": report["run"]["quality_status"],
        "uncertainty": report["uncertainty"],
        "result_class": report["result_class"],
        "result_hash": report["result_hash"],
        "benchmark_id": report["run"]["benchmark_id"],
    }


def compare(reports: dict[str, dict]) -> dict:
    """Rank only runs with known net P&L; unknown results stay unranked, never zero."""
    rows = [_row(name, report) for name, report in reports.items()]
    ranked = sorted(
        (r for r in rows if r["net_pnl"] is not None),
        key=lambda r: Decimal(r["net_pnl"]),
        reverse=True,
    )
    unranked = [r["name"] for r in rows if r["net_pnl"] is None]
    modes = sorted({r["mode"] for r in rows})
    return {
        "rows": rows,
        "ranking": [r["name"] for r in ranked],
        "unranked_unknown_net_pnl": unranked,
        "modes": modes,
        "comparable": len({r["benchmark_id"] for r in rows if r["benchmark_id"]}) <= 1,
        "claim": "modeled research comparison; not evidence of profitability or exchange execution",
    }


def benchmark_report(candidate: dict, baselines: dict[str, dict]) -> dict:
    """Candidate versus every baseline on identical data, costs and snapshot."""
    deltas = {}
    for name, base in baselines.items():
        if candidate["pnl"]["net_pnl"] is None or base["pnl"]["net_pnl"] is None:
            deltas[name] = None
        else:
            deltas[name] = str(
                Decimal(candidate["pnl"]["net_pnl"]) - Decimal(base["pnl"]["net_pnl"])
            )
    same_data = {candidate["run"]["data_snapshot_id"]} == {
        b["run"]["data_snapshot_id"] for b in baselines.values()
    }
    return {
        "version": BASELINE_VERSION,
        "same_data_snapshot": same_data,
        "net_pnl_minus_baseline": deltas,
        "comparison": compare({"candidate": candidate, **baselines}),
    }
