"""Read-only advisory settlement lifecycle & redemption risk API; never executes or signs."""

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_strict_account
from app.api.routes.auth.auth import get_current_user_from_token
from app.config import get_settings
from app.models.platform.app_event import AppEvent
from app.models.users.account import Account
from app.services.analytics import settlement_ledger as ledger
from app.services.analytics.settlement_lifecycle import (
    SettlementAnalysisRequest,
    SettlementLifecycleReport,
    SettlementState,
    analyze_settlement_lifecycle,
)
from app.services.analytics.settlement_read_side import RecordedSettlementReport
from app.services.trading.position_lifecycle_service import read_recorded_settlement
from app.utils.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/settlement",
    tags=["settlement lifecycle"],
    dependencies=[Depends(get_current_user_from_token)],
)


def enabled() -> None:
    if not get_settings().settlement_redemption_enabled:
        raise HTTPException(503, "Settlement lifecycle analysis disabled")


@router.get("/schema", dependencies=[Depends(enabled)])
def schema() -> dict[str, Any]:
    return {
        "input_schema": SettlementAnalysisRequest.model_json_schema(),
        "output_schema": SettlementLifecycleReport.model_json_schema(),
    }


@router.post("/analyze", dependencies=[Depends(enabled)])
def analyze(payload: SettlementAnalysisRequest) -> SettlementLifecycleReport:
    report = analyze_settlement_lifecycle(payload)
    for discrepancy in report.discrepancies:
        logger.warning("settlement_scenario_discrepancy code=%s", discrepancy.code)
    return report


@router.get("/recorded", dependencies=[Depends(enabled)])
async def recorded(
    db: Session = Depends(get_db),
    account: Account = Depends(get_strict_account),
) -> RecordedSettlementReport:
    return await read_recorded_settlement(db, account)


class TransitionRequest(BaseModel):
    """A caller-asserted observation. It can never claim chain verification."""

    model_config = ConfigDict(extra="forbid")

    condition_id: str = Field(pattern=r"^0x[a-fA-F0-9]{64}$")
    token_id: str | None = Field(default=None, min_length=1, max_length=200)
    state: SettlementState
    observed_at: datetime
    evidence_class: str = Field(pattern=r"^(venue_reported|operator_asserted)$")
    rule_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    evidence: dict[str, Any] = Field(default_factory=dict)


def _rejected(exc: ledger.TransitionRejected) -> HTTPException:
    return HTTPException(422, exc.code)


@router.post("/transitions", dependencies=[Depends(enabled)])
def record_transition(
    payload: TransitionRequest,
    db: Session = Depends(get_db),
    account: Account = Depends(get_strict_account),
) -> dict[str, Any]:
    """Append an unverified observation; previous state is read from the ledger."""
    try:
        row, created = ledger.append_transition(
            db,
            account,
            condition_id=payload.condition_id,
            token_id=payload.token_id,
            state=payload.state,
            observed_at=payload.observed_at,
            evidence_class=payload.evidence_class,
            rule_hash=payload.rule_hash,
            evidence=payload.evidence,
        )
        db.commit()
    except ledger.TransitionRejected as exc:
        db.rollback()
        raise _rejected(exc) from exc
    return {"created": created, "transition": ledger.serialize(row)}


@router.get("/transitions", dependencies=[Depends(enabled)])
def transitions(
    condition_id: str = Query(pattern=r"^0x[a-fA-F0-9]{64}$"),
    db: Session = Depends(get_db),
    account: Account = Depends(get_strict_account),
) -> dict[str, Any]:
    try:
        rows = ledger.history(db, account, condition_id)
    except ledger.TransitionRejected as exc:
        raise _rejected(exc) from exc
    problems = ledger.verify_chain(rows)
    return {
        "schema_version": ledger.SCHEMA_VERSION,
        "condition_id": condition_id.lower(),
        "integrity": "verified" if not problems else "broken",
        "integrity_problems": problems,
        "transitions": [ledger.serialize(row) for row in rows],
    }


def _audit_discrepancy(db, account, code: str, scope: str) -> bool:
    identity = hashlib.sha256(f"{account.id}:{scope}:{code}".encode()).hexdigest()
    existing = db.scalar(
        select(AppEvent.id).where(
            AppEvent.aggregate_type == "settlement",
            AppEvent.aggregate_id == identity,
            AppEvent.event_type == "settlement_discrepancy_flagged",
        )
    )
    if existing is not None:
        return False
    db.add(
        AppEvent(
            user_id=account.owner_user_id,
            account_id=account.id,
            aggregate_type="settlement",
            aggregate_id=identity,
            event_type="settlement_discrepancy_flagged",
            payload_json=json.dumps({"code": code, "scope": scope, "review": "manual"}),
            source="settlement_sync",
        )
    )
    return True


@router.post("/sync", dependencies=[Depends(enabled)])
async def sync(
    db: Session = Depends(get_db),
    account: Account = Depends(get_strict_account),
) -> dict[str, Any]:
    """Observe chain/recorded evidence and append server-derived transitions.

    Writes only the append-only transition ledger and manual-review audit events;
    never signs, submits, redeems or credits cash.
    """
    report = await read_recorded_settlement(db, account)
    created, skipped = [], []
    for condition, resolution in sorted(report.resolutions.items()):
        if not resolution.get("resolved"):
            continue
        observed = report.as_of
        try:
            row, made = ledger.append_transition(
                db,
                account,
                condition_id=condition,
                state=SettlementState.FINAL,
                observed_at=observed,
                evidence_class="chain_verified",
                evidence={
                    "source": "ctf_payout_vector",
                    "numerators": resolution["numerators"],
                    "denominator": resolution["denominator"],
                },
                server_derived=True,
            )
            if made:
                created.append(ledger.serialize(row))
        except ledger.TransitionRejected as exc:
            skipped.append({"condition_id": condition, "code": exc.code})
    for attempt in report.attempts:
        condition = attempt["condition_id"].lower()
        tx = attempt.get("transaction_hash")
        if attempt.get("status") == "confirmed":
            state, evidence = (
                SettlementState.TX_CONFIRMED,
                {
                    key: attempt.get(key)
                    for key in (
                        "transaction_hash",
                        "block_hash",
                        "block_timestamp",
                        "confirmations",
                        "source_sha256",
                        "payout_usdc",
                        "gas_fee_native",
                        "index_sets",
                    )
                },
            )
            observed = report.as_of
        elif attempt.get("recorded_status") == "failed" and tx:
            state, evidence = (
                SettlementState.TX_FAILED,
                {
                    "transaction_hash": tx,
                    "recorded_status": "failed",
                },
            )
            observed = report.as_of
        elif attempt.get("recorded_status") == "submitted" and tx:
            state, evidence = (
                SettlementState.TX_PENDING,
                {
                    "transaction_hash": tx,
                    "recorded_status": "submitted",
                },
            )
            observed = report.as_of
        else:
            continue
        try:
            row, made = ledger.append_transition(
                db,
                account,
                condition_id=condition,
                state=state,
                observed_at=observed,
                evidence_class=(
                    "chain_verified"
                    if state == SettlementState.TX_CONFIRMED
                    else "application_recorded"
                ),
                evidence=evidence,
                server_derived=True,
            )
            if made:
                created.append(ledger.serialize(row))
        except ledger.TransitionRejected as exc:
            skipped.append({"condition_id": condition, "code": exc.code})
    flagged = 0
    codes = list(report.abstain_reasons) + list(report.accounting_reasons)
    for code in codes:
        flagged += _audit_discrepancy(db, account, code, "account")
    db.commit()
    return {
        "created": created,
        "skipped": skipped,
        "discrepancies_flagged_for_manual_review": flagged,
        "discrepancy_codes": sorted(set(codes)),
        "as_of": datetime.now(UTC).isoformat(),
    }
