"""Evaluate social evidence against a web-only baseline without claiming alpha.

Every metric is ``None`` with a reason when the sample cannot support it. Nothing here
produces a positive "social edge" statement: the strongest output is a paired difference
with its sample size, and only when coverage and sample thresholds are both met.
"""

from dataclasses import dataclass
from datetime import datetime
from statistics import median

MIN_PAIRED_SAMPLES = 30
MIN_COVERAGE = 0.2
MIN_LABELLED = 20


@dataclass(frozen=True)
class LabelledMatch:
    """A post the system attributed to a market/entity, with a human label of correctness."""

    attributed: bool
    correct: bool


@dataclass(frozen=True)
class PairedForecast:
    """Same market, same as-of time, with and without social evidence; outcome is 0 or 1."""

    outcome: int
    p_web_only: float
    p_with_social: float
    social_available: bool


def attribution_precision(matches: list[LabelledMatch]) -> dict:
    attributed = [m for m in matches if m.attributed]
    if len(attributed) < MIN_LABELLED:
        return {"value": None, "n": len(attributed), "reason": "INSUFFICIENT_LABELLED_MATCHES"}
    return {"value": sum(m.correct for m in attributed) / len(attributed), "n": len(attributed)}


def index_lag(pairs: list[tuple[datetime | None, datetime | None]]) -> dict:
    """Seconds from posting to first observation, only where both times are real."""
    lags = [
        (seen - posted).total_seconds()
        for posted, seen in pairs
        if posted is not None and seen is not None and seen >= posted
    ]
    if not lags:
        return {"median_s": None, "n": 0, "reason": "NO_POSTED_TIMES"}
    return {"median_s": median(lags), "n": len(lags), "unknown_posted_time": len(pairs) - len(lags)}


def coverage(total_markets: int, markets_with_social: int) -> dict:
    if total_markets <= 0:
        return {"value": None, "reason": "NO_MARKETS"}
    return {"value": markets_with_social / total_markets, "n": total_markets}


def _brier(rows: list[tuple[float, int]]) -> float:
    return sum((p - y) ** 2 for p, y in rows) / len(rows)


def paired_brier(pairs: list[PairedForecast], total_markets: int | None = None) -> dict:
    """Chronological order is the caller's; this reports paired Brier on social-covered rows."""
    covered = [p for p in pairs if p.social_available]
    cov = coverage(total_markets if total_markets is not None else len(pairs), len(covered))
    base = {"coverage": cov, "n_paired": len(covered)}
    if len(covered) < MIN_PAIRED_SAMPLES:
        return {**base, "delta_brier": None, "reason": "INSUFFICIENT_PAIRED_SAMPLES"}
    if cov["value"] is None or cov["value"] < MIN_COVERAGE:
        return {**base, "delta_brier": None, "reason": "INSUFFICIENT_COVERAGE"}
    web = _brier([(p.p_web_only, p.outcome) for p in covered])
    soc = _brier([(p.p_with_social, p.outcome) for p in covered])
    return {
        **base,
        "brier_web_only": web,
        "brier_with_social": soc,
        "delta_brier": soc - web,  # negative is better; not a claim of significance or alpha
        "claim": "descriptive_only",
    }


def citation_quality(
    web_only_attributed: int,
    web_only_total: int,
    with_social_attributed: int,
    with_social_total: int,
) -> dict:
    if web_only_total <= 0 or with_social_total <= 0:
        return {"web_only": None, "with_social": None, "reason": "NO_CLAIMS"}
    return {
        "web_only": web_only_attributed / web_only_total,
        "with_social": with_social_attributed / with_social_total,
        "n_web_only": web_only_total,
        "n_with_social": with_social_total,
    }
