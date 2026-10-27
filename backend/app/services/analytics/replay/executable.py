"""Deterministic IOC research replay using #18 books and #27 exact accounting.

No signer, wallet, live gateway or network client belongs in this module.
"""

from datetime import timedelta
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal, localcontext

from app.services.analytics.replay.executable_contracts import ReplayDefinition, ReplaySignal
from app.services.analytics.replay.ledger import ACCOUNTING_CONTEXT, PaperLedger
from app.services.analytics.replay.nav_statistics import family_statistics, periodic_statistics
from app.services.market_data.binary_book import Level, WalkRequest, reconstruct, walk_depth
from app.services.market_data.replay_capture import digest

VERSION = "executable-replay-v1"


def _book(definition, token, condition, moment, trial):
    specs = [
        s for s in definition.tape.specs if s.condition_id == condition and s.received_at <= moment
    ]
    if not specs:
        return None, None
    newest = max(s.received_at for s in specs)
    peers = [s for s in specs if s.received_at == newest]
    if (
        len(
            {
                (
                    s.rule_hash,
                    s.active,
                    s.tick,
                    s.minimum_shares,
                    s.yes_token,
                    s.no_token,
                    s.supported,
                )
                for s in peers
            }
        )
        > 1
    ):
        return None, None
    spec = max(specs, key=lambda s: (s.received_at, s.metadata_id)).model_copy(
        update={"lot": trial.lot_size}
    )
    if token not in (spec.yes_token, spec.no_token):
        return spec, None
    events = [
        e for e in definition.tape.events if e.condition_id == condition and e.token_id == token
    ]
    return spec, reconstruct(spec, token, events, moment, trial.max_book_age_seconds)


def _signals(definition, trial):
    if trial.strategy == "follower_signals":
        return list({s.id: s for s in definition.tape.signals}.values())
    signals, history, stance = [], {}, {}
    # Only completed available book states enter the indicator. A signal still
    # cannot execute on its own state's receipt, even with zero latency.
    seen = set()
    for event in sorted(definition.tape.events, key=lambda e: (e.received_at, e.snapshot_id)):
        if event.snapshot_id in seen or event.kind not in {"snapshot", "delta"}:
            continue
        seen.add(event.snapshot_id)
        if not definition.start_at <= event.received_at <= definition.end_at:
            continue
        _, book = _book(definition, event.token_id, event.condition_id, event.received_at, trial)
        if book is None or book.grade != "VERIFIED_BOOK":
            continue
        values = history.setdefault((event.condition_id, event.token_id), [])
        values.append((book.bids[0].price + book.asks[0].price) / 2)
        if len(values) < trial.slow_period:
            continue
        current = (
            sum(values[-trial.fast_period :]) / trial.fast_period
            > sum(values[-trial.slow_period :]) / trial.slow_period
        )
        key = (event.condition_id, event.token_id)
        if key in stance and stance[key] != current:
            signals.append(
                ReplaySignal(
                    id=f"sma:{event.snapshot_id}",
                    condition_id=event.condition_id,
                    token_id=event.token_id,
                    side="BUY" if current else "SELL",
                    available_at=event.received_at,
                    source_at=event.source_at,
                )
            )
        stance[key] = current
    return signals


def _adjusted(book, trial, used, tick):
    grouped, allocation = {"BUY": {}, "SELL": {}}, {}
    midpoint = (book.bids[0].price + book.asks[0].price) / 2
    for side, levels in (("BUY", book.asks), ("SELL", book.bids)):
        for level in levels:
            key = (book.condition_id, book.token_id, book.rule_hash, side, level.price)
            remaining = max(Decimal(0), level.shares - used.get(key, Decimal(0)))
            price = level.price * (
                1 + trial.slippage_bps / 10000 if side == "BUY" else 1 - trial.slippage_bps / 10000
            )
            # Tick rounding always makes the stress adverse, never favorable.
            price = (price / tick).to_integral_value(
                rounding=ROUND_CEILING if side == "BUY" else ROUND_DOWN
            ) * tick
            if not 0 < price < 1 or remaining <= 0:
                continue
            grouped[side][price] = grouped[side].get(price, Decimal(0)) + remaining
            allocation.setdefault((side, price), []).append([key, level.price, remaining])
    updated = book.model_copy(
        update={
            "asks": [Level(price=p, shares=q) for p, q in sorted(grouped["BUY"].items())],
            "bids": [
                Level(price=p, shares=q) for p, q in sorted(grouped["SELL"].items(), reverse=True)
            ],
        }
    )
    return updated, allocation, midpoint


def _execute(definition, trial, ledger, signal, event, when, used, rules, frozen):
    row = {
        "signal_id": signal.id,
        "condition_id": signal.condition_id,
        "token_id": signal.token_id,
        "side": signal.side,
        "decision_at": signal.available_at.isoformat(),
        "eligible_at": (
            signal.available_at + timedelta(milliseconds=trial.decision_latency_ms)
        ).isoformat(),
        "status": "unfilled",
        "shares": "0",
        "notional_usdc": "0",
        "fee_usdc": "0",
        "slippage_usdc": "0",
        "spread_cost_usdc": "0",
        "fills": [],
        "actual_execution": False,
    }
    if event is None:
        eligible = signal.available_at + timedelta(milliseconds=trial.decision_latency_ms)
        expired = when >= eligible + timedelta(seconds=trial.expiry_seconds)
        return {
            **row,
            "reason": "EXPIRED_MISSING_NEXT_BOOK"
            if expired
            else "NO_ELIGIBLE_BOOK_BEFORE_WINDOW_END",
            "remainder": "expired" if expired else "unfilled_at_window_end",
        }
    spec, book = _book(definition, signal.token_id, signal.condition_id, when, trial)
    if spec is None or book is None or book.grade != "VERIFIED_BOOK":
        return {**row, "reason": "MISSING_OR_INVALID_ASOF_BOOK"}
    row.update(
        snapshot_ids=book.source_ids,
        metadata_id=spec.metadata_id,
        rule_hash=spec.rule_hash,
        book_hash=book.book_hash,
        available_at=when.isoformat(),
    )
    eligible = signal.available_at + timedelta(milliseconds=trial.decision_latency_ms)
    if (
        event.source_at is None
        or event.source_at <= signal.available_at
        or event.source_at < eligible
    ):
        return {**row, "reason": "NO_POST_DECISION_SOURCE_STATE"}
    if any(
        reason in {"SETTLEMENT_FINAL_CONFLICT", "SETTLEMENT_IDENTITY_CONFLICT"}
        for reason in frozen.values()
    ):
        return {**row, "reason": "SETTLEMENT_RECONCILIATION_REQUIRED"}
    if signal.token_id in frozen or signal.token_id in ledger.settled_tokens:
        return {**row, "reason": "SETTLEMENT_UNCERTAIN_OR_FINAL"}
    if (
        signal.token_id in rules
        and rules[signal.token_id] != spec.rule_hash
        and ledger.available_shares(signal.token_id)
    ):
        frozen[signal.token_id] = "RULE_VERSION_CONFLICT"
        return {**row, "reason": "RULE_VERSION_CONFLICT"}
    if trial.fee_bps is None or not trial.fee_version:
        return {**row, "reason": "FEE_POLICY_UNKNOWN"}
    # Pass tick explicitly; no modification of the frozen trial contract.
    adjusted, allocation, midpoint = _adjusted(book, trial, used, spec.tick)
    constrained = False
    if signal.side == "SELL":
        inventory = ledger.available_shares(signal.token_id)
        amount = signal.amount or inventory
        units = signal.units if signal.amount else "SHARES"
        if units == "SHARES":
            constrained = amount > inventory
            amount = min(amount, inventory)
        remaining_inventory, bids = inventory, []
        for level in adjusted.bids:
            take = min(level.shares, remaining_inventory)
            if take:
                bids.append(Level(price=level.price, shares=take))
                remaining_inventory -= take
        adjusted = adjusted.model_copy(update={"bids": bids})
    else:
        amount = signal.amount or trial.order_notional
        units = signal.units
        if units == "USDC":
            constrained = amount > ledger.available_cash
            amount = min(amount, ledger.available_cash)
    if amount <= 0:
        return {**row, "reason": "INSUFFICIENT_CASH_OR_INVENTORY"}
    if signal.side == "BUY" and units == "SHARES":
        if ledger.available_cash <= 0:
            return {**row, "reason": "INSUFFICIENT_CASH"}
        affordable = walk_depth(
            spec,
            adjusted,
            WalkRequest(
                side="BUY",
                units="USDC",
                amount=min(ledger.available_cash, Decimal("1000000000")),
                fee_bps=trial.fee_bps,
                fee_version=trial.fee_version,
                limit_price=signal.limit_price,
            ),
        )
        if affordable.status != "modeled":
            return {**row, "reason": "INSUFFICIENT_CASH_FOR_MINIMUM_ORDER"}
        constrained = amount > affordable.matched_shares
        amount = min(amount, affordable.matched_shares)
    estimate = walk_depth(
        spec,
        adjusted,
        WalkRequest(
            side=signal.side,
            units=units,
            amount=amount,
            fee_bps=trial.fee_bps,
            fee_version=trial.fee_version,
            limit_price=signal.limit_price,
        ),
    )
    if estimate.status != "modeled":
        return {**row, "reason": estimate.reason}
    quantity = gross = fees = slippage = spread = Decimal(0)
    for index, point in enumerate(estimate.curve):
        take = point.shares
        if signal.side == "BUY":
            affordable = ledger.available_cash / (point.price * (1 + trial.fee_bps / 10000))
            take = min(
                take, (affordable / spec.lot).to_integral_value(rounding=ROUND_DOWN) * spec.lot
            )
        if take <= 0:
            continue
        order_id = f"{trial.id}:{signal.id}:{index}"
        ledger.reserve(order_id, signal.token_id, signal.side, take, point.price, trial.fee_bps)
        ledger.fill(f"fill:{order_id}", order_id, take, point.price)
        left = take
        for key, native_price, capacity in allocation[(signal.side, point.price)]:
            consumed = min(left, capacity)
            used[key] = used.get(key, Decimal(0)) + consumed
            slippage += consumed * abs(point.price - native_price)
            spread += consumed * abs(native_price - midpoint)
            left -= consumed
            if left <= 0:
                break
        quantity += take
        gross += take * point.price
        fee = take * point.price * trial.fee_bps / 10000
        fees += fee
        row["fills"].append({"shares": str(take), "price": str(point.price), "fee_usdc": str(fee)})
    if quantity == 0:
        return {**row, "reason": "INSUFFICIENT_CASH_OR_INVENTORY"}
    rules[signal.token_id] = spec.rule_hash
    partial = (
        constrained
        or estimate.partial
        or quantity < estimate.matched_shares
        or (signal.amount is not None and signal.units == "SHARES" and quantity < signal.amount)
    )
    return {
        **row,
        "status": "partial" if partial else "filled",
        "reason": "MODELED_IOC_DISPLAYED_DEPTH",
        "remainder": "cancelled" if partial else "none",
        "shares": str(quantity),
        "notional_usdc": str(gross),
        "fee_usdc": str(fees),
        "slippage_usdc": str(slippage),
        "spread_cost_usdc": str(spread),
        "vwap": str(gross / quantity) if quantity else None,
    }


def _scenario(definition, trial, end_at, signals_override=None):
    ledger = PaperLedger(definition.funding)
    outcomes, samples, settlement_log = [], [], []
    used, rules, frozen, settled = {}, {}, {}, {}
    timeline = []
    for signal in _signals(definition, trial) if signals_override is None else signals_override:
        if not definition.start_at <= signal.available_at <= end_at:
            continue
        eligible = signal.available_at + timedelta(milliseconds=trial.decision_latency_ms)
        candidates = [
            event
            for event in definition.tape.events
            if event.token_id == signal.token_id
            and event.condition_id == signal.condition_id
            and event.kind != "trade"
            and event.received_at > signal.available_at
            and eligible
            <= event.received_at
            <= min(end_at, eligible + timedelta(seconds=trial.expiry_seconds))
        ]
        event = (
            min(candidates, key=lambda e: (e.received_at, e.snapshot_id)) if candidates else None
        )
        when = (
            event.received_at
            if event
            else min(end_at, eligible + timedelta(seconds=trial.expiry_seconds))
        )
        timeline.append((when, 1, signal.id, (signal, event)))
    for event in {s.id: s for s in definition.tape.settlements}.values():
        if event.available_at <= end_at:
            timeline.append((event.available_at, 0, event.id, event))
    moment = definition.start_at
    while moment <= end_at:
        timeline.append((moment, 2, moment.isoformat(), None))
        moment += timedelta(seconds=definition.sample_seconds)
    lockup = Decimal(0)
    previous = definition.start_at
    last_value = None
    for when, kind, _, item in sorted(timeline, key=lambda e: e[:3]):
        if when >= definition.start_at:
            basis = sum((p.cost_basis for p in ledger.positions.values()), Decimal(0))
            lockup += basis * Decimal(
                str((when - max(previous, definition.start_at)).total_seconds())
            )
            previous = when
        if kind == 0:
            event = item
            if event.status not in {"FINAL", "VOID"} or not event.confirmed:
                frozen[event.token_id] = (
                    "SETTLEMENT_FINAL_CONFLICT" if event.token_id in settled else event.status
                )
                settlement_log.append({"id": event.id, "status": event.status, "booked": False})
                continue
            if trial.settlement_fee_bps is None or not trial.fee_version:
                frozen[event.token_id] = "SETTLEMENT_FEE_UNKNOWN"
                continue
            spec, _ = _book(
                definition, event.token_id, event.condition_id, event.available_at, trial
            )
            if (
                spec is None
                or not spec.supported
                or spec.rule_hash != event.rule_hash
                or event.token_id not in (spec.yes_token, spec.no_token)
                or (event.token_id in rules and rules[event.token_id] != event.rule_hash)
            ):
                frozen[event.token_id] = "SETTLEMENT_IDENTITY_CONFLICT"
                continue
            if event.token_id in ledger.settled_tokens:
                if settled[event.token_id] != (event.condition_id, event.rule_hash, event.payout):
                    frozen[event.token_id] = "SETTLEMENT_FINAL_CONFLICT"
                settlement_log.append(
                    {"id": event.id, "status": "REPEATED_FINAL_UNVERIFIED", "booked": False}
                )
                continue
            ledger.settle(
                f"settle:{event.id}", event.token_id, event.payout, trial.settlement_fee_bps
            )
            settled[event.token_id] = (event.condition_id, event.rule_hash, event.payout)
            frozen.pop(event.token_id, None)
            settlement_log.append(
                {
                    "id": event.id,
                    "status": event.status,
                    "payout": str(event.payout),
                    "booked": True,
                    "source_ids": event.source_ids,
                }
            )
        elif kind == 1:
            signal, event = item
            outcomes.append(
                _execute(definition, trial, ledger, signal, event, when, used, rules, frozen)
            )
        else:
            value = _valuation(definition, trial, ledger, when, rules, frozen)
            samples.append(
                {
                    "at": when.isoformat(),
                    "epoch": when.timestamp(),
                    "nav": value["nav"],
                    "cash": value["cash"],
                    "position_value": value["position_value"],
                    "reasons": dict(frozen),
                }
            )
            last_value = value
    if last_value is None or samples[-1]["at"] != end_at.isoformat():
        last_value = _valuation(definition, trial, ledger, end_at, rules, frozen)
    intervals = definition.annualization_periods or 31_536_000 / definition.sample_seconds
    stats = periodic_statistics(samples, intervals, definition.sample_seconds)
    splits = {}
    for name, low, high in (
        ("train", definition.start_at, definition.train_end),
        ("validation", definition.train_end, definition.validation_end),
        ("test", definition.validation_end, end_at),
    ):
        points = [p for p in samples if low.timestamp() <= p["epoch"] <= high.timestamp()]
        splits[name] = periodic_statistics(points, intervals, definition.sample_seconds)
        start_nav = Decimal(points[0]["nav"]) if points and points[0]["nav"] is not None else None
        end_nav = Decimal(points[-1]["nav"]) if points and points[-1]["nav"] is not None else None
        valid = (
            len(points) >= 2
            and start_nav is not None
            and start_nav > 0
            and end_nav is not None
            and all(p["nav"] is not None for p in points)
        )
        splits[name].update(
            start_nav=str(start_nav) if start_nav is not None else None,
            end_nav=str(end_nav) if end_nav is not None else None,
            net_pnl_change=str(end_nav - start_nav) if valid else None,
            total_return=str(end_nav / start_nav - 1) if valid else None,
        )
    slippage = sum((Decimal(row["slippage_usdc"]) for row in outcomes), Decimal(0))
    spread = sum((Decimal(row["spread_cost_usdc"]) for row in outcomes), Decimal(0))
    net = Decimal(last_value["net_pnl"]) if last_value["net_pnl"] is not None else None
    peak, drawdown = definition.funding, Decimal(0)
    complete = all(point["nav"] is not None for point in samples)
    for point in samples:
        if point["nav"] is not None:
            nav = Decimal(point["nav"])
            peak = max(peak, nav)
            drawdown = max(drawdown, (peak - nav) / peak if peak else Decimal(0))
    return {
        "max_drawdown": str(drawdown) if complete else None,
        "trial_id": trial.id,
        "strategy_version": trial.strategy_version,
        "valuation": last_value,
        "gross_pnl_on_matched_quantity": str(net + ledger.fees + slippage)
        if net is not None
        else None,
        "net_pnl": str(net) if net is not None else None,
        "fees_usdc": str(ledger.fees),
        "slippage_usdc": str(slippage),
        "spread_cost_usdc": str(spread),
        "displayed_capacity_shares": str(sum((Decimal(o["shares"]) for o in outcomes), Decimal(0))),
        "cash_lockup_usdc_seconds": str(lockup),
        "trade_count": sum(o["status"] in {"filled", "partial"} for o in outcomes),
        "outcomes": outcomes,
        "settlements": settlement_log,
        "ledger": ledger.journal,
        "nav": samples,
        "statistics": stats,
        "splits": splits,
        "no_trade_baseline": {"nav": str(definition.funding), "net_pnl": "0"},
        "mark_policy": "fresh captured exit bid; indicative mark, not forced liquidation",
        "execution_eligible": False,
        "confidence": "counterfactual_displayed_depth_not_actual_fills",
    }


def _valuation(definition, trial, ledger, moment, rules, frozen):
    marks = {}
    for token, position in ledger.positions.items():
        if not position.shares or token in frozen:
            continue
        specs = [
            s
            for s in definition.tape.specs
            if token in (s.yes_token, s.no_token) and s.received_at <= moment
        ]
        if not specs:
            continue
        spec, book = _book(definition, token, specs[-1].condition_id, moment, trial)
        if spec and book and book.grade == "VERIFIED_BOOK" and rules.get(token) == spec.rule_hash:
            marks[token] = book.bids[0].price
    value = ledger.value(marks)
    if any(
        reason in {"SETTLEMENT_FINAL_CONFLICT", "SETTLEMENT_IDENTITY_CONFLICT"}
        for reason in frozen.values()
    ):
        value.update(nav=None, net_pnl=None, position_value=None, unrealized_pnl=None)
        value["reconciliation_required"] = True
    return value


def replay(definition: ReplayDefinition):
    with localcontext(ACCOUNTING_CONTEXT):
        # Family evaluation ends at validation. Holdout outcomes cannot choose a
        # winner, and only the frozen validation winner receives a test replay.
        candidates = [
            _scenario(definition, trial, definition.validation_end) for trial in definition.trials
        ]
        scores = []
        for result in candidates:
            points = [
                p
                for p in result["nav"]
                if definition.train_end.timestamp()
                <= p["epoch"]
                <= definition.validation_end.timestamp()
            ]
            if (
                len(points) >= 2
                and all(p["nav"] is not None for p in points)
                and Decimal(points[0]["nav"]) > 0
            ):
                scores.append(Decimal(points[-1]["nav"]) / Decimal(points[0]["nav"]) - 1)
            else:
                scores.append(None)
        eligible = [i for i, score in enumerate(scores) if score is not None]
        winner = max(eligible, key=lambda i: (scores[i], -i)) if eligible else None
        selected = (
            _scenario(definition, definition.trials[winner], definition.end_at)
            if winner is not None
            else None
        )
        market_baseline = {
            "status": "unavailable",
            "reason": "NO_KNOWN_START_MARKET_OR_SELECTED_POLICY",
        }
        known = [
            s
            for s in definition.tape.specs
            if s.received_at <= definition.start_at and s.active and s.supported
        ]
        if winner is not None and known:
            spec = min(known, key=lambda s: (s.condition_id, s.yes_token))
            baseline_signal = ReplaySignal(
                id="market-baseline",
                condition_id=spec.condition_id,
                token_id=spec.yes_token,
                side="BUY",
                available_at=definition.start_at,
                source_at=definition.start_at,
            )
            value = _scenario(
                definition, definition.trials[winner], definition.end_at, [baseline_signal]
            )
            market_baseline = {
                "status": "modeled",
                "name": "known-at-start YES buy-and-hold, selected policy/order size",
                "valuation": value["valuation"],
                "statistics": value["statistics"],
                "outcomes": value["outcomes"],
            }
        # Missing/alignment gaps prohibit family diagnostics rather than dropping
        # inconvenient samples. The first trial does not become a default winner.
        series = [
            r["splits"]["validation"]["returns"]
            if r["splits"]["validation"]["reason"] != "MISSING_OR_NONPERIODIC_NAV"
            else []
            for r in candidates
        ]
        return {
            "version": VERSION,
            "input_hash": digest(definition.model_dump(mode="json")),
            "classification": "implemented_model_not_historically_validated",
            "execution_evidence": "imported_unverified_modeled",
            "execution_eligible": False,
            "trial_results": candidates,
            "selected_trial_id": definition.trials[winner].id if winner is not None else None,
            "selection_metric": "validation marked NAV return only; ties use frozen family order",
            "holdout": selected,
            "family_statistics": family_statistics(series, definition.declared_complete_family),
            "holdout_claim": "chronological declared holdout; not independently blinded",
            "market_baseline": market_baseline,
            "baseline": {"name": "no-trade cash", "nav": str(definition.funding), "net_pnl": "0"},
            "actual_fills": None,
            "money_path_changed": False,
        }
