"""Exact-Decimal settlement accounting reconciliation (#24); pure and side-effect free.

Realized P&L is recognized for a condition only when every input agrees:
recorded inventory, a canonical finalized redemption receipt, the CTF payout
vector, and a wallet snapshot taken after the receipt block. Available cash is
the observed collateral balance minus reservations; unsettled and unredeemed
positions are reported as locked and never counted as cash. Any disagreement
abstains for that scope and yields a discrepancy code for manual review.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from app.services.analytics.replay.ledger import ACCOUNTING_CONTEXT

USDC = Decimal("0.000001")
CASH_SNAPSHOT_MAX_AGE_SECONDS = 900
OUTCOME_INDEX = {"YES": 0, "NO": 1}


@dataclass
class ConditionResult:
    condition_id: str
    status: str  # reconciled | abstain | locked
    cost_basis_usdc: Decimal | None = None
    payout_usdc: Decimal | None = None
    realized_pnl_usdc: Decimal | None = None
    gas_fee_native: Decimal | None = None
    transaction_hashes: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


@dataclass
class AccountingReconciliation:
    status: str
    conditions: list[ConditionResult]
    available_cash_usdc: Decimal | None
    realized_pnl_usdc: Decimal | None
    locked_collateral_usdc: Decimal | None
    reasons: list[str]
    cash_reasons: list[str]
    realized_pnl_excludes_native_gas: bool = True


def _q(value: Decimal) -> Decimal:
    return value.quantize(USDC, rounding=ROUND_HALF_EVEN)


def reconcile_accounting(
    *,
    legs,
    proofs,
    resolutions,
    snapshot,
    reserved_open_orders_usdc: Decimal,
    token_index: dict[str, int],
    now: datetime,
) -> AccountingReconciliation:
    """Reconcile recorded inventory with chain proofs and a balance snapshot.

    ``legs`` are recorded ExposureLeg values. ``proofs`` maps condition ids to
    verified receipt proofs (dicts as returned by ``verify_redemption_receipt``).
    ``resolutions`` maps condition ids to ``read_condition_resolution`` results.
    ``token_index`` maps token ids to their CTF outcome index.
    """
    with localcontext(ACCOUNTING_CONTEXT):
        return _reconcile(
            legs, proofs, resolutions, snapshot, reserved_open_orders_usdc, token_index, now
        )


def _reconcile(legs, proofs, resolutions, snapshot, reserved, token_index, now):
    by_condition: dict[str, list] = {}
    for leg in legs:
        by_condition.setdefault(leg.condition_id.lower(), []).append(leg)
    for condition in proofs:
        by_condition.setdefault(condition.lower(), [])
    results, reasons = [], []
    redeemed_tokens: set[str] = set()
    locked = Decimal(0)
    for condition in sorted(by_condition):
        condition_legs = by_condition[condition]
        held = [leg for leg in condition_legs if leg.status != "pending_order"]
        pending = [leg for leg in condition_legs if leg.status == "pending_order"]
        cost = sum((leg.shares * (leg.entry_price + leg.fee_per_share) for leg in held), Decimal(0))
        found = proofs.get(condition, [])
        result = ConditionResult(condition, "locked", cost_basis_usdc=_q(cost))
        results.append(result)
        if not found:
            locked += cost + sum(
                (leg.shares * (leg.entry_price + leg.fee_per_share) for leg in pending),
                Decimal(0),
            )
            continue
        result.status = "abstain"
        why = result.reasons
        if pending:
            why.append("UNSETTLED_ORDER_ON_REDEEMED_CONDITION")
        nonzero = [p for p in found if Decimal(p["payout_usdc"]) != 0]
        hashes = sorted({p["transaction_hash"].lower() for p in found})
        result.transaction_hashes = hashes
        if len(nonzero) > 1:
            why.append("MULTIPLE_NONZERO_REDEMPTIONS_NEED_MANUAL_REVIEW")
        resolution = resolutions.get(condition)
        if not resolution or not resolution.get("resolved"):
            why.append("CONDITION_PAYOUT_NOT_FINAL_ON_CHAIN")
        if not held:
            why.append("NO_RECORDED_INVENTORY_FOR_REDEEMED_CONDITION")
        if not why:
            unmapped = [leg.token_id for leg in held if leg.token_id not in token_index]
            if unmapped:
                why.append("TOKEN_OUTCOME_INDEX_UNVERIFIED")
        if not why:
            expected = sum(
                (
                    leg.shares
                    * Decimal(resolution["numerators"][token_index[leg.token_id]])
                    / Decimal(resolution["denominator"])
                    for leg in held
                ),
                Decimal(0),
            )
            observed = sum((Decimal(p["payout_usdc"]) for p in found), Decimal(0))
            if _q(expected) != _q(observed):
                why.append("REDEMPTION_PAYOUT_DISAGREES_WITH_RECORDED_INVENTORY")
            result.payout_usdc = _q(observed)
        if not why:
            indexes = {i for p in found for i in p.get("index_sets", [])}
            needed = {token_index[leg.token_id] + 1 for leg in held}
            if not needed <= indexes:
                why.append("REDEEMED_INDEX_SETS_DO_NOT_COVER_RECORDED_INVENTORY")
        if not why:
            result.status = "reconciled"
            result.realized_pnl_usdc = _q(result.payout_usdc - cost)
            fees = [p.get("gas_fee_native") for p in found]
            result.gas_fee_native = sum((Decimal(f) for f in fees if f is not None), Decimal(0))
            redeemed_tokens.update(leg.token_id for leg in held)
        else:
            result.payout_usdc = None
        reasons.extend(f"{condition}:{code}" for code in why)
    pnl_scope = [r for r in results if r.status == "reconciled"]
    realized = _q(sum((r.realized_pnl_usdc for r in pnl_scope), Decimal(0))) if pnl_scope else None
    if any(r.status == "abstain" for r in results):
        realized = None  # a partially reconciled total would read as complete
    cash, cash_reasons = _available_cash(
        legs, results, snapshot, reserved, redeemed_tokens, proofs, now
    )
    status = "reconciled" if cash is not None and realized is not None else "abstain"
    if cash is None and "NO_BALANCE_SNAPSHOT" not in cash_reasons:
        reasons.extend(f"cash:{code}" for code in cash_reasons)
    elif cash is None:
        reasons.append("cash:NO_BALANCE_SNAPSHOT")
    if not pnl_scope and not any(r.status == "abstain" for r in results):
        realized = None
        reasons.append("NO_VERIFIED_REDEMPTION_TO_RECOGNIZE")
        status = "abstain"
    return AccountingReconciliation(
        status=status,
        conditions=results,
        available_cash_usdc=cash,
        realized_pnl_usdc=realized,
        locked_collateral_usdc=_q(locked),
        reasons=sorted(set(reasons)),
        cash_reasons=sorted(set(cash_reasons)),
    )


def _available_cash(legs, results, snapshot, reserved, redeemed_tokens, proofs, now):
    reasons: list[str] = []
    if snapshot is None:
        return None, ["NO_BALANCE_SNAPSHOT"]
    observed = datetime.fromisoformat(snapshot["observed_at"])
    if observed > now:
        reasons.append("SNAPSHOT_FROM_THE_FUTURE")
    elif (now - observed).total_seconds() > CASH_SNAPSHOT_MAX_AGE_SECONDS:
        reasons.append("STALE_BALANCE_SNAPSHOT")
    if snapshot.get("active_or_unsettled_orders"):
        reasons.append("ACTIVE_OR_UNSETTLED_ORDERS_AT_SNAPSHOT")
    for condition_proofs in proofs.values():
        for proof in condition_proofs:
            block_time = proof.get("block_timestamp")
            if not block_time:
                reasons.append("REDEMPTION_BLOCK_TIME_UNVERIFIED")
            elif datetime.fromisoformat(block_time) >= observed:
                reasons.append("SNAPSHOT_PREDATES_REDEMPTION")
    positions = {k: Decimal(v) for k, v in snapshot.get("positions", {}).items()}
    expected: dict[str, Decimal] = {}
    for leg in legs:
        if leg.status == "pending_order" or leg.token_id in redeemed_tokens:
            continue
        expected[leg.token_id] = expected.get(leg.token_id, Decimal(0)) + leg.shares
    for token in set(expected) | {t for t, v in positions.items() if v != 0}:
        if positions.get(token, Decimal(0)) != expected.get(token, Decimal(0)):
            reasons.append("WALLET_INVENTORY_DISAGREES_WITH_RECORDS")
    if any(r.status == "abstain" for r in results):
        reasons.append("UNRECONCILED_SETTLEMENT_SCOPE")
    if reasons:
        return None, reasons
    cash = Decimal(snapshot["cash_usdc"]) - reserved
    if cash < 0:
        return None, ["RESERVATIONS_EXCEED_OBSERVED_CASH"]
    return _q(cash), []
