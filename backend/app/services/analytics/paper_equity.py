"""Account-scoped paper equity read model; legacy estimates remain explicitly unknown."""

import json
from datetime import datetime
from decimal import Decimal

from sqlalchemy import or_

from app.models import PaperNavPoint, PortfolioSnapshot
from app.services.market_data.replay_capture import aware
from app.services.users.account_service import (
    AccountAccessDenied,
    get_owned_account,
    get_personal_account_read_only,
)


def owner(db, user_id, account_id=None):
    account = (
        get_owned_account(db, user_id, account_id)
        if account_id is not None
        else get_personal_account_read_only(db, user_id)
    )
    if account is None:
        raise AccountAccessDenied("account unavailable")
    return account


def serialize(row):
    value = json.loads(row.valuation_json)

    def number(name):
        return float(value[name]) if value.get(name) is not None else None

    return {
        "captured_at": aware(row.captured_at).isoformat(),
        "bucket_hour": aware(row.bucket_hour).isoformat(),
        "account_id": row.account_id,
        "is_paper": True,
        "bankroll": number("nav"),
        "usdc_balance": number("cash"),
        "unrealized_pnl": number("unrealized_pnl"),
        "total_invested": number("cost_basis"),
        "total_current_value": number("position_value"),
        "positions_count": len(value["positions"]),
        "available_cash": number("available_cash"),
        "reserved_cash": number("reserved_cash"),
        "fees": number("fees"),
        "realized_pnl": number("realized_pnl"),
        "funding": number("funding"),
        "cash_flow_count": value.get("cash_flow_count", 0),
        "net_pnl": number("net_pnl"),
        "quality": value["quality"],
        "policy": value.get("policy", {}),
        "model_version": value["model_version"],
        "mark_provenance": value["mark_provenance"],
        "settlement_issues": value.get("settlement_issues", {}),
        "missing_marks": value["missing_marks"],
        "legacy_data_excluded": value["legacy_data_excluded"],
    }


def legacy(row, account_id=None):
    return {
        "captured_at": aware(row.captured_at).isoformat(),
        "bucket_hour": aware(row.bucket_hour).isoformat(),
        "account_id": account_id or row.account_id,
        "is_paper": True,
        "bankroll": None,
        "usdc_balance": None,
        "unrealized_pnl": None,
        "total_invested": None,
        "total_current_value": None,
        "positions_count": None,
        "quality": "legacy_unverified",
        "model_version": "legacy_paper_balance_only",
        "legacy_reported_balance": row.bankroll,
        "legacy_data_excluded": True,
    }


def history(db, user_id, account_id=None, *, start=None, end=None, limit=500):
    account = owner(db, user_id, account_id)
    start = aware(start) if start is not None else None
    end = aware(end) if end is not None else None
    query = db.query(PaperNavPoint).filter_by(user_id=user_id, account_id=account.id)
    if start is not None:
        query = query.filter(PaperNavPoint.bucket_hour >= start)
    if end is not None:
        query = query.filter(PaperNavPoint.bucket_hour < end, PaperNavPoint.captured_at < end)
    rows = query.order_by(PaperNavPoint.bucket_hour.desc()).limit(limit).all()
    points = [serialize(row) for row in rows]
    # Old paper_balance-only points are readable as unknown observations. Never
    # join their invented numeric baseline to the new economic epoch.
    old = db.query(PortfolioSnapshot).filter(
        PortfolioSnapshot.user_id == user_id,
        PortfolioSnapshot.is_paper.is_(True),
        or_(PortfolioSnapshot.account_id == account.id, PortfolioSnapshot.account_id.is_(None))
        if account.is_default
        else PortfolioSnapshot.account_id == account.id,
    )
    if start is not None:
        old = old.filter(PortfolioSnapshot.bucket_hour >= start)
    if end is not None:
        old = old.filter(PortfolioSnapshot.bucket_hour < end, PortfolioSnapshot.captured_at < end)
    occupied = {p["bucket_hour"] for p in points}
    for row in old.order_by(PortfolioSnapshot.bucket_hour.desc()).limit(limit):
        if aware(row.bucket_hour).isoformat() in occupied:
            continue
        points.append(legacy(row, account.id))

    return sorted(points, key=lambda point: point["bucket_hour"])[-limit:]


def summary(points, period):
    valid = bool(points) and all(
        p["bankroll"] is not None and p.get("funding") is not None for p in points
    )
    first = points[0]["bankroll"] if points else None
    last = points[-1]["bankroll"] if points else None
    result = {
        "period": period,
        "start": first,
        "end": last,
        "delta": None,
        "delta_pct": None,
        "max_drawdown_pct": None,
        "points": len(points),
        "quality": "journal_backed_modeled" if valid else "incomplete",
        "net_cash_flows": None,
    }
    if not valid:
        return result
    funding = Decimal(str(points[-1]["funding"])) - Decimal(str(points[0]["funding"]))
    delta = Decimal(str(last)) - Decimal(str(first)) - funding
    result.update(delta=float(delta), net_cash_flows=float(funding))
    # Cash flows are not returns. Percentage and drawdown are unavailable until
    # there is a consecutive, complete series without intervening contributions.
    same_funding = all(
        p["funding"] == points[0]["funding"]
        and p.get("cash_flow_count", 0) == points[0].get("cash_flow_count", 0)
        for p in points
    )
    consecutive = len(points) > 1 and all(
        (
            datetime.fromisoformat(b["bucket_hour"]) - datetime.fromisoformat(a["bucket_hour"])
        ).total_seconds()
        == 3600
        for a, b in zip(points, points[1:], strict=False)
    )
    if same_funding and consecutive and first > 0:
        peak, drawdown = first, 0
        for point in points:
            peak = max(peak, point["bankroll"])
            drawdown = max(drawdown, (peak - point["bankroll"]) / peak * 100 if peak else 0)
        result.update(delta_pct=float(delta / Decimal(str(first)) * 100), max_drawdown_pct=drawdown)
    return result
