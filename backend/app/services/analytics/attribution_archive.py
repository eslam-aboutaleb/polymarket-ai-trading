"""Owner-scoped immutable report artifacts; no financial-history updates."""

import json

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.analytics.live_balance_snapshot import LiveAttributionReport
from app.services.analytics.live_account_reconciliation import (
    build_reconciliation_report,
    canonical,
    digest,
)


def read_report(db, account, report_id):
    row = db.scalar(
        select(LiveAttributionReport).where(
            LiveAttributionReport.id == report_id,
            LiveAttributionReport.user_id == account.owner_user_id,
            LiveAttributionReport.account_id == account.id,
        )
    )
    if row is None:
        raise LookupError("Attribution report unavailable")
    report = json.loads(row.report_json)
    if digest(report) != row.id or report["input_hash"] != row.input_hash:
        raise ValueError("ATTRIBUTION_REPORT_INTEGRITY_MISMATCH")
    return {"report_id": row.id, **report}


def freeze_report(
    db,
    account,
    opening_id,
    closing_id,
    *,
    verify_chain=False,
    opening_state_report_id=None,
    study_id=None,
):
    opening_state = (
        read_report(db, account, opening_state_report_id) if opening_state_report_id else None
    )
    report = build_reconciliation_report(
        db,
        account,
        opening_id,
        closing_id,
        verify_chain=verify_chain,
        opening_state=opening_state,
    )
    if study_id:
        from app.services.analytics.attribution_contracts import acceptance_checks
        from app.services.analytics.attribution_studies import evaluate_study

        report["forward_study"] = evaluate_study(db, account, study_id, report)
        report["confidence"].update(
            forward_event_count=report["forward_study"]["event_count"],
            confidence_interval=report["forward_study"]["confidence_interval"],
            release_decision=report["forward_study"]["release_decision"],
        )
        report["input_hash"] = digest(
            {
                "attribution_input": report["input_hash"],
                "study_input": report["forward_study"]["input_hash"],
            }
        )
        report["acceptance"] = acceptance_checks(report)
        report["acceptance_complete"] = all(row["status"] == "pass" for row in report["acceptance"])
        report["verified_net_realized_pnl"] = (
            report["net_realized_pnl"] if report["acceptance_complete"] else None
        )
    identity = digest(report)
    row = LiveAttributionReport(
        id=identity,
        input_hash=report["input_hash"],
        user_id=account.owner_user_id,
        account_id=account.id,
        report_json=canonical(report),
    )
    try:
        # Both supported production/test databases support conflict-safe insert.
        if db.bind.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        db.execute(
            insert(LiveAttributionReport)
            .values(
                id=row.id,
                input_hash=row.input_hash,
                user_id=row.user_id,
                account_id=row.account_id,
                report_json=row.report_json,
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ValueError("ATTRIBUTION_REPORT_IDENTITY_CONFLICT") from None
    return read_report(db, account, identity)
