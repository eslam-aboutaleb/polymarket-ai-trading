"""Journal-based owner/account limits inside the serialized paper transaction."""

import json
from decimal import Decimal

from app.models.trading.paper_account import PaperJournalEvent, PaperNavPoint
from app.services.analytics.paper_accounting import apply_journal_event, restore, value_account
from app.services.analytics.replay.ledger import PaperLedger
from app.services.market_data.replay_capture import aware


def negative_realized(db, state, day, month):
    ledger = PaperLedger(Decimal(state.funding))
    daily = monthly = Decimal(0)
    for row in (
        db.query(PaperJournalEvent)
        .filter_by(account_id=state.account_id)
        .order_by(PaperJournalEvent.id)
        .yield_per(500)
    ):
        before = ledger.realized_pnl
        apply_journal_event(ledger, row)
        loss = max(Decimal(0), before - ledger.realized_pnl)
        if aware(row.observed_at) >= month:
            monthly += loss
        if aware(row.observed_at) >= day:
            daily += loss
    return daily, monthly


def check(db, states, settings, amount, moment, strategy_limit=None):
    exposure = funding = net = peak_profit = cash = daily = monthly = Decimal(0)
    day = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    month = day.replace(day=1)
    for state in states:
        ledger = restore(db, state)
        value = value_account(db, state, moment)
        if value["nav"] is None:
            return "PAPER_RISK_VALUATION_UNAVAILABLE"
        exposure += sum((position.cost_basis for position in ledger.positions.values()), Decimal(0))
        exposure += ledger.reserved_cash
        cash += ledger.available_cash
        funding += ledger.funding
        net += Decimal(value["net_pnl"])
        day_loss, month_loss = negative_realized(db, state, day, month)
        daily += day_loss
        monthly += month_loss
        peak = Decimal(0)
        for row in db.query(PaperNavPoint).filter_by(account_id=state.account_id).yield_per(500):
            point = json.loads(row.valuation_json)
            if point.get("net_pnl") is not None:
                peak = max(peak, Decimal(point["net_pnl"]))
        peak_profit += peak
    max_position = Decimal(str(settings.max_position_size or 0))
    if max_position > 0 and exposure + amount > max_position:
        return "PAPER_POSITION_LIMIT"
    for loss, limit in (
        (daily, settings.daily_loss_limit),
        (monthly, settings.monthly_loss_limit),
        (daily, strategy_limit),
    ):
        if limit is not None and limit > 0 and loss + amount > Decimal(str(limit)):
            return "PAPER_LOSS_LIMIT"
    # Flow-adjusted P&L excludes deposits/withdrawals. Summed individual peaks
    # conservatively bound the owner peak when account sampling is asynchronous.
    total_limit = Decimal(str(settings.total_loss_halt_pct or 0))
    if funding > 0 and total_limit > 0 and max(Decimal(0), -net) * 100 >= funding * total_limit:
        return "PAPER_TOTAL_LOSS_LIMIT"
    max_drawdown = Decimal(str(settings.max_drawdown_pct or 0))
    peak = funding + peak_profit
    if peak > 0 and max_drawdown > 0 and (peak_profit - net) * 100 >= peak * max_drawdown:
        return "PAPER_DRAWDOWN_LIMIT"
    if cash < amount:
        return "PAPER_CASH_LIMIT"
    return None
