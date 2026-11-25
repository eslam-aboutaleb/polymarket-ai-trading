"""Account-scoped settlement observations; no ledger writes or wallet credentials."""

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.models.analytics.live_balance_snapshot import LiveBalanceSnapshot
from app.models.trading.live_order import LiveOrder
from app.models.trading.redemption_attempt import RedemptionAttempt
from app.models.trading.user_trade import UserTrade
from app.models.users.user import User
from app.services.analytics.event_exposure_read_side import aware, recorded_exposure
from app.services.analytics.replay.ledger import ACCOUNTING_CONTEXT
from app.services.analytics.settlement_accounting import (
    OUTCOME_INDEX,
    AccountingReconciliation,
    reconcile_accounting,
)
from app.services.analytics.settlement_receipts import read_condition_resolution
from app.services.trading.redemption_service import read_redemption_cashflow

logger = logging.getLogger(__name__)


class RecordedSettlementReport(BaseModel):
    schema_version: Literal["settlement-lifecycle-v1"] = "settlement-lifecycle-v1"
    provenance_class: Literal["application_recorded_with_current_rpc_receipts"] = (
        "application_recorded_with_current_rpc_receipts"
    )
    execution_eligible: Literal[False] = False
    advisory_only: Literal[True] = True
    status: Literal["observed", "abstain"]
    account_id: int
    as_of: datetime
    positions_as_of: datetime
    positions: list[dict[str, Any]] = Field(default_factory=list)
    attempts: list[dict[str, Any]] = Field(default_factory=list)
    sources: list[dict[str, Any]] = Field(default_factory=list)
    abstain_reasons: list[str] = Field(default_factory=list)
    observed_redemption_cashflows_usdc: Decimal | None = None
    accounting_status: Literal["reconciled", "abstain"] = "abstain"
    accounting_abstain_reason: str | None = (
        "ACCOUNT_LEDGER_FINALITY_AND_FEE_CONVERSION_NOT_RECONCILED"
    )
    available_cash_usdc: Decimal | None = None
    realized_pnl_usdc: Decimal | None = None
    locked_collateral_usdc: Decimal | None = None
    realized_pnl_excludes_native_gas: Literal[True] = True
    conditions: list[dict[str, Any]] = Field(default_factory=list)
    accounting_reasons: list[str] = Field(default_factory=list)
    shadow_comparison: dict[str, Any] | None = None
    resolutions: dict[str, dict[str, Any]] = Field(default_factory=dict)
    limitations: list[str] = Field(
        default_factory=lambda: [
            (
                "Application-recorded executions/reservations only; external wallet "
                "activity and partial redemption share accounting require ledger reconciliation."
            ),
            (
                "Observed chain cash flow is not available account cash or realized "
                "P&L. Native gas has no verified USDC conversion."
            ),
            (
                "Only current canonical Polygon root CTF USDC.e payouts to the owner's "
                "EOA are supported; proxy/nested/other collateral abstain."
            ),
        ]
    )


async def recorded_settlement(db, account):
    now = datetime.now(UTC)
    reasons, sources, positions, attempts = [], [], [], []
    legs = None
    try:
        exposures, sources = recorded_exposure(db, account, now)
        legs = exposures.legs
        positions = [leg.model_dump(mode="json") for leg in legs]
    except ValueError as exc:
        reasons.append(str(exc))
    rows = db.scalars(
        select(RedemptionAttempt)
        .where(
            RedemptionAttempt.user_id == account.owner_user_id,
            RedemptionAttempt.account_id == account.id,
        )
        .order_by(RedemptionAttempt.created_at, RedemptionAttempt.id)
        .limit(21)
    ).all()
    if len(rows) > 20:
        reasons.append("ABSTAIN_REDEMPTION_HISTORY_BOUND_EXCEEDED")
        rows = []
    user = db.get(User, account.owner_user_id)
    seen = set()
    proofs: dict[str, list[dict]] = {}
    resolutions: dict[str, dict] = {}
    with localcontext(ACCOUNTING_CONTEXT):
        observed = Decimal(0)
        async with httpx.AsyncClient(timeout=2) as client:
            try:
                async with asyncio.timeout(8):
                    for row in rows:
                        result = {
                            "attempt_id": row.id,
                            "recorded_status": row.status,
                            "condition_id": row.market_id,
                            "transaction_hash": row.tx_hash,
                        }
                        attempts.append(result)
                        if aware(row.created_at) > now:
                            raise ValueError("ABSTAIN_FUTURE_REDEMPTION_RECORD")
                        if row.status == "preview" and row.tx_hash is None:
                            result["status"] = "preview_not_broadcast"
                            continue
                        if not row.tx_hash or row.status in {
                            "preview",
                            "legacy_unknown",
                            "pending",
                            "claim_via_ui",
                        }:
                            result["status"] = "unknown"
                            reasons.append("ABSTAIN_UNVERIFIED_REDEMPTION_STATUS")
                            continue
                        if row.tx_hash.lower() in seen:
                            result["status"] = "duplicate_not_counted"
                            reasons.append("ABSTAIN_DUPLICATE_REDEMPTION_TRANSACTION")
                            continue
                        seen.add(row.tx_hash.lower())
                        try:
                            proof = await read_redemption_cashflow(
                                client,
                                tx_hash=row.tx_hash,
                                condition_id=row.market_id,
                                wallet=user.wallet_address,
                            )
                            result.update(proof)
                            observed += Decimal(proof["payout_usdc"])
                            proofs.setdefault(row.market_id.lower(), []).append(proof)
                        except (
                            ValueError,
                            KeyError,
                            TypeError,
                            ArithmeticError,
                            httpx.HTTPError,
                        ) as exc:
                            result["status"] = "unknown"
                            result["reason"] = (
                                str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                            )
                            reasons.append("ABSTAIN_REDEMPTION_RECEIPT_UNVERIFIED")
                    for condition in sorted(proofs):
                        try:
                            resolutions[condition] = await read_condition_resolution(
                                client, condition_id=condition
                            )
                        except (ValueError, KeyError, TypeError, httpx.HTTPError) as exc:
                            logger.warning(
                                "settlement_resolution_unverified condition=%s %s",
                                condition,
                                type(exc).__name__,
                            )
            except (TimeoutError, ValueError) as exc:
                reasons.append(str(exc) or "ABSTAIN_RECEIPT_DEADLINE_EXCEEDED")
    # Accounting is recognized only by the exact reconciliation below; otherwise
    # every scope abstains with a code. No total is synthesized from amount/status.
    accounting = None
    if legs is not None and not reasons:
        accounting = _reconcile_account(db, account, legs, proofs, resolutions, now)
    for reason in sorted(set(reasons)):
        logger.warning("settlement_discrepancy account_id=%s code=%s", account.id, reason)
    if accounting is not None:
        for code in accounting.reasons:
            logger.warning("settlement_discrepancy account_id=%s code=%s", account.id, code)
    return RecordedSettlementReport(
        status="abstain" if reasons else "observed",
        account_id=account.id,
        as_of=datetime.now(UTC),
        positions_as_of=now,
        positions=positions,
        attempts=attempts,
        sources=sources,
        abstain_reasons=sorted(set(reasons)),
        observed_redemption_cashflows_usdc=observed
        if not any(
            "RECEIPT" in r or "DUPLICATE" in r or "STATUS" in r or "BOUND" in r or "FUTURE" in r
            for r in reasons
        )
        else None,
        resolutions=resolutions,
        **_accounting_fields(db, account, accounting),
    )


def latest_snapshot(db, account):
    from app.services.analytics.live_account_reconciliation import read_snapshot

    row = db.scalar(
        select(LiveBalanceSnapshot)
        .where(
            LiveBalanceSnapshot.user_id == account.owner_user_id,
            LiveBalanceSnapshot.account_id == account.id,
        )
        .order_by(LiveBalanceSnapshot.observed_at.desc(), LiveBalanceSnapshot.id)
        .limit(1)
    )
    if row is None:
        return None
    try:
        return read_snapshot(db, account, row.id)
    except (ValueError, LookupError, KeyError):
        return "INTEGRITY"


OPEN_ORDER_STATES = ("SUBMITTING", "UNKNOWN", "SUBMITTED", "PARTIALLY_FILLED")


def _reconcile_account(db, account, legs, proofs, resolutions, now):
    snapshot = latest_snapshot(db, account)
    reserved = db.scalar(
        select(func.coalesce(func.sum(LiveOrder.reserved), 0)).where(
            LiveOrder.user_id == account.owner_user_id,
            LiveOrder.account_id == account.id,
            LiveOrder.state.in_(OPEN_ORDER_STATES),
        )
    )
    if snapshot == "INTEGRITY":
        snapshot = None
    with localcontext(ACCOUNTING_CONTEXT):
        return reconcile_accounting(
            legs=legs,
            proofs=proofs,
            resolutions=resolutions,
            snapshot=snapshot,
            reserved_open_orders_usdc=Decimal(str(reserved)),
            token_index={leg.token_id: OUTCOME_INDEX[leg.outcome] for leg in legs},
            now=now,
        )


def _baseline_realized(db, account):
    """The existing bot's recorded sell-side realized P&L (read-only)."""
    value = db.scalar(
        select(func.coalesce(func.sum(UserTrade.pnl), 0)).where(
            UserTrade.user_id == account.owner_user_id,
            UserTrade.account_id == account.id,
            UserTrade.pnl.is_not(None),
        )
    )
    return Decimal(str(value))


def _accounting_fields(db, account, accounting: AccountingReconciliation | None):
    if accounting is None:
        return {}
    baseline = _baseline_realized(db, account)
    reconciled = accounting.status == "reconciled"
    fields = {
        "accounting_status": "reconciled" if reconciled else "abstain",
        "accounting_abstain_reason": None
        if reconciled
        else (accounting.reasons[0] if accounting.reasons else "ACCOUNTING_NOT_RECONCILED"),
        "available_cash_usdc": accounting.available_cash_usdc,
        "realized_pnl_usdc": accounting.realized_pnl_usdc,
        "locked_collateral_usdc": accounting.locked_collateral_usdc,
        "accounting_reasons": accounting.reasons,
        "conditions": [
            {
                "condition_id": c.condition_id,
                "status": c.status,
                "cost_basis_usdc": str(c.cost_basis_usdc),
                "payout_usdc": None if c.payout_usdc is None else str(c.payout_usdc),
                "realized_pnl_usdc": None
                if c.realized_pnl_usdc is None
                else str(c.realized_pnl_usdc),
                "gas_fee_native": None if c.gas_fee_native is None else str(c.gas_fee_native),
                "transaction_hashes": c.transaction_hashes,
                "reasons": c.reasons,
            }
            for c in accounting.conditions
        ],
    }
    if accounting.realized_pnl_usdc is not None:
        fields["shadow_comparison"] = {
            "baseline": "existing_bot_recorded_sell_side_realized_pnl",
            "baseline_realized_pnl_usdc": str(baseline),
            "settlement_realized_pnl_usdc": str(accounting.realized_pnl_usdc),
            "combined_realized_pnl_usdc": str(baseline + accounting.realized_pnl_usdc),
            "baseline_omits_redemption_pnl_usdc": str(accounting.realized_pnl_usdc),
        }
    return fields
