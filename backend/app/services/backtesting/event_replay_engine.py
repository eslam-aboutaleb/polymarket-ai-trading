"""Deterministic priority-queue event replay.

Guarantees:
* events become visible to a strategy only at ``available_at``;
* orders arrive after decision + submission latency (+ seeded jitter) and
  match against the book visible at that instant, never the decision book;
* the same events, config, candidate parameters and seed produce a
  byte-identical report (``result_hash``);
* the cash/position ledger is the only P&L source. A leader's ``source_pnl`` is
  never read.
"""

import hashlib
import heapq
import json
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from app.services.backtesting.contracts import (
    ENGINE_VERSION,
    CancelIntent,
    EngineConfig,
    OrderIntent,
    ReplayEvent,
)
from app.services.backtesting.simulated_exchange import OrderState, SimulatedExchange

ZERO = Decimal(0)
# Priority at an identical instant: market observations, then order arrivals,
# then cancels, then expiries.
_EXTERNAL, _ARRIVAL, _CANCEL, _EXPIRE = 0, 1, 2, 3


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class DecisionContext:
    now: datetime
    event: ReplayEvent
    visible_events: tuple[ReplayEvent, ...]
    books: dict[str, ReplayEvent]
    position_shares: Callable[[str], Decimal]
    available_cash: Decimal


Decider = Callable[[DecisionContext], list[OrderIntent | CancelIntent]]


def events_snapshot_id(events: list[ReplayEvent]) -> str:
    ordered = sorted(events, key=lambda e: (e.available_at, e.sequence, e.event_id))
    return "snap-" + digest([e.model_dump(mode="json") for e in ordered])[:32]


def _dedupe(events: list[ReplayEvent]) -> list[ReplayEvent]:
    seen: dict[str, ReplayEvent] = {}
    for event in events:
        previous = seen.get(event.event_id)
        if previous is not None and previous != event:
            raise ValueError(f"conflicting content for event {event.event_id}")
        seen[event.event_id] = event
    return sorted(seen.values(), key=lambda e: (e.available_at, e.sequence, e.event_id))


def replay(
    events: list[ReplayEvent],
    decider: Decider,
    config: EngineConfig,
    *,
    strategy_version: str,
    parameters: dict | None = None,
    benchmark_id: str | None = None,
    mode: str = "simulated_replay",
) -> dict:
    ordered = _dedupe(events)
    exchange = SimulatedExchange(config)
    rng = random.Random(config.seed)  # noqa: S311 - seeded research jitter, not security
    heap: list[tuple] = []
    counter = 0
    for event in ordered:
        heapq.heappush(heap, (event.available_at, _EXTERNAL, counter, "event", event))
        counter += 1
    visible: list[ReplayEvent] = []
    rejected_cancels: list[dict] = []
    equity: list[Decimal] = []
    last_time = ordered[0].available_at if ordered else None

    def schedule(when, priority, kind, payload):
        nonlocal counter
        heapq.heappush(heap, (when, priority, counter, kind, payload))
        counter += 1

    def submit(now: datetime, item: OrderIntent | CancelIntent) -> None:
        if isinstance(item, OrderIntent):
            if item.intent_id in exchange.orders:
                raise ValueError(f"duplicate intent id {item.intent_id}")
            jitter = rng.randint(0, config.latency_jitter_ms) if config.latency_jitter_ms else 0
            delay = config.decision_latency_ms + config.submission_latency_ms + jitter
            arrival = now + timedelta(milliseconds=delay)
            state = OrderState(item, now, arrival)
            exchange.orders[item.intent_id] = state
            schedule(arrival, _ARRIVAL, "arrive", item.intent_id)
        else:
            delay = config.submission_latency_ms + item.cancel_after_ms
            schedule(now + timedelta(milliseconds=delay), _CANCEL, "cancel", item.intent_id)

    while heap:
        when, _, _, kind, payload = heapq.heappop(heap)
        last_time = when
        if kind == "event":
            event = payload
            visible.append(event)
            if event.kind == "BOOK":
                exchange.apply_book(event)
            elif event.kind == "TRADE":
                exchange.apply_trade(event, when)
            elif event.kind == "SETTLEMENT":
                exchange.settle(event)
            if event.kind in ("BOOK", "SIGNAL"):
                context = DecisionContext(
                    now=when,
                    event=event,
                    visible_events=tuple(visible),
                    books=dict(exchange.books),
                    position_shares=lambda token: exchange.ledger.available_shares(token),
                    available_cash=exchange.ledger.available_cash,
                )
                for item in decider(context) or []:
                    submit(when, item)
            marks_ready = exchange.valuation().get("nav")
            if marks_ready is not None:
                equity.append(Decimal(marks_ready))
        elif kind == "arrive":
            order = exchange.orders[payload]
            if not order.final:
                exchange.arrive(order, when)
                if order.intent.expire_after_ms is not None and not order.final:
                    schedule(
                        when + timedelta(milliseconds=order.intent.expire_after_ms),
                        _EXPIRE,
                        "expire",
                        payload,
                    )
        elif kind == "cancel":
            outcome = exchange.cancel(payload)
            if outcome["result"].startswith("rejected"):
                rejected_cancels.append({**outcome, "at": when.isoformat()})
        elif kind == "expire":
            exchange.expire(payload)

    return _report(
        exchange,
        ordered,
        config,
        strategy_version=strategy_version,
        parameters=parameters or {},
        benchmark_id=benchmark_id,
        mode=mode,
        rejected_cancels=rejected_cancels,
        equity=equity,
        finished_at=last_time,
    )


def _drawdown(equity: list[Decimal]) -> str | None:
    if len(equity) < 2:
        return None
    peak, worst = equity[0], ZERO
    for value in equity:
        peak = max(peak, value)
        worst = max(worst, peak - value)
    return str(worst)


def _report(
    exchange,
    events,
    config,
    *,
    strategy_version,
    parameters,
    benchmark_id,
    mode,
    rejected_cancels,
    equity,
    finished_at,
) -> dict:
    ledger = exchange.ledger
    valuation = exchange.valuation()
    fills = exchange.fills

    # Independent cash identity from the fill list, not from the ledger.
    expected_cash = config.funding + exchange.settlement_cash
    for fill in fills:
        notional = Decimal(fill["price"]) * Decimal(fill["shares"])
        bps = exchange.costs.ledger_bps(fill["liquidity"])
        fee = notional * bps / 10000
        expected_cash += (-notional - fee) if fill["side"] == "BUY" else (notional - fee)
    basis = sum((p.cost_basis for p in ledger.positions.values()), ZERO)
    residual = ledger.cash - expected_cash
    identity = ledger.cash + basis - config.funding - ledger.realized_pnl
    conservation = {
        "cash_matches_fill_replay": residual == 0,
        "cash_residual": str(residual),
        "cash_plus_cost_basis_equals_funding_plus_realized": identity == 0,
        "identity_residual": str(identity),
    }
    conservation["holds"] = all(v for k, v in conservation.items() if isinstance(v, bool))

    fees_unknown = exchange.unknown_fee_fills > 0
    nav = valuation["nav"]
    fees = ledger.fees
    uncertainty = []
    if exchange.approximate_fills:
        uncertainty.append("approximation_no_depth: fills assumed from price-only book data")
    if fees_unknown:
        uncertainty.append("fees_unknown: fee rate not supplied, net P&L withheld")
    if valuation["missing_marks"]:
        uncertainty.append("marks_missing: open inventory has no two-sided book, NAV unknown")
    maker_uncertain = [
        {"order_id": o.intent.intent_id, **u} for o in exchange.orders.values() for u in o.uncertain
    ]
    if maker_uncertain:
        uncertainty.append("maker_fill_uncertain_no_queue_evidence")
    if exchange.unconfirmed:
        uncertainty.append("settlement_unconfirmed: provisional settlement never credited")
    if exchange.disputed - set(exchange.settled):
        uncertainty.append("settlement_disputed: open dispute at end of run")
    if not fills:
        uncertainty.append("no_fills")
    quality = (
        "approximate"
        if uncertainty
        and any(
            u.startswith(("approximation", "fees_unknown", "maker_fill", "marks_missing"))
            for u in uncertainty
        )
        else "modeled_displayed_depth"
    )

    per_token: dict[str, Decimal] = {}
    for fill in fills:
        notional = Decimal(fill["price"]) * Decimal(fill["shares"])
        fee = notional * exchange.costs.ledger_bps(fill["liquidity"]) / 10000
        delta = (-notional - fee) if fill["side"] == "BUY" else (notional - fee)
        per_token[fill["token_id"]] = per_token.get(fill["token_id"], ZERO) + delta
    for token, info in exchange.settled.items():
        if Decimal(info["shares"]) == 0 and token not in per_token:
            continue  # settled a market we never held: no attributable P&L
        per_token[token] = per_token.get(token, ZERO) + Decimal(info["cash_credited"])
    for token, position in ledger.positions.items():
        if position.shares and token not in exchange.settled:
            mark = exchange.mark(token)
            per_token[token] = per_token.get(token, ZERO) + (
                position.shares * mark if mark is not None else ZERO
            )
    net = None if (nav is None or fees_unknown) else Decimal(nav) - config.funding
    gross = None if nav is None else Decimal(nav) - config.funding + fees
    body = {
        "engine_version": ENGINE_VERSION,
        "mode": mode,
        "result_class": "simulated_not_exchange_confirmed",
        "run": {
            "strategy_version": strategy_version,
            "data_snapshot_id": events_snapshot_id(events),
            "parameters_hash": digest(
                {"config": config.model_dump(mode="json"), "parameters": parameters}
            ),
            "fee_schedule_id": config.fee_schedule_id,
            "seed": config.seed,
            "benchmark_id": benchmark_id,
            "quality_status": quality,
            "event_count": len(events),
            "data_window": {
                "first_available_at": events[0].available_at.isoformat() if events else None,
                "last_available_at": events[-1].available_at.isoformat() if events else None,
                "finished_at": finished_at.isoformat() if finished_at else None,
            },
        },
        "orders": [o.view() for o in exchange.orders.values()],
        "fills": fills,
        "rejected_cancels": rejected_cancels,
        "settlements": exchange.settled,
        "uncertain_maker_fills": maker_uncertain,
        "ledger": valuation,
        "conservation": conservation,
        "pnl": {
            "funding": str(config.funding),
            "gross_pnl": None if gross is None else str(gross),
            "net_pnl": None if net is None else str(net),
            "fees": None if fees_unknown else str(fees),
            "fee_status": "unknown" if fees_unknown else ("modeled" if fills else "none_incurred"),
            "nav": nav,
            "realized_pnl": str(ledger.realized_pnl),
        },
        "per_token_net": {k: str(v) for k, v in sorted(per_token.items())},
        "downside": {"max_drawdown_usdc": _drawdown(equity), "equity_samples": len(equity)},
        "uncertainty": uncertainty,
    }
    body["result_hash"] = digest(body)
    return body
