"""Equal-all-in-capital YES buy/hold control, using the existing paper ledger.

Counterfactual visible-depth execution and archived fee upper bounds never become
actual fills. Each control deployment has the same capital and decision time as
its paired observed entry. Reused native liquidity budgets are consumed once.
"""

from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from app.services.analytics.paper_evidence import (
    EvidenceUnavailable,
    current_book,
    final_payout,
    market_rows,
)
from app.services.analytics.replay.ledger import PaperLedger, accounting
from app.services.market_data.valuation_fees import archived_taker_fee


@accounting
def paired_control(db, fill, closing_at, capital, consumed):
    moment = datetime.fromisoformat(fill["decision_at"])
    native = current_book(db, fill["token_id"], moment)
    clob, gamma = market_rows(db, fill["condition_id"], moment, native.rule_hash)
    yes = [
        str(row["token_id"])
        for row in clob.get("tokens", [])
        if str(row.get("outcome", "")).upper() == "YES"
    ]
    if len(yes) != 1:
        raise EvidenceUnavailable("CONTROL_BINARY_YES_IDENTITY_UNAVAILABLE")
    quote = current_book(db, yes[0], moment)
    if quote.condition_id != fill["condition_id"] or quote.rule_hash != native.rule_hash:
        raise EvidenceUnavailable("CONTROL_CONDITION_RULE_MISMATCH")
    payout = final_payout(db, yes[0], fill["condition_id"], quote.rule_hash, closing_at)
    ledger = PaperLedger(capital)
    curve, fees, quantity = [], Decimal(0), Decimal(0)
    granularity = Decimal(".000001")
    budget_id = quote.book.liquidity_id or quote.capture_id
    for index, level in enumerate(quote.book.asks):
        key = (budget_id, yes[0], str(level.price))
        available = max(Decimal(0), level.shares - consumed.get(key, Decimal(0)))
        low, high = 0, int((available / granularity).to_integral_value(rounding=ROUND_DOWN))
        # Bounded integer search avoids optimistic continuous-size fee rounding.
        for _ in range(96):
            if low >= high:
                break
            middle = (low + high + 1) // 2
            take = granularity * middle
            candidate = curve + [{"price": str(level.price), "shares": str(take)}]
            total_fee, _ = archived_taker_fee({"clob": clob, "gamma": gamma}, candidate)
            required = (
                sum(Decimal(row["price"]) * Decimal(row["shares"]) for row in candidate) + total_fee
            )
            if required <= capital:
                low = middle
            else:
                high = middle - 1
        take = granularity * low
        if take <= 0:
            continue
        curve.append({"price": str(level.price), "shares": str(take)})
        total_fee, fee_version = archived_taker_fee({"clob": clob, "gamma": gamma}, curve)
        incremental_fee = total_fee - fees
        notional = take * level.price
        fee_bps = incremental_fee / notional * 10000
        ledger.reserve(f"control-{index}", yes[0], "BUY", take, level.price, fee_bps)
        ledger.fill(f"fill-{index}", f"control-{index}", take, level.price)
        fees = total_fee
        quantity += take
        consumed[key] = consumed.get(key, Decimal(0)) + take
    if quantity and quantity < quote.minimum_shares:
        raise EvidenceUnavailable("CONTROL_BELOW_NATIVE_MINIMUM_SIZE")
    if not quantity:
        # Even a zero-fill deployment must prove its fee schedule.
        _, fee_version = archived_taker_fee({"clob": clob, "gamma": gamma}, [])
    ledger.settle("control-payoff", yes[0], payout)
    value = ledger.value({})
    return {
        "version": "paired-equal-capital-yes-control-v1",
        "classification": "modeled_control_not_actual_fills",
        "capital": str(capital),
        "net_pnl": value["net_pnl"],
        "fees": str(ledger.fees),
        "shares": str(quantity),
        "unspent_cash": str(
            capital - sum(Decimal(row["price"]) * Decimal(row["shares"]) for row in curve) - fees
        ),
        "decision_at": fill["decision_at"],
        "token_id": yes[0],
        "book_capture_id": quote.capture_id,
        "rule_hash": quote.rule_hash,
        "fee_version": fee_version,
        "payout": str(payout),
        "curve": curve,
        "settlement_fee_assumption": "zero_native_protocol_fee; gas_and_relayer_excluded",
        "expense_scope": "native_collateral_trading_only",
    }
