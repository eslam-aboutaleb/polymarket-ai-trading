"""Compare paper outcomes and forecast skill by pre-decision market regime."""

from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from statistics import mean

REGIMES = ("quiet", "rising", "falling", "reversal", "unknown")
DIRECTION_THRESHOLD_BPS = Decimal(10)
REVERSAL_RANGE_BPS = Decimal(25)


def regime(move, spread):
    """Classify a same-source return and high-low range, both in basis points."""
    try:
        change, span = Decimal(str(move)), Decimal(str(spread))
    except InvalidOperation:
        return "unknown"
    if not change.is_finite() or not span.is_finite() or span < abs(change):
        return "unknown"
    if span >= REVERSAL_RANGE_BPS and abs(change) <= span / 2:
        return "reversal"
    if change >= DIRECTION_THRESHOLD_BPS:
        return "rising"
    if change <= -DIRECTION_THRESHOLD_BPS:
        return "falling"
    return "quiet"


def drawdown(pnls):
    equity = peak = worst = Decimal(0)
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return worst


def cohort(windows):
    scores = [w["score"]["scores"] for w in windows if "score" in w]
    keys = sorted({key for score in scores for key in score if "market" in score})
    comparisons = {}
    for key in keys:
        pairs = [
            (Decimal(score[key]), Decimal(score["market"]))
            for score in scores
            if key in score and "market" in score
        ]
        candidate = mean(a for a, _ in pairs)
        market = mean(b for _, b in pairs)
        comparisons[key] = {
            "windows": len(pairs),
            "brier": candidate,
            "matched_market_brier": market,
            "brier_improvement": market - candidate,
        }
    executions = [w["execution"]["order"] for w in windows if "execution" in w]
    filled = [o for o in executions if o.get("status") in {"filled", "partial"}]
    pnls = [
        Decimal(w["score"]["modeled_settlement_pnl"])
        for w in windows
        if w.get("score", {}).get("modeled_settlement_pnl") is not None
    ]
    venue_pnls = [
        Decimal(w["score"]["venue_fee_adjusted_pnl"])
        for w in windows
        if w.get("score", {}).get("venue_fee_adjusted_pnl") is not None
    ]
    return {
        "windows": len(windows),
        "forecasts": sum("forecast" in w for w in windows),
        "scored": len(scores),
        "held": sum(
            w["decision"].get("effective_action", w["decision"]["decision"]["action"]) == "HOLD"
            for w in windows
            if "decision" in w
        ),
        "unavailable": sum("unavailable" in w or "model_unavailable" in w for w in windows),
        "orders": len(executions),
        "fills": len(filled),
        "fill_rate": Decimal(len(filled)) / len(executions) if executions else None,
        "paper_fees": sum((Decimal(o["fee_usdc"]) for o in filled), Decimal(0)) if filled else None,
        "settled_fills": len(pnls),
        "fee_evidence_windows": sum(
            (w.get("venue_evidence") or w.get("inputs") or {}).get("fees") is not None
            for w in windows
        ),
        "ledger_posted_fills": sum(
            w.get("ledger_check", {}).get("settled") is True for w in windows
        ),
        "wins": sum(pnl > 0 for pnl in pnls),
        "losses": sum(pnl < 0 for pnl in pnls),
        "paper_net_pnl": sum(pnls) if pnls else None,
        "realized_drawdown_by_round": drawdown(pnls) if pnls else None,
        "venue_fee_adjusted_fills": len(venue_pnls),
        "venue_fee_adjusted_net_pnl": sum(venue_pnls) if venue_pnls else None,
        "venue_fee_adjusted_drawdown_by_round": drawdown(venue_pnls) if venue_pnls else None,
        "replay_status": dict(
            Counter(w["market_replay"]["result"]["status"] for w in windows if "market_replay" in w)
        ),
        "reference_checks": dict(
            Counter(
                "match" if w["venue_reference_close"]["matches"] else "mismatch"
                for w in windows
                if "venue_reference_close" in w
            )
        ),
        "forecast_skill": comparisons,
    }


def scorecard(rows, *, start=None, end=None):
    windows = {}
    diagnostics = Counter()
    for row in rows:
        stamp = row["start"]
        kind = row["kind"]
        if row.get("diagnostic") is True:
            if (start is None or stamp >= start) and (end is None or stamp < end):
                diagnostics[kind] += 1
            continue
        if kind == "batch":
            duration = row.get("duration", 300)
            stamps = range(stamp, stamp + row["windows"] * duration, duration)
            for expected in stamps:
                windows.setdefault(expected, {})
            continue
        window = windows.setdefault(stamp, {})
        if kind in {"score", "forecast", "execution"} and kind in window and window[kind] != row:
            # Duplicate collectors may write the same economics at different times.
            original = {k: v for k, v in window[kind].items() if k != "at"}
            current = {k: v for k, v in row.items() if k != "at"}
            if current != original:
                raise ValueError(f"Conflicting {kind} in window {stamp}")
        window[kind] = row
    groups = {key: [] for key in REGIMES}
    selected = []
    for stamp, window in sorted(windows.items()):
        if (start is not None and stamp < start) or (end is not None and stamp >= end):
            continue
        selected.append(window)
        proxy = window.get("inputs", {}).get("evidence", {}).get("proxy", {})
        groups[
            regime(proxy.get("same_source_return_bps"), proxy.get("same_source_range_bps"))
        ].append(window)
    return {
        "overall": cohort(selected),
        "diagnostic_records": dict(diagnostics),
        "regimes": {key: cohort(group) for key, group in groups.items()},
    }


def prior_skill(rows, first, duration):
    """Keep model selection on earlier, time-ordered, matching windows."""
    completed = {}
    for row in rows:
        if row["kind"] != "score" or row.get("duration", 300) != duration:
            continue
        scores = row["scores"]
        if "proxy_twap" not in scores or "market" not in scores or not row.get("resolved_at"):
            continue
        if (
            not row.get("at")
            or max(
                datetime.fromisoformat(row["resolved_at"]).timestamp(),
                datetime.fromisoformat(row["at"]).timestamp(),
            )
            >= first
        ):
            continue
        if row["start"] + duration > first:
            continue
        try:
            pair = Decimal(str(scores["proxy_twap"])), Decimal(str(scores["market"]))
        except InvalidOperation:
            return False
        if any(not score.is_finite() or not 0 <= score <= 1 for score in pair):
            return False
        if row["start"] in completed and completed[row["start"]] != pair:
            return False
        completed[row["start"]] = pair
    return len(completed) >= 20 and sum(a - b for a, b in completed.values()) < 0
