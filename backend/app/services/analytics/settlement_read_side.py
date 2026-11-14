"""Account-scoped settlement observations; no ledger writes or wallet credentials."""

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.models.trading.redemption_attempt import RedemptionAttempt
from app.models.users.user import User
from app.services.analytics.event_exposure_read_side import aware, recorded_exposure
from app.services.analytics.replay.ledger import ACCOUNTING_CONTEXT
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
    positions: list[dict[str, Any]] = Field(default_factory=list)
    attempts: list[dict[str, Any]] = Field(default_factory=list)
    sources: list[dict[str, Any]] = Field(default_factory=list)
    abstain_reasons: list[str] = Field(default_factory=list)
    observed_redemption_cashflows_usdc: Decimal | None = None
    accounting_status: Literal["abstain"] = "abstain"
    accounting_abstain_reason: str = "ACCOUNT_LEDGER_FINALITY_AND_FEE_CONVERSION_NOT_RECONCILED"
    available_cash_usdc: None = None
    realized_pnl_usdc: None = None
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
    try:
        exposures, sources = recorded_exposure(db, account, now)
        positions = [leg.model_dump(mode="json") for leg in exposures.legs]
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
            except (TimeoutError, ValueError) as exc:
                reasons.append(str(exc) or "ABSTAIN_RECEIPT_DEADLINE_EXCEEDED")
    # Existing records do not prove complete wallet inventory, fees or released
    # account collateral. Do not synthesize accounting totals from amount/status.
    for reason in sorted(set(reasons)):
        logger.warning("settlement_discrepancy account_id=%s code=%s", account.id, reason)
    return RecordedSettlementReport(
        status="abstain" if reasons else "observed",
        account_id=account.id,
        as_of=now,
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
    )
