"""Periodic NAV research statistics; never annualize irregular trade P&Ls."""

import math
import random
from itertools import combinations
from statistics import NormalDist, mean, stdev


def periodic_statistics(points, annualization_periods, sample_seconds):
    values = []
    missing = False
    for previous, current in zip(points, points[1:], strict=False):
        if (
            current["epoch"] - previous["epoch"] != sample_seconds
            or previous["nav"] is None
            or current["nav"] is None
        ):
            missing = True
            continue
        base = float(previous["nav"])
        if base <= 0:
            missing = True
            continue
        values.append(float(current["nav"]) / base - 1)
    result = {
        "nav_samples": len(points),
        "return_samples": len(values),
        "sample_seconds": sample_seconds,
        "annualization_periods": annualization_periods,
        "year_convention": "365 days unless frozen annualization override supplied",
        "risk_free_return": 0,
        "sharpe_ratio": None,
        "mean_return_ci95": None,
        "reason": None,
        "confidence_assumption": (
            "IID periodic returns; descriptive, not a profitability probability"
        ),
        "returns": values,
    }
    if missing:
        result["reason"] = "MISSING_OR_NONPERIODIC_NAV"
    elif len(values) < 30:
        result["reason"] = "INSUFFICIENT_PERIODIC_SAMPLES_MIN_30"
    elif stdev(values) == 0:
        result["reason"] = "ZERO_RETURN_VARIANCE"
    else:
        result["sharpe_ratio"] = mean(values) / stdev(values) * math.sqrt(annualization_periods)
        # Seeded research bootstrap, not a source of security randomness.
        rng = random.Random(0)  # noqa: S311
        means = sorted(mean(rng.choices(values, k=len(values))) for _ in range(200))
        result["mean_return_ci95"] = [means[4], means[194]]
    return result


def _sr(values):
    return mean(values) / stdev(values) if len(values) > 1 and stdev(values) else None


def family_statistics(series, declared_complete):
    result = {
        "deflated_sharpe_probability": None,
        "pbo": None,
        "trial_count": len(series),
        "aligned_return_samples": 0,
        "reason": None,
        "assumptions": (
            "Declared complete frozen family; IID Sharpe approximation, "
            "eight contiguous CSCV blocks; not independent replication"
        ),
    }
    if not declared_complete:
        result["reason"] = "ATTEMPTED_FAMILY_COMPLETENESS_NOT_DECLARED"
        return result
    if len(series) < 2 or len({len(s) for s in series}) != 1:
        result["reason"] = "INCOMPARABLE_OR_TOO_FEW_TRIALS"
        return result
    if len({tuple(values) for values in series}) != len(series):
        result["reason"] = "DUPLICATE_RETURN_SERIES_NOT_DISTINCT_TRIALS"
        return result
    n = len(series[0])
    result["aligned_return_samples"] = n
    ratios = [_sr(values) for values in series]
    if n < 30 or any(value is None for value in ratios):
        result["reason"] = "INSUFFICIENT_ALIGNED_NONCONSTANT_RETURNS"
        return result
    best = max(range(len(ratios)), key=lambda i: (ratios[i], -i))
    result["evaluated_trial_index"] = best
    result["diagnostic_selection_metric"] = (
        "maximum per-period validation Sharpe; separate from NAV-return winner"
    )
    values, sr = series[best], ratios[best]
    sigma, mu = stdev(values), mean(values)
    skew = mean(((x - mu) / sigma) ** 3 for x in values)
    kurtosis = mean(((x - mu) / sigma) ** 4 for x in values)
    nd = NormalDist()
    gamma = 0.5772156649015329
    trials = len(series)
    expected = stdev(ratios) * (
        (1 - gamma) * nd.inv_cdf(1 - 1 / trials) + gamma * nd.inv_cdf(1 - 1 / (trials * math.e))
    )
    variance = 1 - skew * sr + (kurtosis - 1) * sr * sr / 4
    if variance > 0:
        result["deflated_sharpe_probability"] = nd.cdf(
            (sr - expected) * math.sqrt(n - 1) / math.sqrt(variance)
        )
    if trials < 8 or n < 80:
        result["reason"] = "PBO_REQUIRES_8_TRIALS_AND_80_ALIGNED_RETURNS"
        return result
    block = n // 8
    below, evaluated = 0, 0
    for train_blocks in combinations(range(8), 4):
        train = [i for b in train_blocks for i in range(b * block, (b + 1) * block)]
        test = [
            i for b in range(8) if b not in train_blocks for i in range(b * block, (b + 1) * block)
        ]
        train_sr = [_sr([v[i] for i in train]) for v in series]
        test_sr = [_sr([v[i] for i in test]) for v in series]
        if any(v is None for v in train_sr + test_sr):
            continue
        winner = max(range(trials), key=lambda i: (train_sr[i], -i))
        rank = (
            sum(value < test_sr[winner] for value in test_sr)
            + (sum(value == test_sr[winner] for value in test_sr) + 1) / 2
        )
        below += rank / (trials + 1) <= 0.5
        evaluated += 1
    if evaluated != 70:
        result["reason"] = "DEGENERATE_CSCV_PARTITIONS"
    else:
        result.update(
            pbo=below / evaluated,
            reason=None,
            cscv_partitions=evaluated,
            discarded_tail_samples=n - block * 8,
        )
    return result
