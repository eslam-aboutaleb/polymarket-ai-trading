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


def freeze_report(db, account, opening_id, closing_id):
    report = build_reconciliation_report(db, account, opening_id, closing_id)
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
