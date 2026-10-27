"""Reprocess legacy metadata without reconstructing missing cash, fills or fees."""

import json

from app.models import PaperOrderRecord, UserTrade


def classify(db, *, after=0, limit=500, apply=False):
    rows = (
        db.query(UserTrade)
        .filter(UserTrade.id > after, UserTrade.status == "simulated")
        .order_by(UserTrade.id)
        .limit(max(1, min(limit, 1000)))
        .all()
    )
    legacy = 0
    for trade in rows:
        try:
            details = json.loads(trade.calculation_details or "{}")
        except (ValueError, TypeError):
            details = {"legacy_raw_calculation_details": trade.calculation_details}
        if not isinstance(details, dict):
            details = {"legacy_raw_calculation_details": trade.calculation_details}
        record = (
            db.get(PaperOrderRecord, details.get("paper_journal_order_id"))
            if details.get("paper_journal_order_id")
            else None
        )
        if (
            record
            and record.user_id == trade.user_id
            and record.account_id == trade.account_id
            and json.loads(record.result_json).get("trade_id") == trade.id
        ):
            continue
        legacy += 1
        if apply:
            details.update(
                model_version="legacy_unverified",
                quality="legacy_unverified",
                legacy_reason="Missing durable journal, unit, fill or fee evidence",
            )
            trade.calculation_details = json.dumps(details, sort_keys=True)
    if apply:
        db.flush()
    return {
        "scanned": len(rows),
        "legacy_unverified": legacy,
        "next_after": rows[-1].id if rows else None,
        "applied": apply,
    }
